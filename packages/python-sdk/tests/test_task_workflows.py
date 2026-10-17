"""Task-context, image-pyramid, cross-frame, and lock contracts without live services."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation.cli import cross_frame_jobs as cross_frame_jobs_cli
from ai_annotation.cli import image_pyramids as image_pyramids_cli
from ai_annotation.cli import task_context as task_context_cli
from ai_annotation.cli import task_locks as task_locks_cli
from ai_annotation.errors import ConflictError, NotFoundError
from ai_annotation.image_pyramids import ImagePyramids
from ai_annotation.cross_frame_jobs import CrossFrameJobs
from ai_annotation.task_context import TaskContext
from ai_annotation.task_locks import TaskLocks
from ai_annotation.task_workflow_models import (
    CrossFrameJobCreateRequest,
    ImagePyramidAssetUrlsRequest,
    ImagePyramidOverviewAssetRequest,
    ImagePyramidResponse,
    ImagePyramidTileAssetRequest,
)
from ai_annotation.models import Job

from .conftest import API

runner = CliRunner()
TASK_ID = str(uuid4())
PROJECT_ID = str(uuid4())
USER_ID = str(uuid4())
LOCK_ID = str(uuid4())
JOB_ID = str(uuid4())
NOW = datetime.now(timezone.utc).isoformat()
CURSOR = "opaque|next+/="


def _annotation(task_id: str = TASK_ID) -> dict:
    return {
        "id": str(uuid4()),
        "task_id": task_id,
        "project_id": PROJECT_ID,
        "user_id": USER_ID,
        "source": "manual",
        "annotation_type": "bbox",
        "tool_unit_id": "bbox",
        "class_name": "cat",
        "geometry": {"type": "bbox", "x": 10, "y": 12, "width": 20, "height": 30},
        "confidence": None,
        "parent_prediction_id": None,
        "parent_annotation_id": None,
        "is_active": True,
        "ground_truth": False,
        "attributes": {},
        "version": 4,
        "created_at": NOW,
        "updated_at": NOW,
    }


def _job(status: str = "completed") -> dict:
    return {
        "id": JOB_ID,
        "kind": "point_cloud_cross_frame",
        "status": status,
        "project_id": PROJECT_ID,
        "user_id": USER_ID,
        "progress_pct": 100,
        "payload": {"scene_id": str(uuid4()), "source_task_id": TASK_ID},
        "result": {
            "items": [
                {"frame_index": 10, "status": "success", "created_count": 1},
                {"frame_index": 11, "status": "failed", "reason": "task_locked"},
            ]
        },
        "error_message": None,
        "project_display_id": "P-1",
        "project_name": "scene project",
        "celery_task_id": "celery-task-1",
        "created_at": NOW,
        "updated_at": NOW,
        "started_at": NOW,
        "completed_at": NOW,
    }


def _pyramid_manifest(task_id: str = TASK_ID) -> dict:
    return {
        "task_id": task_id,
        "status": "ready",
        "required": True,
        "retryable": False,
        "generation": 9,
        "building_generation": 10,
        "building_status": "building",
        "manifest": {
            "schema": "aap-image-pyramid/v1",
            "generation": 9,
            "sourceFingerprint": "fingerprint-9",
            "normalizationVersion": "3",
            "width": 4096,
            "height": 2048,
            "tileSize": 512,
            "overlap": 2,
            "format": "webp",
            "levels": [
                {
                    "level": 0,
                    "scaleFactor": 8,
                    "width": 512,
                    "height": 256,
                    "columns": 1,
                    "rows": 1,
                }
            ],
            "overview": {
                "width": 512,
                "height": 256,
                "contentDigest": "overview-digest",
            },
        },
        "overview": {
            "url": "https://storage.test/overview?signature=manifest-secret",
            "expires_at": NOW,
        },
    }


def test_task_context_forwards_cursor_segment_track_and_prediction_filters(
    client, respx_mock
):
    page_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/annotations/page").mock(
        return_value=httpx.Response(
            200,
            json={"items": [_annotation()], "next_cursor": CURSOR},
        )
    )
    page = TaskContext(client._http).annotations_page(
        TASK_ID, limit=1000, cursor=CURSOR, video_segment_id=LOCK_ID
    )
    assert page.next_cursor == CURSOR
    assert page.items[0].id
    assert page_route.calls.last.request.url.params["limit"] == "1000"
    assert page_route.calls.last.request.url.params["cursor"] == CURSOR
    assert page_route.calls.last.request.url.params["video_segment_id"] == LOCK_ID

    neighbor_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/neighbors").mock(
        return_value=httpx.Response(
            200,
            json={
                "scene_id": PROJECT_ID,
                "scene_name": "scene",
                "frame_index": 8,
                "scene_total_frames": 20,
                "prev": [{"task_id": str(uuid4()), "frame_index": 7}],
                "next": [{"task_id": str(uuid4()), "frame_index": 9}],
            },
        )
    )
    neighbors = TaskContext(client._http).neighbors(TASK_ID, k=20)
    assert neighbors.prev[0].frame_index == 7 and neighbors.next[0].frame_index == 9
    assert neighbor_route.calls.last.request.url.params["k"] == "20"

    track_id = "trk_a+b/1"
    neighbor_annotations_route = respx_mock.get(
        f"{API}/tasks/{TASK_ID}/neighbor-annotations"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "scene_id": PROJECT_ID,
                "frame_index": 8,
                "frames": [
                    {"task_id": str(uuid4()), "frame_index": 7, "annotations": []},
                    {
                        "task_id": str(uuid4()),
                        "frame_index": 9,
                        "annotations": [_annotation()],
                    },
                ],
            },
        )
    )
    result = TaskContext(client._http).neighbor_annotations(
        TASK_ID, k=2, track_id=track_id
    )
    assert result.frames[0].annotations == []
    assert result.frames[1].annotations[0].class_name == "cat"
    assert (
        neighbor_annotations_route.calls.last.request.url.params["track_id"] == track_id
    )

    respx_mock.get(f"{API}/tasks/{TASK_ID}/mask-capabilities").mock(
        return_value=httpx.Response(
            200,
            json={
                "read_enabled": True,
                "write_enabled": False,
                "legacy_polygon_commit_enabled": False,
                "project_enabled": True,
                "region_enabled": True,
                "reason": "task_locked",
                "max_dimension": 8192,
                "max_pixels": 67108864,
                "max_runs": 1000000,
                "max_bytes": 4194304,
            },
        )
    )
    caps = TaskContext(client._http).mask_capabilities(TASK_ID)
    assert caps.reason == "task_locked" and not caps.write_enabled

    predictions_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/predictions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": str(uuid4()),
                    "task_id": TASK_ID,
                    "project_id": PROJECT_ID,
                    "ml_backend_id": None,
                    "model_version": "model-4",
                    "score": 0.8,
                    "source": "ml_backend",
                    "tool_unit_id": "bbox",
                    "result": [
                        {
                            "type": "rectanglelabels",
                            "class_name": "cat",
                            "geometry": {"type": "bbox", "x": 1},
                            "confidence": 0.8,
                            "attributes": {},
                            "shape_index": 2,
                        },
                        {
                            "type": "rectanglelabels",
                            "class_name": "dog",
                            "geometry": {"type": "bbox", "x": 2},
                            "confidence": 0.7,
                            "attributes": {},
                            "shape_index": 7,
                        },
                    ],
                    "cluster": None,
                    "created_at": NOW,
                }
            ],
        )
    )
    predictions = TaskContext(client._http).predictions(
        TASK_ID,
        model_version="model-4",
        min_confidence=0.6,
        limit=20,
        offset=3,
    )
    assert [shape.shape_index for shape in predictions[0].result] == [2, 7]
    params = predictions_route.calls.last.request.url.params
    assert params["model_version"] == "model-4"
    assert params["min_confidence"] == "0.6"
    assert params["limit"] == "20" and params["offset"] == "3"


def test_image_pyramid_preserves_signed_generation_assets_and_celery_receipt(
    client, respx_mock
):
    manifest_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/image-pyramid").mock(
        return_value=httpx.Response(200, json=_pyramid_manifest())
    )
    api = ImagePyramids(client._http)
    manifest = api.manifest(TASK_ID)
    assert manifest.manifest is not None
    assert manifest.manifest.schema_ == "aap-image-pyramid/v1"
    assert manifest.manifest.levels[0].scale_factor == 8
    assert manifest.overview is not None
    assert "manifest-secret" not in repr(manifest)
    assert manifest.model_dump(mode="json", by_alias=True)["overview"]["url"].endswith(
        "manifest-secret"
    )
    assert manifest_route.calls.call_count == 1

    body = ImagePyramidAssetUrlsRequest(
        items=[
            ImagePyramidTileAssetRequest(kind="tile", generation=9, level=0, x=0, y=0),
            ImagePyramidOverviewAssetRequest(kind="overview", generation=9),
        ]
    )
    asset_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/image-pyramid/asset-urls"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "generation": 9,
                "expires_at": NOW,
                "items": [
                    {
                        "kind": "tile",
                        "generation": 9,
                        "level": 0,
                        "x": 0,
                        "y": 0,
                        "url": "https://storage.test/tile?signature=tile-secret",
                    },
                    {
                        "kind": "overview",
                        "generation": 9,
                        "url": "https://storage.test/overview?signature=asset-secret",
                    },
                ],
            },
        )
    )
    assets = api.asset_urls(TASK_ID, body)
    sent = json.loads(asset_route.calls.last.request.content)
    assert sent["items"][0]["generation"] == 9
    assert sent["items"][0]["kind"] == "tile"
    assert [item.generation for item in assets.items] == [9, 9]
    assert "tile-secret" not in repr(assets)
    assert assets.model_dump(mode="json")["items"][0]["url"].endswith("tile-secret")

    with pytest.raises(ValidationError):
        ImagePyramidAssetUrlsRequest(
            items=[
                ImagePyramidTileAssetRequest(
                    kind="tile", generation=9, level=0, x=0, y=0
                )
                for _ in range(129)
            ]
        )

    retry_route = respx_mock.post(f"{API}/tasks/{TASK_ID}/image-pyramid/retry").mock(
        return_value=httpx.Response(
            202,
            json={"task_id": TASK_ID, "status": "queued", "celery_task_id": "celery-9"},
        )
    )
    retry = api.retry(TASK_ID)
    assert retry.status == "queued" and retry.celery_task_id == "celery-9"
    assert retry_route.calls.call_count == 1


def test_image_pyramid_generation_conflict_is_not_refetched(client, respx_mock):
    stale_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/image-pyramid/asset-urls"
    ).mock(
        return_value=httpx.Response(
            409, json={"detail": {"reason": "stale_generation"}}
        )
    )
    with pytest.raises(ConflictError) as caught:
        ImagePyramids(client._http).asset_urls(
            TASK_ID,
            ImagePyramidAssetUrlsRequest(
                items=[ImagePyramidOverviewAssetRequest(kind="overview", generation=8)]
            ),
        )
    assert caught.value.status_code == 409
    assert stale_route.calls.call_count == 1


def test_cross_frame_jobs_reuse_async_job_models_and_preserve_partial_receipts(
    client, respx_mock
):
    api = CrossFrameJobs(client._http)
    list_route = respx_mock.get(f"{API}/tasks/{TASK_ID}/cross-frame-jobs").mock(
        return_value=httpx.Response(200, json={"items": [_job()], "total": 1})
    )
    jobs = api.list(TASK_ID, limit=50)
    assert jobs.items[0].kind == "point_cloud_cross_frame"
    assert jobs.items[0].result["items"][1]["status"] == "failed"
    assert jobs.items[0].celery_task_id == "celery-task-1"
    assert list_route.calls.last.request.url.params["limit"] == "50"

    annotation_id = str(uuid4())
    create_route = respx_mock.post(f"{API}/tasks/{TASK_ID}/cross-frame-jobs").mock(
        return_value=httpx.Response(202, json=_job("running"))
    )
    request = CrossFrameJobCreateRequest(
        scope="selected",
        annotation_ids=[annotation_id],
        direction="forward",
        start_frame=12,
        end_frame=20,
    )
    created = api.create(TASK_ID, request)
    sent = json.loads(create_route.calls.last.request.content)
    assert sent == {
        "scope": "selected",
        "annotation_ids": [annotation_id],
        "direction": "forward",
        "start_frame": 12,
        "end_frame": 20,
    }
    assert create_route.calls.last.request.headers.get("Idempotency-Key") is None
    assert create_route.calls.call_count == 1
    assert created.status == "running"

    retry_route = respx_mock.post(
        f"{API}/tasks/{TASK_ID}/cross-frame-jobs/{JOB_ID}/retry-failed"
    ).mock(return_value=httpx.Response(202, json=_job("pending")))
    retried = api.retry_failed(TASK_ID, JOB_ID)
    assert retried.result["items"][1]["reason"] == "task_locked"
    assert retry_route.calls.call_count == 1


def test_cross_frame_request_enforces_server_scope_and_range_contract():
    with pytest.raises(ValidationError):
        CrossFrameJobCreateRequest(
            scope="selected",
            annotation_ids=[],
            direction="forward",
            start_frame=1,
            end_frame=1,
        )
    with pytest.raises(ValidationError):
        CrossFrameJobCreateRequest(
            scope="all",
            direction="forward",
            start_frame=1,
            end_frame=101,
        )
    with pytest.raises(ValidationError):
        CrossFrameJobCreateRequest(
            scope="all",
            direction="forward",
            start_frame=2,
            end_frame=1,
            unexpected=True,
        )


def test_task_locks_are_current_actor_calls_without_unique_id_cas(client, respx_mock):
    api = TaskLocks(client._http)
    acquire_route = respx_mock.post(f"{API}/tasks/{TASK_ID}/lock").mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": TASK_ID,
                "user_id": USER_ID,
                "expire_at": NOW,
                "unique_id": LOCK_ID,
            },
        )
    )
    lock = api.acquire(TASK_ID)
    assert str(lock.unique_id) == LOCK_ID
    assert acquire_route.calls.last.request.content == b""

    heartbeat_route = respx_mock.post(f"{API}/tasks/{TASK_ID}/lock/heartbeat").mock(
        return_value=httpx.Response(200, json={"status": "renewed"})
    )
    heartbeat = api.heartbeat(TASK_ID)
    assert heartbeat.status == "renewed"
    assert heartbeat_route.calls.last.request.content == b""

    release_route = respx_mock.delete(f"{API}/tasks/{TASK_ID}/lock").mock(
        return_value=httpx.Response(204)
    )
    assert api.release(TASK_ID) is None
    assert release_route.calls.last.request.content == b""


def test_missing_current_lock_is_not_hidden(client, respx_mock):
    route = respx_mock.post(f"{API}/tasks/{TASK_ID}/lock/heartbeat").mock(
        return_value=httpx.Response(404, json={"detail": "No active lock found"})
    )
    with pytest.raises(NotFoundError):
        TaskLocks(client._http).heartbeat(TASK_ID)
    assert route.calls.call_count == 1


class _ClientContext:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *exc):
        return None


def test_cli_manifest_hides_signed_url_in_text_but_json_is_explicit(monkeypatch):
    class Resource:
        def manifest(self, task_id):
            assert task_id == TASK_ID
            return ImagePyramidResponse.model_validate(_pyramid_manifest())

    class FakeClient:
        image_pyramids = Resource()

    monkeypatch.setattr(
        image_pyramids_cli,
        "get_client",
        lambda *_args, **_kwargs: _ClientContext(FakeClient()),
    )
    text_result = runner.invoke(image_pyramids_cli.app, ["manifest", TASK_ID])
    json_result = runner.invoke(image_pyramids_cli.app, ["manifest", TASK_ID, "--json"])
    assert text_result.exit_code == 0, text_result.output
    assert "manifest-secret" not in text_result.output
    assert json_result.exit_code == 0, json_result.output
    assert "manifest-secret" in json_result.output


def test_cross_frame_cli_confirms_and_preserves_job_item_receipts(
    monkeypatch, tmp_path
):
    body_path = tmp_path / "cross-frame.json"
    body_path.write_text(
        json.dumps(
            {
                "scope": "all",
                "direction": "forward",
                "start_frame": 9,
                "end_frame": 12,
            }
        ),
        encoding="utf-8",
    )

    class Resource:
        def __init__(self):
            self.calls = []

        def create(self, task_id, body):
            self.calls.append((task_id, body))
            return Job.model_validate(_job("completed"))

    class Jobs:
        def wait(self, *_args, **_kwargs):
            pytest.fail("cross-frame creation must not poll or wait for the AsyncJob")

    class FakeClient:
        def __init__(self):
            self.cross_frame_jobs = Resource()
            self.jobs = Jobs()

    client = FakeClient()

    def no_client(*_args, **_kwargs):
        pytest.fail("confirmation refusal must precede client creation")

    monkeypatch.setattr(cross_frame_jobs_cli, "get_client", no_client)
    declined = runner.invoke(
        cross_frame_jobs_cli.app,
        ["create", TASK_ID, "--body", str(body_path), "--json"],
    )
    assert declined.exit_code == 2 and "--yes" in declined.output

    monkeypatch.setattr(
        cross_frame_jobs_cli,
        "get_client",
        lambda *_args, **_kwargs: _ClientContext(client),
    )
    result = runner.invoke(
        cross_frame_jobs_cli.app,
        ["create", TASK_ID, "--body", str(body_path), "--yes", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["result"]["items"][1]["status"] == "failed"
    assert len(client.cross_frame_jobs.calls) == 1
    sent_task, sent_body = client.cross_frame_jobs.calls[0]
    assert sent_task == TASK_ID and sent_body.scope == "all"


@pytest.mark.parametrize(
    ("command", "args"),
    [
        (image_pyramids_cli, ["retry", TASK_ID, "--json"]),
        (cross_frame_jobs_cli, ["retry-failed", TASK_ID, JOB_ID, "--json"]),
        (task_locks_cli, ["acquire", TASK_ID, "--json"]),
        (task_locks_cli, ["heartbeat", TASK_ID, "--json"]),
        (task_locks_cli, ["release", TASK_ID, "--json"]),
    ],
)
def test_cli_confirmation_refusal_happens_before_client_creation(
    monkeypatch, command, args
):
    def unexpected_client(*_args, **_kwargs):
        pytest.fail("declined action must not construct a client or send a request")

    monkeypatch.setattr(command, "get_client", unexpected_client)
    result = runner.invoke(command.app, args)
    assert result.exit_code == 2
    assert "--yes" in result.output


def test_task_context_cli_passes_literal_cursor_and_segment(monkeypatch):
    class Page:
        def model_dump(self, **_kwargs):
            return {"items": [], "next_cursor": CURSOR}

    class ContextResource:
        def __init__(self):
            self.call = None

        def annotations_page(self, task_id, **kwargs):
            self.call = (task_id, kwargs)
            return Page()

    resource = ContextResource()

    class FakeClient:
        task_context = resource

    monkeypatch.setattr(
        task_context_cli,
        "get_client",
        lambda *_args, **_kwargs: _ClientContext(FakeClient()),
    )
    result = runner.invoke(
        task_context_cli.app,
        [
            "annotations-page",
            TASK_ID,
            "--cursor",
            CURSOR,
            "--video-segment-id",
            LOCK_ID,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["next_cursor"] == CURSOR
    assert resource.call == (
        TASK_ID,
        {"limit": 200, "cursor": CURSOR, "video_segment_id": LOCK_ID},
    )
