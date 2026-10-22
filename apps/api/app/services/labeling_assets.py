"""Single-statement reads of labeling source facts and linked media assets."""

from __future__ import annotations

from typing import cast as typing_cast

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task

from app.services.dataset_task_facts import (
    AnnotationSourceFact,
    JsonObject,
    ProjectTaskSourceRead,
    ProjectTaskSourceReader,
    ProjectTaskSourceFacts,
    TaskLabelingSourceFact,
    freeze_source_json,
)
from app.services.labeling_scene import read_spatial_item_facts
from app.services.workspace_read_relations import project_facts


class SqlAlchemyProjectTaskSourceReader(ProjectTaskSourceReader):
    """Bind the labeling source-read port to the caller's existing transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read_project_task_source_facts(
        self, request: ProjectTaskSourceRead
    ) -> ProjectTaskSourceFacts:
        """Read one ordered project/batch snapshot without per-task queries."""
        db = self._db
        project_id = request.project_id
        tool_unit_id = request.tool_unit_id
        batch_id = request.batch_id
        task_ids = request.task_ids
        limit = request.limit
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
        project = aliased(Project, project_facts(), adapt_on_names=True)
        stmt = (
            select(
                Task,
                DatasetItem.id,
                DatasetItem.width,
                DatasetItem.height,
                project.tool_bindings,
                annotation_json,
            )
            .join(project, project.id == Task.project_id)
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
        source_rows = []
        tool_bindings: JsonObject | None = None
        first_row = True
        try:
            async for row in rows:
                task, item_id, width, height, raw_bindings, annotation_summary = row
                if first_row:
                    tool_bindings = typing_cast(
                        JsonObject, freeze_source_json(raw_bindings)
                    )
                    first_row = False
                source_rows.append((task, item_id, width, height, annotation_summary))
        finally:
            await rows.close()

        item_facts, _ = await read_spatial_item_facts(
            db, [item_id for _, item_id, *_ in source_rows if item_id is not None]
        )
        task_facts = []
        for task, item_id, width, height, annotation_summary in source_rows:
            item = item_facts.get(item_id) if item_id is not None else None
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
                    width=width,
                    height=height,
                    annotations=annotations,
                )
            )
        return ProjectTaskSourceFacts(tool_bindings, tuple(task_facts))
