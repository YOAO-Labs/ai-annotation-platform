"""Single-statement reads of labeling source facts and linked media assets."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import cast as typing_cast

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task

from app.services.dataset_task_facts import (
    AnnotationSourceFact,
    JsonObject,
    ProjectTaskSourceFacts,
    TaskLabelingSourceFact,
    freeze_source_json,
)


async def read_project_task_source_facts(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    tool_unit_id: str,
    batch_id: uuid.UUID | None = None,
    task_ids: Sequence[uuid.UUID] = (),
    limit: int,
) -> ProjectTaskSourceFacts:
    """Freeze production qualifications and scoped labels in one SQL snapshot."""
    annotation_json = (
        select(
            func.jsonb_build_object(
                "count",
                func.count(Annotation.id),
                "selected",
                func.jsonb_agg(
                    func.jsonb_build_object(
                        "id",
                        cast(Annotation.id, String),
                        "version",
                        Annotation.version,
                        "class_name",
                        Annotation.class_name,
                        "tool_unit_id",
                        Annotation.tool_unit_id,
                        "geometry",
                        Annotation.geometry,
                    )
                ).filter(Annotation.tool_unit_id == tool_unit_id),
            )
        )
        .where(
            Annotation.task_id == Task.id,
            Annotation.is_active.is_(True),
            Annotation.was_cancelled.is_(False),
        )
        .correlate(Task)
        .scalar_subquery()
    )
    stmt = (
        select(Task, DatasetItem, Project.tool_bindings, annotation_json)
        .join(Project, Project.id == Task.project_id)
        .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
        .where(Task.project_id == project_id)
        .order_by(Task.id)
        .limit(limit)
    )
    if batch_id:
        stmt = stmt.where(Task.batch_id == batch_id)
    if task_ids:
        stmt = stmt.where(Task.id.in_(task_ids))

    rows = await db.stream(
        stmt.execution_options(populate_existing=True, yield_per=128)
    )
    task_facts = []
    tool_bindings: JsonObject | None = None
    first_row = True
    try:
        async for row in rows:
            task, item, raw_bindings, annotation_summary = row
            if first_row:
                tool_bindings = typing_cast(
                    JsonObject, freeze_source_json(raw_bindings)
                )
                first_row = False
            raw_annotations = annotation_summary["selected"] or ()
            annotation_count = annotation_summary["count"]
            annotations = tuple(
                AnnotationSourceFact(
                    id=annotation["id"],
                    version=annotation["version"],
                    class_name=annotation["class_name"],
                    tool_unit_id=annotation["tool_unit_id"],
                    geometry=freeze_source_json(annotation["geometry"]),
                )
                for annotation in raw_annotations
            )
            current_skip = (
                task.skipped_at is not None and task.skipped_at == task.submitted_at
            )
            reviewed = task.status == "completed" and task.reviewed_at is not None
            task_facts.append(
                TaskLabelingSourceFact(
                    task_id=task.id,
                    task_version=task.version,
                    file_type=task.file_type,
                    reviewed=reviewed,
                    unusable=current_skip
                    and task.skip_reason in {"image_corrupt", "unclear", "other"},
                    reviewed_negative=reviewed
                    and current_skip
                    and task.skip_reason == "no_target"
                    and annotation_count == 0,
                    excluded_annotation_count=annotation_count - len(annotations),
                    dataset_item_id=task.dataset_item_id,
                    file_path=task.file_path,
                    scene_id=item.scene_id if item else None,
                    width=item.width if item else None,
                    height=item.height if item else None,
                    annotations=annotations,
                )
            )
    finally:
        await rows.close()
    return ProjectTaskSourceFacts(tool_bindings, tuple(task_facts))
