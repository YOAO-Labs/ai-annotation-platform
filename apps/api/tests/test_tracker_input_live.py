"""Owned Media processes and actual HTTP model reads of frozen tracker video."""

from contextlib import asynccontextmanager
from hashlib import sha256
import asyncio
import json
import os
import sys
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, text

from aap_assets_video_inputs import execution as e
from aap_assets_video_inputs import tracker as t
from aap_assets_video_inputs.tracker_client import TrackerVideoInputClient
from training_contract.observability import redact_text
from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.task import Task
from app.db.models.video_tracker_job import VideoTrackerJob
from app.services.accepted_tracker_inputs import capture_video_tracker_input
from app.services.video_tracking.runner import (
    accept_tracker_job,
    decide_tracker_job,
    run_tracker_job,
)
from tests.conftest import create_registry_with_pool
from tests.factory import build_tool_bindings
from tests.fixtures.tracker_video_ml_peer import PROGRAM
from tests.test_assets_catalog_native import _seed_actor
from tests.test_assets_media_live import (
    _free_loopback_port,
    _process_log,
    _runtime_database_url,
    _runtime_environment,
    _start_process,
    _wait_control_ready,
    _wait_process_exit,
)
from tests.test_video_input_authorities_native import _seed_video_task
from tests.test_video_input_live import _video_bytes, _video_runtime

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires owned role/S3/process qualification",
    ),
]


async def _call(db, operation, request):
    definition = t.TRACKER_OPERATIONS[operation]
    parameters = request.model_dump(mode="json", by_alias=True)
    for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
        if kind == "jsonb" and parameters[name] is not None:
            parameters[name] = json.dumps(parameters[name], allow_nan=False)
    placeholders = ", ".join(
        f"CAST(:{name} AS {kind})"
        for name, kind in zip(definition.arguments, definition.sql_types, strict=True)
    )
    raw = await db.scalar(
        text(f"SELECT {definition.namespace}.{operation}({placeholders})"),
        parameters,
    )
    return t.parse_native(operation, request, raw)


@asynccontextmanager
async def _model_peer(runtime, tmp_path, body, *, gate_after_calls=0):
    port = _free_loopback_port()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "AAP_TRACKER_PEER_STORAGE_ORIGIN": runtime.endpoint,
        "AAP_TRACKER_PEER_SOURCE_SHA256": sha256(body).hexdigest(),
        "AAP_TRACKER_PEER_PORT": str(port),
        "AAP_TRACKER_PEER_GATE_AFTER_CALLS": str(gate_after_calls),
    }
    log = tmp_path / "tracker-model-peer.log"
    process, stream = _start_process([sys.executable, "-c", PROGRAM], environment, log)
    runtime.processes.append(process)
    runtime.streams.append(stream)
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False
    ) as client:
        await _wait_control_ready(client, process, log)
        try:
            yield client
        finally:
            if process.poll() is None:
                try:
                    await client.post("/__acceptance/release")
                except httpx.HTTPError:
                    pass


async def _seed_tracker(runtime, body, *, backend_url=None, output_geometry="bbox"):
    key = f"tracker-input-qualification/{uuid4()}.mp4"
    runtime.owner.put_object(
        Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"], Key=key, Body=body
    )
    async with runtime.sessions.begin() as db:
        actor, project, _, item_id, task_id = await _seed_video_task(db)
        unit = "region" if output_geometry == "mask" else "bbox"
        project.tool_bindings = build_tool_bindings(["car"], unit=unit)
        item = await db.get(DatasetItem, item_id)
        item.file_path, item.file_size = key, len(body)
        item.content_hash = sha256(body).hexdigest()
        item.width, item.height = 80, 48
        item.metadata_ = {
            "video": {
                "fps": 6,
                "frame_count": 6,
                "duration_ms": 1000,
                "width": 80,
                "height": 48,
            }
        }
        # A linked Task need not duplicate the Assets object's locator.
        task = await db.get(Task, task_id)
        task.file_path = ""
        if backend_url is not None:
            _, pool = await create_registry_with_pool(
                db,
                name=f"tracker-input-{uuid4().hex[:10]}",
                url=backend_url,
                enabled_pool=True,
                health_meta={"capabilities": {"supported_trackers": ["sam2_video"]}},
            )
            project.ml_backend_pool_id = pool.id
            db.add(
                ProjectMLBackendPool(
                    project_id=project.id, pool_id=pool.id, enabled=True
                )
            )
        job_id = uuid4()
        job = VideoTrackerJob(
            id=job_id,
            task_id=task_id,
            dataset_item_id=item_id,
            created_by=actor,
            status="queued",
            model_key="sam2_video" if backend_url is not None else "mock_bbox",
            direction="forward",
            from_frame=0,
            to_frame=5,
            target_class_name="car",
            target_tool_unit_id=unit,
            prompt={"text": "car", "output_geometry": output_geometry},
            event_channel=f"video-tracker-job:{job_id}",
        )
        db.add(job)
        await db.flush()
        record = await capture_video_tracker_input(
            db, actor_id=actor, tracker_job_id=job.id, grid_step=1
        )
        job_id, project_id = job.id, project.id
    return actor, project_id, item_id, task_id, job_id, key, record


async def _materialize(runtime, tmp_path, status):
    environment = _runtime_environment(
        tmp_path=tmp_path,
        role="worker",
        database_url=_runtime_database_url(runtime.worker_url),
        endpoint=runtime.endpoint,
        public_endpoint=runtime.endpoint,
        datasets_bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
        cache_bucket=runtime.buckets["MINIO_MEDIA_CACHE_BUCKET"],
        access_key=runtime.worker_access,
        secret_key=runtime.worker_secret,
    )
    log = tmp_path / "tracker-original-worker.log"
    process, stream = _start_process(
        [
            sys.executable,
            "-m",
            "aap_assets_media_runtime.worker",
            "--once",
            "--tracker-input-id",
            str(status.input_id),
        ],
        environment,
        log,
    )
    runtime.processes.append(process)
    runtime.streams.append(stream)
    await _wait_process_exit(
        process, timeout=45, message="tracker original materializer did not finish"
    )
    assert process.returncode == 0, _process_log(log)


async def _prepared(runtime, tmp_path, actor, task_id, job_id):
    client = TrackerVideoInputClient(runtime.origin, runtime.token, client=runtime.http)
    scope = t.TrackerInputReadRequest(
        actor_id=actor, tracker_job_id=job_id, task_id=task_id
    )
    captured = await client.input(scope)
    request = t.TrackerInputRequest(
        **scope.model_dump(),
        input_id=captured.input_id,
        request_digest=captured.request_digest,
    )
    prepared = await client.prepare(request)
    assert prepared.status in {"pending", "materializing"}
    await _materialize(runtime, tmp_path, prepared)
    ready = await client.input(scope)
    assert ready.status == "ready" and ready.video.frame_count == 6
    return client, request, ready


async def _wait_gate(peer, running):
    async with asyncio.timeout(15):
        while True:
            if running.done():
                result = await running
                pytest.fail(
                    "tracker stopped before the model response gate: "
                    + redact_text(f"{result.status}: {result.error_message}")
                )
            response = await peer.get("/__acceptance/calls")
            response.raise_for_status()
            observation = response.json()
            if observation["gate"]["entered"]:
                return observation
            await asyncio.sleep(0.05)


async def _silent_event(_channel, _event):
    return None


@pytest.mark.parametrize("review_mode", ["all", "windows", "mask"])
async def test_tracker_http_windows_keep_one_original_version_and_pin_after_review(
    test_engine, tmp_path, monkeypatch, review_mode
):
    async with _video_runtime(test_engine, tmp_path) as runtime:
        body = await asyncio.to_thread(_video_bytes, tmp_path)
        async with _model_peer(runtime, tmp_path, body, gate_after_calls=1) as peer:
            actor, project, _, task, job, _, _ = await _seed_tracker(
                runtime,
                body,
                backend_url=str(peer.base_url).rstrip("/"),
                output_geometry="mask" if review_mode == "mask" else "bbox",
            )
            client, request, ready = await _prepared(
                runtime, tmp_path, actor, task, job
            )
            reference = await client.reference(
                t.TrackerSourceReferenceRequest(
                    **request.model_dump(), plan_digest=ready.plan_digest
                )
            )
            assert reference.source_version.sha256 == sha256(body).hexdigest()
            assert (await runtime.http.get(reference.url)).content == body
            async with runtime.sessions.begin() as db:
                blob = await db.scalar(
                    text(
                        "SELECT plan #> '{input,blob}' FROM "
                        "public.asset_accepted_video_inputs WHERE input_id=:input"
                    ),
                    {"input": ready.input_id},
                )
                blob = json.loads(blob) if isinstance(blob, str) else blob
                await db.execute(
                    text(
                        "UPDATE public.projects SET video_sampling=CAST(:sampling AS jsonb) "
                        "WHERE id=:project"
                    ),
                    {
                        "project": project,
                        "sampling": json.dumps({"mode": "step", "frame_step": 3}),
                    },
                )
            monkeypatch.setattr(settings, "assets_media_url", runtime.origin)
            monkeypatch.setattr(settings, "assets_media_token", runtime.token)
            monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
            monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
            async with runtime.sessions() as db:
                running = asyncio.create_task(
                    run_tracker_job(db, job, publisher=_silent_event)
                )
                try:
                    await _wait_gate(peer, running)
                    runtime.owner.put_object(
                        Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                        Key=blob["key"],
                        Body=b"a different latest version must not retarget the job",
                    )
                    (await peer.post("/__acceptance/release")).raise_for_status()
                    result = await asyncio.wait_for(running, timeout=45)
                finally:
                    if not running.done():
                        running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
            assert result.status == "pending_review", result.error_message
            assert result.staged_result["grid_step"] == 1
            calls = (await peer.get("/__acceptance/calls")).json()["calls"]
            assert len(calls) == 3
            assert {row["source_sha256"] for row in calls} == {sha256(body).hexdigest()}
            assert {row["version_id"] for row in calls} == {blob["version_id"]}
            async with runtime.sessions.begin() as db:
                reviewer = await _seed_actor(db)
                await db.execute(
                    text("UPDATE public.users SET is_active=false WHERE id=:actor"),
                    {"actor": actor},
                )
            async with runtime.sessions() as db:
                if review_mode != "windows":
                    accepted = await accept_tracker_job(
                        db,
                        job,
                        actor_id=reviewer,
                        privileged=True,
                        publisher=_silent_event,
                    )
                else:
                    for first, last in ((0, 2), (3, 5)):
                        current = await db.get(VideoTrackerJob, job)
                        accepted = await decide_tracker_job(
                            db,
                            job,
                            instance_ids=["1"],
                            from_frame=first,
                            to_frame=last,
                            decision="accept",
                            expected_source_versions={
                                UUID(key): value
                                for key, value in (current.prompt or {})
                                .get("expected_source_versions", {})
                                .items()
                            },
                            job_revision=current.revision,
                            actor_id=reviewer,
                            privileged=True,
                            publisher=_silent_event,
                        )
                        if first == 0:
                            assert accepted.status == "partially_reviewed"
                assert accepted.status == "accepted"
                annotations = (
                    await db.scalars(
                        select(Annotation).where(Annotation.task_id == task)
                    )
                ).all()
                assert len(annotations) == 1
                assert [
                    item["frame_index"] for item in annotations[0].geometry["keyframes"]
                ] == list(range(6))
                if review_mode == "mask":
                    assert annotations[0].annotation_type == "video_track_mask"
                    assert all(
                        row["mask"]["size"] == [48, 80]
                        for row in annotations[0].geometry["keyframes"]
                    )
                parent_id = annotations[0].id
            async with runtime.sessions.begin() as db:
                await db.execute(
                    text("DELETE FROM public.video_tracker_jobs WHERE id=:job"),
                    {"job": job},
                )
                retained = await _call(
                    db,
                    "video_tracker_input_referenced",
                    t.TrackerVideoInputReferenceRequest(input_id=ready.input_id),
                )
                assert retained.referenced
                db.add(
                    Annotation(
                        task_id=task,
                        project_id=project,
                        user_id=reviewer,
                        parent_annotation_id=parent_id,
                        annotation_type="bbox",
                        class_name="car",
                        geometry={
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.2,
                            "w": 0.3,
                            "h": 0.4,
                        },
                    )
                )
                await db.flush()
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.annotation_video_tracker_input_sources "
                            "WHERE input_id=:input"
                        ),
                        {"input": ready.input_id},
                    )
                    == 2
                )
                await db.execute(
                    text(
                        "UPDATE public.asset_accepted_video_inputs SET "
                        "retire_after=pg_catalog.clock_timestamp()-interval '1 second',"
                        "retention_checked_at=NULL WHERE input_id=:input"
                    ),
                    {"input": ready.input_id},
                )
                await _call(
                    db,
                    "sweep_video_tracker_inputs",
                    e.SweepVideoInputsRequest(limit=100),
                )
                state = (
                    await db.execute(
                        text(
                            "SELECT status,retire_after,retention_checked_at FROM "
                            "public.asset_accepted_video_inputs WHERE input_id=:input"
                        ),
                        {"input": ready.input_id},
                    )
                ).one()
                assert state[0] == "ready" and state[1] is None and state[2] is not None
            original = runtime.owner.get_object(
                Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                Key=blob["key"],
                VersionId=blob["version_id"],
            )
            with original["Body"] as stream:
                assert stream.read() == body


@pytest.mark.parametrize("fault", ["source", "retained"])
async def test_tracker_reference_rejects_changed_or_missing_exact_bytes(
    test_engine, tmp_path, fault
):
    async with _video_runtime(test_engine, tmp_path) as runtime:
        body = await asyncio.to_thread(_video_bytes, tmp_path)
        actor, _, _, task, job, source_key, _ = await _seed_tracker(runtime, body)
        client, request, ready = await _prepared(runtime, tmp_path, actor, task, job)
        reference_request = t.TrackerSourceReferenceRequest(
            **request.model_dump(), plan_digest=ready.plan_digest
        )
        reference = await client.reference(reference_request)
        async with runtime.sessions() as db:
            key = await db.scalar(
                text(
                    "SELECT plan #>> '{input,blob,key}' FROM "
                    "public.asset_accepted_video_inputs WHERE input_id=:input"
                ),
                {"input": ready.input_id},
            )
        if fault == "source":
            runtime.owner.put_object(
                Bucket=runtime.buckets["MINIO_DATASETS_BUCKET"],
                Key=source_key,
                Body=b"current source bytes changed without changing SQL metadata",
            )
        else:
            runtime.owner.delete_object(
                Bucket=runtime.buckets["MINIO_INPUTS_BUCKET"],
                Key=key,
                VersionId=reference.source_version.version_id,
            )
        expected = "source_changed" if fault == "source" else "input_corrupt"
        with pytest.raises(t.TrackerFailure, match=expected):
            await client.reference(reference_request)


async def test_tracker_cancel_stops_new_windows_but_preserves_finished_candidates(
    test_engine, tmp_path, monkeypatch
):
    async with _video_runtime(test_engine, tmp_path) as runtime:
        body = await asyncio.to_thread(_video_bytes, tmp_path)
        async with _model_peer(runtime, tmp_path, body, gate_after_calls=1) as peer:
            actor, _, _, task, job, _, _ = await _seed_tracker(
                runtime, body, backend_url=str(peer.base_url).rstrip("/")
            )
            client, request, ready = await _prepared(
                runtime, tmp_path, actor, task, job
            )
            monkeypatch.setattr(settings, "assets_media_url", runtime.origin)
            monkeypatch.setattr(settings, "assets_media_token", runtime.token)
            monkeypatch.setattr(settings, "video_tracker_window_size_frames", 2)
            monkeypatch.setattr(settings, "ml_backend_router_mode", "off")
            async with runtime.sessions() as db:
                running = asyncio.create_task(
                    run_tracker_job(db, job, publisher=_silent_event)
                )
                try:
                    await _wait_gate(peer, running)
                    async with runtime.sessions.begin() as canceller:
                        await canceller.execute(
                            text(
                                "UPDATE public.video_tracker_jobs SET status='cancelled',"
                                "cancel_requested_at=pg_catalog.clock_timestamp(),"
                                "completed_at=pg_catalog.clock_timestamp() WHERE id=:job"
                            ),
                            {"job": job},
                        )
                    (await peer.post("/__acceptance/release")).raise_for_status()
                    result = await asyncio.wait_for(running, timeout=45)
                finally:
                    if not running.done():
                        running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
            assert result.status == "cancelled", result.error_message
            assert [row["frame_index"] for row in result.staged_result["results"]] == [
                0,
                1,
            ]
            calls = (await peer.get("/__acceptance/calls")).json()["calls"]
            assert len(calls) == 2
            with pytest.raises(t.TrackerFailure, match="job_not_active"):
                await client.reference(
                    t.TrackerSourceReferenceRequest(
                        **request.model_dump(), plan_digest=ready.plan_digest
                    )
                )
            async with runtime.sessions() as db:
                accepted = await accept_tracker_job(
                    db,
                    job,
                    actor_id=actor,
                    privileged=True,
                    publisher=_silent_event,
                )
                assert accepted.status == "accepted"
