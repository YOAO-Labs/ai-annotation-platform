from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI
from unittest.mock import AsyncMock
import pytest

from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as t
from aap_assets_video_inputs import tracker_http as h
from aap_assets_video_inputs.values import (
    FrameSampling,
    FrameTimeEntry,
    FrozenFramePlan,
    FrozenVideoInput,
    ObjectIdentity,
    TaskVideoBinding,
    VideoBlob,
    VideoFacts,
    VideoSourceSnapshot,
)
from aap_assets_video_inputs.tracker_client import TrackerVideoInputClient
from aap_assets_video_inputs_runtime.tracker_api import install_tracker_routes
from aap_assets_video_inputs_runtime.tracker_control import TrackerVideoInputControl
from aap_assets_video_inputs_runtime.tracker_database import (
    AsyncpgTrackerVideoInputDatabase,
    TrackerVideoInputExecutorDatabase,
)

TOKEN = "tracker-control-token-for-runtime-tests-0001"


def _fixtures():
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    tracker_job_id, input_id, actor_id, task_id, project_id, item_id = (
        uuid4() for _ in range(6)
    )
    identity = ObjectIdentity(
        etag="source-etag", byte_size=4096, version_id="source-version-1"
    )
    task = TaskVideoBinding(
        task_id=task_id,
        project_id=project_id,
        task_version=3,
        task_source_generation=2,
        dataset_item_id=item_id,
        task_source_snapshot="a" * 64,
        file_path="datasets/internal/clip.mp4",
        file_type="video",
    )
    source = VideoSourceSnapshot(
        dataset_item_id=item_id,
        dataset_id=uuid4(),
        source_generation=4,
        source_snapshot="b" * 64,
        timetable_revision=5,
        object_identity=identity,
    )
    blob = VideoBlob(
        key=f"accepted-video-inputs/{input_id}/g1/a{uuid4()}/source",
        version_id="retained-video-version-7",
        etag="retained-etag",
        byte_size=4096,
        sha256="c" * 64,
    )
    video = FrozenVideoInput(
        input_id=input_id,
        job_id=tracker_job_id,
        actor_id=actor_id,
        task=task,
        source=source,
        blob=blob,
        video=VideoFacts(
            frame_count=3, width=64, height=48, duration_ms=300, video_stream=0
        ),
        created_at=now,
    )
    sampling = FrameSampling(max_frames=2, step=2, width=128, format="jpeg")
    plan = FrozenFramePlan.from_entries(
        video,
        sampling,
        (
            FrameTimeEntry(ordinal=1, frame_index=0, pts_ms=0, is_keyframe=True),
            FrameTimeEntry(ordinal=2, frame_index=2, pts_ms=200, is_keyframe=False),
        ),
    )
    execution = t.TrackerExecutionSpec(
        job_kind="tracking",
        model_key="mock_tracker",
        direction="forward",
        from_frame=0,
        to_frame=2,
        grid_step=2,
        correction_frame=None,
        segment_id=None,
        track_id_snapshot=None,
        target_class_name="car",
        target_tool_unit_id="bbox",
        prompt_snapshot={"text": "vehicle"},
        source_annotations=(),
        correction_mask=None,
    )
    source_facts = e.VideoCaptureSource(
        dataset_item_id=item_id,
        dataset_id=source.dataset_id,
        source_generation=source.source_generation,
        source_snapshot=source.source_snapshot,
        timetable_revision=source.timetable_revision,
        object_identity=identity,
        source_drifted=False,
        file_path=task.file_path,
        file_type="video",
        file_size=4096,
        content_hash=None,
    )
    scope = t.TrackerVideoInputScope(
        tracker_job_id=tracker_job_id,
        origin_actor_id=actor_id,
        task=task,
        source_facts=source_facts,
        execution=execution,
        request_digest="d" * 64,
        prompt_digest="e" * 64,
    )
    record = t.TrackerVideoInputRecord(
        input_id=input_id,
        scope=scope,
        status="ready",
        plan=plan,
        error=None,
        created_at=now,
        retire_after=None,
    )
    return SimpleNamespace(
        now=now,
        tracker_job_id=tracker_job_id,
        input_id=input_id,
        actor_id=actor_id,
        task_id=task_id,
        project_id=project_id,
        record=record,
        plan=plan,
        source_facts=source_facts,
        source_identity=identity,
        blob=blob,
    )


def _status(record, status="ready"):
    if status == "ready":
        return t.TrackerVideoInputStatus.from_record(record)
    pending = record.model_copy(update={"status": status, "plan": None, "error": None})
    return t.TrackerVideoInputStatus.from_record(pending)


@pytest.mark.asyncio
async def test_control_prepare_observes_source_and_signs_exact_retained_version():
    fixtures = _fixtures()
    pending = fixtures.record.model_copy(update={"status": "pending", "plan": None})
    work = t.TrackerVideoInputWork(record=pending, source_facts=fixtures.source_facts)
    material = t.TrackerVideoSourceMaterial(
        input=fixtures.record, source_facts=fixtures.source_facts
    )
    calls = []

    class Database:
        async def call(self, operation, request):
            calls.append((operation, request))
            if operation == "read_video_tracker_input":
                return work
            if operation == "prepare_video_tracker_input":
                return pending.model_copy(update={"status": "materializing"})
            if operation == "reference_video_tracker_source":
                return material
            raise AssertionError(operation)

    signed_url = (
        "https://objects.example/accepted-video-inputs/source"
        "?versionId=retained-video-version-7&signature=private"
    )
    storage = SimpleNamespace(
        assert_versioning=AsyncMock(),
        head_source=AsyncMock(return_value=fixtures.source_identity),
        verify_blob=AsyncMock(),
        sign_blob=AsyncMock(return_value=signed_url),
    )
    control = TrackerVideoInputControl(Database(), storage, url_expiry_seconds=300)
    prepare_request = t.TrackerInputRequest(
        actor_id=fixtures.actor_id,
        tracker_job_id=fixtures.tracker_job_id,
        task_id=fixtures.task_id,
        input_id=fixtures.input_id,
        request_digest=fixtures.record.scope.request_digest,
    )

    observed = await control.prepare(prepare_request)
    assert observed.status == "materializing"
    storage.assert_versioning.assert_awaited_once()
    native_prepare = calls[-1][1]
    assert calls[-1][0] == "prepare_video_tracker_input"
    assert native_prepare.object_identity == fixtures.source_identity
    assert native_prepare.source_snapshot == fixtures.source_facts.source_snapshot
    assert native_prepare.timetable_revision == fixtures.source_facts.timetable_revision
    assert native_prepare.terminal_error is None

    reference = await control.reference(
        t.TrackerSourceReferenceRequest(
            **prepare_request.model_dump(), plan_digest=fixtures.plan.plan_digest
        )
    )
    assert reference.url == signed_url
    assert reference.source_version.version_id == fixtures.blob.version_id
    assert reference.source_version.sha256 == fixtures.blob.sha256
    assert reference.source_version.byte_size == fixtures.blob.byte_size
    storage.verify_blob.assert_awaited_once_with(fixtures.blob)
    assert storage.sign_blob.await_args.kwargs["expires_in"] == 300
    wire = reference.model_dump(mode="json")
    assert "source_facts" not in wire
    assert "file_path" not in json.dumps(wire)
    assert fixtures.blob.key not in json.dumps(wire)


@pytest.mark.asyncio
async def test_control_prepare_freezes_source_drift_as_terminal_and_not_as_identity():
    fixtures = _fixtures()
    pending = fixtures.record.model_copy(update={"status": "pending", "plan": None})
    work = t.TrackerVideoInputWork(record=pending, source_facts=fixtures.source_facts)
    changed = fixtures.source_identity.model_copy(update={"etag": "replacement"})
    prepared = []

    class Database:
        async def call(self, operation, request):
            if operation == "read_video_tracker_input":
                return work
            if operation == "prepare_video_tracker_input":
                prepared.append(request)
                failed = pending.model_copy(
                    update={"status": "failed", "error": "source_changed"}
                )
                return failed
            raise AssertionError(operation)

    storage = SimpleNamespace(
        assert_versioning=AsyncMock(), head_source=AsyncMock(return_value=changed)
    )
    control = TrackerVideoInputControl(Database(), storage)
    request = t.TrackerInputRequest(
        actor_id=fixtures.actor_id,
        tracker_job_id=fixtures.tracker_job_id,
        task_id=fixtures.task_id,
        input_id=fixtures.input_id,
        request_digest=fixtures.record.scope.request_digest,
    )

    result = await control.prepare(request)
    assert result.status == "failed" and result.error == "source_changed"
    assert prepared[0].object_identity is None
    assert prepared[0].terminal_error == "source_changed"


class _FakeControl:
    def __init__(self, fixtures):
        self.fixtures = fixtures
        self.calls = []

    async def input(self, request):
        self.calls.append(("input", request))
        return _status(self.fixtures.record)

    async def prepare(self, request):
        self.calls.append(("prepare", request))
        return _status(self.fixtures.record)

    async def reference(self, request):
        self.calls.append(("reference", request))
        status = _status(self.fixtures.record)
        return t.TrackerVideoSourceReference(
            input=status,
            source_version=t.TrackerVideoObjectVersion(
                version_id=self.fixtures.blob.version_id,
                etag=self.fixtures.blob.etag,
                byte_size=self.fixtures.blob.byte_size,
                sha256=self.fixtures.blob.sha256,
            ),
            url=(
                "http://127.0.0.1:9999/accepted-video-inputs/source"
                "?versionId=" + self.fixtures.blob.version_id + "&signature=private"
            ),
            expires_in=300,
        )


@pytest.mark.asyncio
async def test_tracker_http_client_server_bounds_auth_scope_and_three_routes():
    fixtures = _fixtures()
    control = _FakeControl(fixtures)
    app = install_tracker_routes(FastAPI(), control, TOKEN)
    input_request = t.TrackerInputReadRequest(
        actor_id=fixtures.actor_id,
        tracker_job_id=fixtures.tracker_job_id,
        task_id=fixtures.task_id,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tracker.example"
    ) as http:
        async with TrackerVideoInputClient(
            "http://tracker.example", TOKEN, client=http
        ) as client:
            prepared = await client.prepare(
                t.TrackerInputRequest(
                    actor_id=fixtures.actor_id,
                    tracker_job_id=fixtures.tracker_job_id,
                    task_id=fixtures.task_id,
                    input_id=fixtures.input_id,
                    request_digest=fixtures.record.scope.request_digest,
                )
            )
            reference = await client.reference(
                t.TrackerSourceReferenceRequest(
                    actor_id=fixtures.actor_id,
                    tracker_job_id=fixtures.tracker_job_id,
                    task_id=fixtures.task_id,
                    input_id=fixtures.input_id,
                    request_digest=fixtures.record.scope.request_digest,
                    plan_digest=fixtures.plan.plan_digest,
                )
            )
            input_status = await client.input(input_request)
    assert input_status.input_id == fixtures.input_id
    assert prepared.status == "ready"
    assert reference.source_version.version_id == fixtures.blob.version_id
    assert [name for name, _ in control.calls] == [
        "prepare",
        "reference",
        "input",
    ]
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {h.HTTP_PREFIX + "/" + operation for operation in h.HTTP_OPERATIONS} <= paths
    assert not any(path.endswith("/capture") or "/claim" in path for path in paths)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://tracker.example"
    ) as http:
        denied = await http.post(
            h.HTTP_PREFIX + "/input",
            headers={"Authorization": "Bearer wrong-token"},
            json=input_request.model_dump(mode="json"),
        )
        oversized = await http.post(
            h.HTTP_PREFIX + "/input",
            headers={"Authorization": "Bearer " + TOKEN},
            content=b" " * (t.REQUEST_BYTES + 1),
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "permission_denied"
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "resource_budget_exceeded"


def test_shared_media_app_composes_tracker_routes_only_when_tracker_control_is_given():
    from aap_assets_media_runtime.api import create_app

    fixtures = _fixtures()
    app = create_app(object(), TOKEN, tracker_control=_FakeControl(fixtures))
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert {h.HTTP_PREFIX + "/" + operation for operation in h.HTTP_OPERATIONS} <= paths


def test_tracker_control_contract_and_openapi_snapshots_are_frozen():
    package = Path(h.__file__).parent
    assert (
        json.loads((package / "tracker-control.schema.json").read_text())
        == h.contract_document()
    )
    assert (
        json.loads((package / "tracker-openapi.snapshot.json").read_text())
        == h.http_document()
    )
    assert set(h.HTTP_OPERATIONS) == {"input", "prepare", "reference"}
    assert set(h.http_document()["paths"]) == {
        h.HTTP_PREFIX + "/" + operation for operation in h.HTTP_OPERATIONS
    }


@pytest.mark.asyncio
async def test_tracker_worker_adapter_maps_only_the_existing_video_input_executor_port():
    calls = []

    class Database:
        async def call(self, operation, request):
            calls.append((operation, request))
            return (operation, request)

    adapter = TrackerVideoInputExecutorDatabase(Database())
    for operation in (
        "claim_accepted_video_input",
        "heartbeat_accepted_video_input",
        "stage_accepted_video_times",
        "finish_accepted_video_input",
        "fail_accepted_video_input",
    ):
        request = object()
        assert await adapter.call(operation, request) == (
            TrackerVideoInputExecutorDatabase._OPERATIONS[operation],
            request,
        )
    assert [operation for operation, _request in calls] == list(
        TrackerVideoInputExecutorDatabase._OPERATIONS.values()
    )
    with pytest.raises(e.VideoInputFailure, match="invalid_request"):
        await adapter.call("claim_accepted_video_frame", object())


def test_tracker_asyncpg_adapter_requires_one_exact_worker_or_control_role():
    for roles in ({"control", "worker"}, {"labeling"}, set()):
        with pytest.raises(ValueError, match="tracker database role"):
            AsyncpgTrackerVideoInputDatabase(object(), allowed_roles=roles)


@pytest.mark.asyncio
async def test_node_peer_uses_only_sanitized_exact_tracker_reference():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    fixtures = _fixtures()
    control = _FakeControl(fixtures)
    app = install_tracker_routes(FastAPI(), control, TOKEN)
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    port = port_socket.getsockname()[1]
    port_socket.close()
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        package = Path(__file__).parent
        peer = package / "fixtures" / "video-tracker-input-http-peer.mjs"
        request = {
            "origin": f"http://127.0.0.1:{port}",
            "token": TOKEN,
            "input": {
                "actor_id": str(fixtures.actor_id),
                "tracker_job_id": str(fixtures.tracker_job_id),
                "task_id": str(fixtures.task_id),
            },
            "prepare": {
                "actor_id": str(fixtures.actor_id),
                "tracker_job_id": str(fixtures.tracker_job_id),
                "task_id": str(fixtures.task_id),
                "input_id": str(fixtures.input_id),
                "request_digest": fixtures.record.scope.request_digest,
            },
            "reference": {
                "actor_id": str(fixtures.actor_id),
                "tracker_job_id": str(fixtures.tracker_job_id),
                "task_id": str(fixtures.task_id),
                "input_id": str(fixtures.input_id),
                "request_digest": fixtures.record.scope.request_digest,
                "plan_digest": fixtures.plan.plan_digest,
            },
            "input_id": str(fixtures.input_id),
            "version_id": fixtures.blob.version_id,
            "plan_digest": fixtures.plan.plan_digest,
        }

        def run_peer():
            return subprocess.run(
                [node, str(peer)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )

        result = await asyncio.to_thread(run_peer)
        assert json.loads(result.stdout) == {
            "operations": ["input", "prepare", "reference"],
            "version_id": fixtures.blob.version_id,
            "plan_digest": fixtures.plan.plan_digest,
        }
    finally:
        server.should_exit = True
        await server_task
