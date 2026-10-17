"""Bind media result writes to their asset and labeling domain owners."""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset import DatasetService
from app.services.labeling_tasks import LabelingTaskService


class SqlAlchemyMediaMaterializationWriter:
    def __init__(self, db: AsyncSession) -> None:
        self.datasets = DatasetService(db)
        self.labeling_tasks = LabelingTaskService(db)

    async def set_item_dimensions(
        self, item_id: UUID, dimensions: tuple[int, int]
    ) -> None:
        await self.datasets.set_item_dimensions(item_id, dimensions)

    async def record_thumbnail_error(self, item_id: UUID, error: str) -> None:
        await self.datasets.record_thumbnail_error(item_id, error)

    async def set_item_thumbnail(
        self, item_id: UUID, thumbnail_path: str, *, blurhash: str
    ) -> None:
        await self.datasets.set_item_thumbnail(
            item_id, thumbnail_path, blurhash=blurhash
        )

    async def replace_video_frame_index(
        self,
        item_id: UUID,
        frame_timetable: Sequence[Mapping[str, Any]],
    ) -> None:
        await self.datasets.replace_video_frame_index(item_id, frame_timetable)

    async def set_video_dimensions(
        self,
        item_id: UUID,
        *,
        width: Any | None,
        height: Any | None,
    ) -> None:
        await self.datasets.set_video_dimensions(item_id, width=width, height=height)

    async def record_video_probe_error(
        self, item_id: UUID, video_metadata: Mapping[str, Any]
    ) -> None:
        await self.datasets.record_video_probe_error(item_id, video_metadata)

    async def save_video_materialization(
        self,
        item_id: UUID,
        video_metadata: Mapping[str, Any],
        *,
        thumbnail_path: str | None,
    ) -> None:
        await self.datasets.save_video_materialization(
            item_id, video_metadata, thumbnail_path=thumbnail_path
        )

    async def set_task_thumbnail(
        self, task_id: UUID, thumbnail_path: str, blurhash: str
    ) -> None:
        await self.labeling_tasks.set_task_thumbnail(task_id, thumbnail_path, blurhash)
