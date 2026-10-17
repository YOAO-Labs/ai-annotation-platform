from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.services.active_learning_references import guard_source
from app.services.dataset import DatasetDeleteCleanup, DatasetService
from app.services.labeling_tasks import LabelingTaskService


async def delete_dataset(
    db: AsyncSession, dataset_id: uuid.UUID
) -> DatasetDeleteCleanup | None:
    assets = DatasetService(db)
    dataset = await assets.get(dataset_id)
    if dataset is None:
        return None

    await guard_source(db, dataset_id=dataset_id)
    return await assets.delete_dataset_record(dataset)


async def unlink_dataset_from_project(
    db: AsyncSession, dataset_id: uuid.UUID, project_id: uuid.UUID
) -> dict | None:
    target_rows = (
        await db.execute(
            select(Task.id, Task.batch_id)
            .join(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .where(
                Task.project_id == project_id,
                DatasetItem.dataset_id == dataset_id,
            )
            .order_by(Task.id)
            .with_for_update(of=Task)
        )
    ).all()
    task_ids = [row[0] for row in target_rows]
    affected_batch_ids = {row[1] for row in target_rows if row[1] is not None}

    await guard_source(db, task_ids=task_ids)
    labeling = LabelingTaskService(db)
    deleted = await labeling.delete_unlinked_tasks(task_ids)

    assets = DatasetService(db)
    if not await assets.remove_project_association(dataset_id, project_id):
        return None

    deleted_batch_ids = await labeling.recalculate_unlink_counters(
        project_id, affected_batch_ids
    )
    await db.flush()
    return {
        **deleted,
        "deleted_batches": len(deleted_batch_ids),
        "deleted_batch_ids": [str(batch_id) for batch_id in deleted_batch_ids],
        "soft": False,
    }
