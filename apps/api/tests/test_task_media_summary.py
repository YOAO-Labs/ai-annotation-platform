from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_media.contract import MediaSummaries, MediaSummary, SummaryRequest
from app.api.v1.tasks._shared import _attach_dimensions, _attach_dimensions_batch
from app.services.assets_media_summary import AssetsTaskMediaSummaryReader


class _Gateway:
    def __init__(self, rows: dict):
        self.rows = rows
        self.requests: list[SummaryRequest] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call(self, operation, request):
        assert operation == "read_media_summaries"
        assert isinstance(request, SummaryRequest)
        self.requests.append(request)
        return MediaSummaries(
            items=[
                self.rows[item_id]
                for item_id in request.item_ids
                if item_id in self.rows
            ]
        )


def _summary(
    item_id,
    *,
    file_type="video",
    metadata=None,
    width=640,
    height=360,
    thumbnail_path="media-assets/derived/poster.webp",
    blurhash="source-blurhash",
    source_generation=1,
    materialized_generation=None,
    source_drifted=False,
):
    return MediaSummary(
        dataset_item_id=item_id,
        width=width,
        height=height,
        thumbnail_path=thumbnail_path,
        blurhash=blurhash,
        file_type=file_type,
        metadata=metadata or {},
        source_generation=source_generation,
        materialized_generation=(
            source_generation
            if materialized_generation is None
            else materialized_generation
        ),
        source_drifted=source_drifted,
    )


@pytest.mark.asyncio
async def test_owner_summary_reader_detaches_video_metadata_and_uses_owner_pages(
    monkeypatch,
):
    item_id = uuid4()
    raw_metadata = {"video": {"frame_count": "7", "extra": {"markers": ["source"]}}}
    gateway = _Gateway({item_id: _summary(item_id, metadata=raw_metadata)})
    monkeypatch.setattr(
        "app.services.assets_media_summary.media_gateway",
        lambda _db: gateway,
    )
    reader = AssetsTaskMediaSummaryReader(object())

    single = await reader.one(item_id)
    batch = await reader.many((item_id, item_id))

    assert single == (
        640,
        360,
        "media-assets/derived/poster.webp",
        "source-blurhash",
        raw_metadata["video"],
    )
    assert batch[item_id] == single
    assert single[4] is not raw_metadata["video"]
    single[4]["extra"]["markers"][0] = "response mutation"
    assert raw_metadata["video"]["extra"]["markers"] == ["source"]
    assert await reader.video_frame_count(item_id) == 7
    assert await reader.current_video_metadata(item_id) == raw_metadata["video"]
    assert await reader.current_video_summary(item_id) == (
        640,
        360,
        raw_metadata["video"],
    )
    assert [request.item_ids for request in gateway.requests] == [
        [item_id],
        [item_id],
        [item_id],
        [item_id],
        [item_id],
    ]


@pytest.mark.asyncio
async def test_owner_summary_frame_count_gates_stale_videos_and_keeps_legacy_nonvideo_values(
    monkeypatch,
):
    video_id, image_id, unknown_id, stale_id, drifted_id = (uuid4() for _ in range(5))
    gateway = _Gateway(
        {
            video_id: _summary(
                video_id, metadata={"video": {"frame_count": "unknown"}}
            ),
            image_id: _summary(
                image_id,
                file_type="image",
                metadata={"video": {"frame_count": 9}},
            ),
            stale_id: _summary(
                stale_id,
                metadata={"video": {"frame_count": 99}},
                source_generation=2,
                materialized_generation=1,
            ),
            drifted_id: _summary(
                drifted_id,
                metadata={"video": {"frame_count": 100}},
                source_drifted=True,
            ),
        }
    )
    monkeypatch.setattr(
        "app.services.assets_media_summary.media_gateway",
        lambda _db: gateway,
    )
    reader = AssetsTaskMediaSummaryReader(object())

    assert await reader.video_frame_count(video_id) is None
    assert await reader.video_frame_count(image_id) == 9
    assert await reader.video_frame_count(stale_id) is None
    assert await reader.video_frame_count(drifted_id) is None
    assert await reader.current_video_metadata(stale_id) is None
    assert await reader.current_video_metadata(drifted_id) is None
    assert await reader.video_frame_count(unknown_id) is None
    stale_summary = await reader.one(stale_id)
    assert stale_summary is not None
    assert stale_summary[:4] == (None, None, None, None)
    assert stale_summary[4] == {"frame_count": 99}


@pytest.mark.asyncio
async def test_image_dimensions_survive_stale_derivatives_but_thumbnails_do_not(
    monkeypatch,
):
    current_id, stale_id, drifted_id = (uuid4() for _ in range(3))
    gateway = _Gateway(
        {
            current_id: _summary(current_id, file_type="image"),
            stale_id: _summary(
                stale_id,
                file_type="image",
                source_generation=2,
                materialized_generation=1,
            ),
            drifted_id: _summary(
                drifted_id,
                file_type="image",
                source_drifted=True,
            ),
        }
    )
    monkeypatch.setattr(
        "app.services.assets_media_summary.media_gateway",
        lambda _db: gateway,
    )
    reader = AssetsTaskMediaSummaryReader(object())

    assert await reader.one(current_id) == (
        640,
        360,
        "media-assets/derived/poster.webp",
        "source-blurhash",
        None,
    )
    assert await reader.one(stale_id) == (640, 360, None, None, None)
    assert await reader.one(drifted_id) == (640, 360, None, None, None)


@pytest.mark.asyncio
async def test_task_summary_fallbacks_remain_distinct_when_owner_has_no_source(
    monkeypatch,
):
    item_id = uuid4()
    gateway = _Gateway({})
    monkeypatch.setattr(
        "app.services.assets_media_summary.media_gateway",
        lambda _db: gateway,
    )
    task = SimpleNamespace(
        id=uuid4(),
        dataset_item_id=item_id,
        thumbnail_path="tasks/fallback.webp",
        blurhash="task-blurhash",
    )

    single = await _attach_dimensions(object(), task)
    batch = await _attach_dimensions_batch(object(), [task])

    assert single == (None, None, task.thumbnail_path, task.blurhash, None)
    assert batch[task.id] == (None, None, None, None, None)

    source_less = SimpleNamespace(
        id=uuid4(),
        dataset_item_id=None,
        thumbnail_path="tasks/source-less.webp",
        blurhash="source-less-blurhash",
    )
    assert await _attach_dimensions(object(), source_less) == (
        None,
        None,
        source_less.thumbnail_path,
        source_less.blurhash,
        None,
    )
    assert (await _attach_dimensions_batch(object(), [source_less]))[
        source_less.id
    ] == (
        None,
        None,
        source_less.thumbnail_path,
        source_less.blurhash,
        None,
    )
