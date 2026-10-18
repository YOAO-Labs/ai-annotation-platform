from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from aap_assets_media.contract import MediaFailure, MediaSource, ReadSourceRequest
from aap_assets_media.transport import SourceReference
from app.services import video_poster_references


def _source(item_id, *, drifted=False, materialized=2, generation=2, file_type="video"):
    return MediaSource(
        dataset_item_id=item_id,
        dataset_id=uuid4(),
        source_generation=generation,
        source_snapshot="a" * 64,
        timetable_revision=0,
        materialized_generation=materialized,
        object_identity=None,
        source_drifted=drifted,
        file_name="source.mp4",
        file_path="source/source.mp4",
        file_type=file_type,
        file_size=1,
        content_hash=None,
        width=16,
        height=9,
        thumbnail_path="media-assets/old-poster.webp",
        blurhash=None,
        video_metadata={"poster_frame_path": "media-assets/old-poster.webp"},
    )


def _reference(
    item_id, *, poster_url="http://minio:9000/poster.webp", **source_updates
):
    return SourceReference(
        source=_source(item_id, **source_updates),
        original_url="http://minio:9000/source.mp4",
        video_url="http://minio:9000/playback.mp4",
        poster_url=poster_url,
        expires_in=3600,
    )


@pytest.mark.asyncio
async def test_video_poster_urls_use_strict_owner_reference_and_isolate_failures(
    monkeypatch,
):
    actor_id = uuid4()
    item_ids = [uuid4() for _ in range(5)]
    requests = [
        ReadSourceRequest(
            actor_id=actor_id,
            dataset_item_id=item_id,
            task_id=uuid4(),
        )
        for item_id in item_ids
    ]
    calls = []
    active = 0
    peak = 0

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def source_reference(self, request):
            nonlocal active, peak
            calls.append(request)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                if request.dataset_item_id == item_ids[1]:
                    raise MediaFailure("source_changed")
                if request.dataset_item_id == item_ids[2]:
                    return _reference(request.dataset_item_id, drifted=True)
                if request.dataset_item_id == item_ids[3]:
                    return _reference(
                        request.dataset_item_id, materialized=1, generation=2
                    )
                if request.dataset_item_id == item_ids[4]:
                    return _reference(request.dataset_item_id, poster_url=None)
                return _reference(request.dataset_item_id)
            finally:
                active -= 1

    monkeypatch.setattr(
        video_poster_references, "media_gateway", lambda _db: FakeGateway()
    )
    monkeypatch.setattr(
        video_poster_references,
        "browser_media_url",
        lambda url: url.replace("http://minio:9000", "/minio"),
    )

    result = await video_poster_references.video_poster_urls(object(), requests)

    assert result == ["/minio/poster.webp", None, None, None, None]
    assert calls == requests
    assert peak == 4


@pytest.mark.asyncio
async def test_video_poster_urls_omit_nonvideo_and_broken_owner_replies(monkeypatch):
    actor_id, item_id, wrong_item_id = uuid4(), uuid4(), uuid4()
    requests = [
        ReadSourceRequest(actor_id=actor_id, dataset_item_id=item_id),
        ReadSourceRequest(actor_id=actor_id, dataset_item_id=wrong_item_id),
    ]

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def source_reference(self, request):
            if request.dataset_item_id == item_id:
                return _reference(item_id, file_type="image")
            return _reference(item_id)

    monkeypatch.setattr(
        video_poster_references, "media_gateway", lambda _db: FakeGateway()
    )
    monkeypatch.setattr(video_poster_references, "browser_media_url", lambda url: url)

    assert await video_poster_references.video_poster_urls(object(), requests) == [
        None,
        None,
    ]
