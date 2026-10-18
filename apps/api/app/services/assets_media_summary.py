"""Bind task response media summaries to the independent Assets media owner."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_media.contract import MediaFailure, MediaSummary, SummaryRequest

from app.services.assets_media import media_gateway, media_http_error
from app.services.assets_media_summary_contract import (
    TaskMediaSummary,
    TaskMediaSummaryReader,
)

SUMMARY_BATCH_SIZE = 500


def _summary(row: MediaSummary) -> TaskMediaSummary:
    video_metadata: dict[str, Any] | None = None
    derived_video_facts_current = row.file_type != "video" or (
        not row.source_drifted and row.materialized_generation == row.source_generation
    )
    thumbnail_current = row.file_type not in {"image", "video"} or (
        not row.source_drifted and row.materialized_generation == row.source_generation
    )
    if row.file_type == "video":
        raw = row.metadata or {}
        if isinstance(raw.get("video"), dict):
            raw = raw["video"]
        video_metadata = deepcopy(dict(raw))
    return (
        row.width if derived_video_facts_current else None,
        row.height if derived_video_facts_current else None,
        row.thumbnail_path if thumbnail_current else None,
        row.blurhash if thumbnail_current else None,
        video_metadata,
    )


def _video_document(row: MediaSummary) -> dict[str, Any]:
    raw = row.metadata or {}
    if isinstance(raw.get("video"), dict):
        raw = raw["video"]
    return deepcopy(dict(raw)) if isinstance(raw, dict) else {}


def _current_video_document(row: MediaSummary) -> dict[str, Any] | None:
    if (
        row.file_type != "video"
        or row.source_drifted
        or row.materialized_generation != row.source_generation
    ):
        return None
    return _video_document(row)


class AssetsTaskMediaSummaryReader(TaskMediaSummaryReader):
    """Keep Task response tuples while delegating source facts to Assets.

    Raw video metadata stays available to legacy diagnostics. Methods that use it
    for frame ranges, dimensions, or aggregate metrics must use the freshness-aware
    projections below.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _read(
        self, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, MediaSummary]:
        if not item_ids:
            return {}
        found: dict[uuid.UUID, MediaSummary] = {}
        try:
            async with media_gateway(self._db) as gateway:
                for offset in range(0, len(item_ids), SUMMARY_BATCH_SIZE):
                    batch = item_ids[offset : offset + SUMMARY_BATCH_SIZE]
                    result = await gateway.call(
                        "read_media_summaries", SummaryRequest(item_ids=list(batch))
                    )
                    requested = set(batch)
                    for row in result.items:
                        if (
                            row.dataset_item_id not in requested
                            or row.dataset_item_id in found
                        ):
                            raise MediaFailure("invalid_response")
                        found[row.dataset_item_id] = row
        except MediaFailure as exc:
            raise media_http_error(exc) from None
        return found

    async def one(self, item_id: uuid.UUID) -> TaskMediaSummary | None:
        row = (await self._read((item_id,))).get(item_id)
        return _summary(row) if row is not None else None

    async def many(
        self, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, TaskMediaSummary]:
        unique_ids = tuple(dict.fromkeys(item_ids))
        summaries = await self._read(unique_ids)
        return {item_id: _summary(row) for item_id, row in summaries.items()}

    async def video_frame_count(self, item_id: uuid.UUID) -> int | None:
        row = (await self._read((item_id,))).get(item_id)
        if row is None:
            return None
        if row.file_type == "video":
            video = _current_video_document(row)
        else:
            raw = row.metadata or {}
            video = raw.get("video") if isinstance(raw.get("video"), dict) else None
        if video is None:
            return None
        frame_count = video.get("frame_count")
        try:
            return int(frame_count) if frame_count is not None else None
        except (TypeError, ValueError):
            return None

    async def current_video_metadata(self, item_id: uuid.UUID) -> dict[str, Any] | None:
        row = (await self._read((item_id,))).get(item_id)
        return None if row is None else _current_video_document(row)

    async def current_video_metadata_many(
        self, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, dict[str, Any] | None]:
        unique_ids = tuple(dict.fromkeys(item_ids))
        rows = await self._read(unique_ids)
        return {
            item_id: _current_video_document(row)
            for item_id, row in rows.items()
            if row.file_type == "video"
        }

    async def current_video_summary(
        self, item_id: uuid.UUID
    ) -> tuple[int | None, int | None, dict[str, Any]] | None:
        row = (await self._read((item_id,))).get(item_id)
        if row is None:
            return None
        video = _current_video_document(row)
        if video is None:
            return None
        return row.width or video.get("width"), row.height or video.get("height"), video
