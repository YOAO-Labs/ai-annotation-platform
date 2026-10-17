"""SQL adapter for media summary facts used by task response composition."""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.services.assets_media_summary_contract import (
    TaskMediaSummary,
    TaskMediaSummaryReader,
)


def _copy_video_metadata(file_type: str, metadata: Any) -> dict[str, Any] | None:
    return (
        deepcopy(dict((metadata or {}).get("video") or {}))
        if file_type == "video"
        else None
    )


def _summary(
    width: int | None,
    height: int | None,
    thumbnail_path: str | None,
    blurhash: str | None,
    file_type: str,
    metadata: Any,
) -> TaskMediaSummary:
    return (
        width,
        height,
        thumbnail_path,
        blurhash,
        _copy_video_metadata(file_type, metadata),
    )


class SqlAlchemyTaskMediaSummaryReader(TaskMediaSummaryReader):
    """Bind task media summary reads to the route's existing AsyncSession."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def one(self, item_id: uuid.UUID) -> TaskMediaSummary | None:
        item = await self._db.get(DatasetItem, item_id)
        if item is None:
            return None
        return _summary(
            item.width,
            item.height,
            item.thumbnail_path,
            item.blurhash,
            item.file_type,
            item.metadata_,
        )

    async def many(
        self, item_ids: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, TaskMediaSummary]:
        if not item_ids:
            return {}
        rows = await self._db.execute(
            select(
                DatasetItem.id,
                DatasetItem.width,
                DatasetItem.height,
                DatasetItem.thumbnail_path,
                DatasetItem.blurhash,
                DatasetItem.file_type,
                DatasetItem.metadata_,
            ).where(DatasetItem.id.in_(item_ids))
        )
        return {
            row[0]: _summary(row[1], row[2], row[3], row[4], row[5], row[6])
            for row in rows
        }

    async def video_frame_count(self, item_id: uuid.UUID) -> int | None:
        item = await self._db.get(DatasetItem, item_id)
        if item is None:
            return None
        video = (item.metadata_ or {}).get("video")
        if not isinstance(video, dict):
            return None
        frame_count = video.get("frame_count")
        try:
            return int(frame_count) if frame_count is not None else None
        except (TypeError, ValueError):
            return None
