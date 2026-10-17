"""Annotation-owned row cleanup shared by local labeling workflows."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Select

from app.db.models.annotation import Annotation


async def detach_prediction_parents_for_tasks(
    db: AsyncSession, task_id_select: Select
) -> None:
    """Detach prediction references without changing annotation content or versions."""
    await db.execute(
        update(Annotation)
        .where(Annotation.task_id.in_(task_id_select))
        .values(parent_prediction_id=None)
    )


async def deactivate_prediction_annotations_for_tasks(
    db: AsyncSession, task_ids: Sequence[uuid.UUID]
) -> int:
    """Soft-delete accepted AI rows after the caller locks tasks and records evidence."""
    result = await db.execute(
        update(Annotation)
        .where(
            Annotation.task_id.in_(task_ids),
            Annotation.source == "prediction_based",
            Annotation.is_active.is_(True),
        )
        .values(is_active=False)
    )
    return result.rowcount or 0
