from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.dataset import DatasetService, ResetDatasetSummary
from app.services.labeling_tasks import LabelingTaskService


class DatasetResetWorkflow:
    """Detach task sources and clear database assets without deleting stored objects."""

    def __init__(self, db: AsyncSession) -> None:
        self.assets = DatasetService(db)
        self.labeling = LabelingTaskService(db)

    async def list_datasets(self) -> tuple[ResetDatasetSummary, ...]:
        return await self.assets.list_for_reset()

    async def reset(self) -> None:
        await self.labeling.clear_dataset_item_sources()
        await self.assets.reset_all()
