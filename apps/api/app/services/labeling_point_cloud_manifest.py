"""SQL adapter for labeling-owned point-cloud task link references."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.labeling_point_cloud_manifest_contract import (
    TaskDatasetItemLinkFact,
    TaskDatasetItemLinksReader,
)
from app.services.task_dataset_link import get_linked_items


class SqlAlchemyTaskDatasetItemLinksReader(TaskDatasetItemLinksReader):
    """Read current links in the caller-owned transaction without leaking rows."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read_task_dataset_item_links(
        self, task_id: UUID
    ) -> tuple[TaskDatasetItemLinkFact, ...]:
        links = await get_linked_items(self._db, task_id)
        return tuple(
            TaskDatasetItemLinkFact(
                dataset_item_id=link.dataset_item_id,
                role=link.role,
            )
            for link in links
        )
