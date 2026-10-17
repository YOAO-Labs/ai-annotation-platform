"""Labeling-owned SQL reader for current sample-evaluation source facts."""

from __future__ import annotations

from typing import cast

from sqlalchemy import String, cast as sql_cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.services.labeling_evaluation_source_contract import (
    CurrentTaskSourceAnnotation,
    CurrentTaskSourceRead,
    CurrentTaskSourceReader,
    CurrentTaskSourceSnapshot,
)
from app.services.storage import storage_service as storage


class SqlAlchemyLabelingEvaluationSourceReader(CurrentTaskSourceReader):
    """Bind the read contract to the caller's existing transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read_current_task_source(
        self, request: CurrentTaskSourceRead
    ) -> CurrentTaskSourceSnapshot | None:
        limited = (
            select(
                Annotation.id,
                Annotation.version,
                Annotation.class_name,
                Annotation.tool_unit_id,
                func.jsonb_build_object(
                    *[
                        item
                        for key in ("type", "x", "y", "w", "h")
                        for item in (key, Annotation.geometry[key])
                    ]
                ).label("geometry"),
            )
            .where(
                Annotation.task_id == Task.id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
            )
            .order_by(Annotation.id)
            .limit(request.annotation_limit)
            .correlate(Task)
            .subquery()
        )
        annotations = (
            select(
                func.jsonb_agg(
                    func.jsonb_build_object(
                        "id",
                        sql_cast(limited.c.id, String),
                        "version",
                        limited.c.version,
                        "class_name",
                        limited.c.class_name,
                        "tool_unit_id",
                        limited.c.tool_unit_id,
                        "geometry",
                        limited.c.geometry,
                    )
                )
            )
            .select_from(limited)
            .scalar_subquery()
        )
        result = (
            await self._db.execute(
                select(Task, DatasetItem, annotations)
                .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
                .where(
                    Task.id == request.task_id,
                    Task.project_id == request.project_id,
                )
                .execution_options(populate_existing=True)
            )
        ).first()
        if not result:
            return None
        task, item, labels = result
        source: CurrentTaskSourceSnapshot = {
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "batch_id": str(task.batch_id) if task.batch_id else None,
            "dataset_item_id": str(task.dataset_item_id)
            if task.dataset_item_id
            else None,
            "source_bucket": storage.datasets_bucket
            if task.dataset_item_id
            else storage.bucket,
            "source_key": task.file_path,
            "file_type": task.file_type,
            "version": task.version,
            "status": task.status,
            "submitted_at": task.submitted_at.isoformat()
            if task.submitted_at
            else None,
            "skipped_at": task.skipped_at.isoformat() if task.skipped_at else None,
            "skip_reason": task.skip_reason,
            "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
            "expected_width": item.width if item else None,
            "expected_height": item.height if item else None,
            "annotations": sorted(
                cast(list[CurrentTaskSourceAnnotation], labels or []),
                key=lambda annotation: annotation["id"],
            ),
        }
        return source
