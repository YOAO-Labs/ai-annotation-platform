from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from aap_assets_media.contract import VideoTotals, VideoTotalsRequest
from app.services.data_management import service as data_management


class _Gateway:
    def __init__(self, *, matched_delta: int = 0):
        self.requests: list[VideoTotalsRequest] = []
        self.matched_delta = matched_delta

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call(self, operation: str, request: VideoTotalsRequest) -> VideoTotals:
        assert operation == "read_video_media_totals"
        assert isinstance(request, VideoTotalsRequest)
        self.requests.append(request)
        return VideoTotals(
            frame_count=2 * len(request.item_ids),
            duration_ms=10 * len(request.item_ids),
            matched_items=len(request.item_ids) + self.matched_delta,
        )


@pytest.mark.asyncio
async def test_video_totals_aggregate_bounded_batches_and_preserve_task_duplicates(
    monkeypatch,
):
    gateway = _Gateway()
    monkeypatch.setattr(data_management, "VIDEO_TOTALS_BATCH_ITEMS", 2)
    monkeypatch.setattr("app.services.assets_media.media_gateway", lambda _db: gateway)
    item_ids = [uuid4(), uuid4(), uuid4()]

    duration_ms, frame_count = await data_management._read_video_media_totals(
        object(), [item_ids[0], item_ids[0], item_ids[1], item_ids[2]]
    )

    assert [request.item_ids for request in gateway.requests] == [
        [item_ids[0], item_ids[0]],
        [item_ids[1], item_ids[2]],
    ]
    assert duration_ms == 40
    assert frame_count == 8


@pytest.mark.asyncio
async def test_video_totals_reject_source_scope_change_for_retry(monkeypatch):
    gateway = _Gateway(matched_delta=-1)
    monkeypatch.setattr(data_management, "VIDEO_TOTALS_BATCH_ITEMS", 2)
    monkeypatch.setattr("app.services.assets_media.media_gateway", lambda _db: gateway)

    with pytest.raises(HTTPException) as exc_info:
        await data_management._read_video_media_totals(object(), [uuid4()])

    assert exc_info.value.status_code == 409
    assert exc_info.value.headers == {"Retry-After": "3"}
