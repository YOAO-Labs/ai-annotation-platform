"""Bind image and task thumbnail writes to their source owners."""

from __future__ import annotations

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

    async def set_task_thumbnail(
        self, task_id: UUID, thumbnail_path: str, blurhash: str | None
    ) -> None:
        await self.labeling_tasks.set_task_thumbnail(task_id, thumbnail_path, blurhash)
