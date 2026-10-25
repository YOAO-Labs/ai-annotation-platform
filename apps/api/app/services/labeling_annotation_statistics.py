"""Recompute Annotation-derived Task projections in the caller's transaction."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm.util import identity_key
from sqlalchemy.orm.attributes import set_committed_value

from aap_labeling.annotation_statistics_contract import (
    AnnotationStatisticsUnavailable,
)
from aap_labeling.annotation_statistics_data import LabelingAnnotationStatisticsData
from app.db.models.task import Task


async def update_annotation_statistics(
    db,
    task_id: uuid.UUID,
    *,
    trigger_batch_transitions: bool = True,
) -> None:
    """Retain the legacy Task and Batch flush order without API-owned writes."""

    if db.autoflush:
        await db.flush()

    async def execute(request_json: str):
        return await db.scalar(
            text(
                "SELECT aap_labeling_v1.annotation_statistics_command("
                "CAST(:request AS jsonb))"
            ),
            {"request": request_json},
        )

    try:
        result = await LabelingAnnotationStatisticsData(execute).recompute_task(task_id)
    except AnnotationStatisticsUnavailable as exc:
        raise HTTPException(status_code=503, detail="标注统计服务暂不可用") from exc

    if result.task_found:
        task = db.identity_map.get(identity_key(Task, ident=(task_id,)))
        if task is not None:
            set_committed_value(task, "total_annotations", result.total_annotations)
            set_committed_value(task, "is_labeled", result.is_labeled)
            set_committed_value(task, "status", result.status)
            set_committed_value(task, "updated_at", result.updated_at)

    await db.flush()

    if trigger_batch_transitions and result.status_changed and result.batch_id:
        from app.services.batch import BatchService

        batch_service = BatchService(db)
        await batch_service.check_auto_transitions(result.batch_id)
        await batch_service.recalculate_counters(result.batch_id)
