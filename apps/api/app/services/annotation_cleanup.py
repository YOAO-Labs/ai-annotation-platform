"""Run fixed Labeling prediction-cleanup commands in the caller's transaction."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.batch_control_contract import (
    DeactivatePredictionAnnotations,
    DetachPredictionParents,
)
from app.db.models.annotation import Annotation
from app.services.batch import execute_batch_control


def _tracked_annotation_ids(
    db: AsyncSession, task_ids: Sequence[uuid.UUID] | None
) -> list[uuid.UUID]:
    task_filter = set(task_ids or ())
    return sorted(
        {
            annotation.id
            for annotation in db.identity_map.values()
            if isinstance(annotation, Annotation)
            and (not task_filter or annotation.task_id in task_filter)
        },
        key=str,
    )


async def detach_prediction_parents_for_tasks(
    db: AsyncSession,
    *,
    task_ids: Sequence[uuid.UUID] | None = None,
    batch_id: uuid.UUID | None = None,
) -> None:
    """Detach soft prediction references as a separate trigger-visible UPDATE."""
    if (task_ids is None) == (batch_id is None):
        raise ValueError("exactly one prediction-detach scope is required")
    await execute_batch_control(
        db,
        DetachPredictionParents(
            schema="aap-labeling-batch-control/v1",
            operation="detach_prediction_parents",
            task_ids=list(dict.fromkeys(task_ids)) if task_ids is not None else None,
            batch_id=batch_id,
            tracked_annotation_ids=_tracked_annotation_ids(db, task_ids),
        ),
    )


async def deactivate_prediction_annotations_for_tasks(
    db: AsyncSession, task_ids: Sequence[uuid.UUID]
) -> int:
    """Soft-delete accepted AI rows after the caller locks tasks and records evidence."""
    result = await execute_batch_control(
        db,
        DeactivatePredictionAnnotations(
            schema="aap-labeling-batch-control/v1",
            operation="deactivate_prediction_annotations",
            task_ids=list(task_ids),
            tracked_annotation_ids=_tracked_annotation_ids(db, task_ids),
        ),
    )
    return result.ai_annotations_deactivated
