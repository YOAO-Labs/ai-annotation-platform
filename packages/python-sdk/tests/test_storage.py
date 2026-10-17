"""Storage contracts and explicit maintenance submission."""

import json
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation import VideoAssetRetryRequest
from ai_annotation.cli.main import app

from .conftest import API, BASE

ITEM = "11111111-1111-4111-8111-111111111111"
ENV = {"AAP_BASE_URL": BASE, "AAP_API_KEY": "ak_test", "NO_COLOR": "1"}


def test_storage_health_and_bucket_partial_failure(client, respx_mock):
    respx_mock.get(f"{API}/storage/health").respond(
        200, json={"status": "ok", "bucket": "annotations"}
    )
    respx_mock.get(f"{API}/storage/buckets").respond(
        200,
        json={
            "items": [
                {
                    "name": "annotations",
                    "role": "annotations",
                    "status": "ok",
                    "object_count": 2,
                    "total_size_bytes": 17,
                },
                {
                    "name": "media",
                    "role": "media-cache",
                    "status": "error",
                    "error": "unavailable",
                    "future": True,
                },
            ],
            "total_object_count": 2,
            "total_size_bytes": 17,
        },
    )
    assert client.storage.health().bucket == "annotations"
    summary = client.storage.buckets()
    assert summary.total_object_count == 2
    assert summary.items[1].status == "error"
    assert summary.items[1].model_extra == {"future": True}


def test_video_failure_page_preserves_asset_locator(client, respx_mock):
    route = respx_mock.get(f"{API}/storage/video-assets/failures").respond(
        200,
        json={
            "items": [
                {
                    "asset_key": "frame:1",
                    "asset_type": "frame",
                    "dataset_item_id": ITEM,
                    "file_name": "clip.mp4",
                    "error": "generation failed",
                    "frame_index": 0,
                    "width": 512,
                    "format": "webp",
                    "task_id": None,
                }
            ],
            "total": 18,
            "limit": 3,
            "offset": 6,
        },
    )
    page = client.storage.video_asset_failures(limit=3, offset=6)
    assert page.total == 18 and len(page.items) == 1
    assert page.items[0].dataset_item_id == UUID(ITEM)
    assert page.items[0].frame_index == 0 and page.items[0].task_id is None
    assert dict(route.calls.last.request.url.params) == {"limit": "3", "offset": "6"}


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}, {"offset": True}],
)
def test_invalid_page_no_http(client, respx_mock, kwargs):
    with pytest.raises(ValueError):
        client.storage.video_asset_failures(**kwargs)
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "fields",
    [
        {"asset_type": "chunk"},
        {"asset_type": "frame", "frame_index": 0},
        {"asset_type": "frame", "frame_index": 0, "width": 4097, "format": "webp"},
    ],
)
def test_retry_requires_complete_actual_asset_locator(fields):
    with pytest.raises(ValidationError):
        VideoAssetRetryRequest(dataset_item_id=ITEM, **fields)


def test_retry_posts_exact_frame_and_does_not_wait(client, respx_mock):
    route = respx_mock.post(f"{API}/storage/video-assets/retry").respond(
        202, json={"status": "queued", "asset_type": "frame", "dataset_item_id": ITEM}
    )
    body = VideoAssetRetryRequest(
        asset_type="frame",
        dataset_item_id=ITEM,
        frame_index=0,
        width=512,
        format="webp",
    )
    result = client.storage.retry_video_asset(body)
    assert result.status == "queued"
    assert json.loads(route.calls.last.request.content) == body.model_dump(
        mode="json", exclude_unset=True
    )
    assert route.call_count == 1 and len(respx_mock.calls) == 1


def test_retry_network_failure_is_single_attempt(client, respx_mock):
    route = respx_mock.post(f"{API}/storage/video-assets/retry").mock(
        side_effect=httpx.ReadError("lost acknowledgement")
    )
    with pytest.raises(httpx.ReadError):
        client.storage.retry_video_asset(
            VideoAssetRetryRequest(asset_type="probe", dataset_item_id=ITEM)
        )
    assert route.call_count == 1


def test_installed_root_cli_confirmation_and_stdin(respx_mock):
    route = respx_mock.post(f"{API}/storage/video-assets/retry").respond(
        202, json={"status": "queued", "asset_type": "probe", "dataset_item_id": ITEM}
    )
    body = json.dumps({"asset_type": "probe", "dataset_item_id": ITEM})
    args = ["storage", "retry-video-asset", "--body", "-", "--json"]
    refusal = CliRunner().invoke(app, args, input=body, env=ENV)
    assert refusal.exit_code == 2 and not respx_mock.calls
    accepted = CliRunner().invoke(app, [*args, "--yes"], input=body, env=ENV)
    assert accepted.exit_code == 0, accepted.output
    assert json.loads(accepted.stdout)["status"] == "queued"
    assert route.call_count == 1
