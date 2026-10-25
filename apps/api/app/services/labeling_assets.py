"""Single-statement reads of labeling source facts and linked media assets."""

from __future__ import annotations

from typing import cast as typing_cast

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models.project import Project

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
from app.services.labeling_core_read_relations import (
    annotation_row,
    task_row,
)


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
                    func.count(annotation_row.id),
                    "selected",
                    func.jsonb_agg(
                        func.jsonb_build_object(
                            "id",
                            cast(annotation_row.id, String),
                            "version",
                            annotation_row.version,
                            "class_name",
                            annotation_row.class_name,
                            "tool_unit_id",
                            annotation_row.tool_unit_id,
                            "geometry",
                            annotation_row.geometry,
                        )
                    ).filter(annotation_row.tool_unit_id == tool_unit_id),
                )
            )
            .where(
                annotation_row.task_id == task_row.id,
                annotation_row.is_active.is_(True),
                annotation_row.was_cancelled.is_(False),
            )
            .correlate(task_row)
            .scalar_subquery()
        )
        project = aliased(Project, project_facts(), adapt_on_names=True)
        stmt = (
            select(
                task_row,
                project.tool_bindings,
                annotation_json,
            )
            .join(project, project.id == task_row.project_id)
            .where(task_row.project_id == project_id)
            .order_by(task_row.id)
            .limit(limit)
        )
        if batch_id:
            stmt = stmt.where(task_row.batch_id == batch_id)
        if task_ids:
            stmt = stmt.where(task_row.id.in_(task_ids))

        rows = await db.stream(
            stmt.execution_options(populate_existing=True, yield_per=128)
        )
        source_rows = []
        tool_bindings: JsonObject | None = None
        first_row = True
        try:
            async for row in rows:
                task, raw_bindings, annotation_summary = row
                if first_row:
                    tool_bindings = typing_cast(
                        JsonObject, freeze_source_json(raw_bindings)
                    )
                    first_row = False
                source_rows.append((task, annotation_summary))
        finally:
            await rows.close()

        item_facts, _ = await read_spatial_item_facts(
            db,
            [
                task.dataset_item_id
                for task, _ in source_rows
                if task.dataset_item_id is not None
            ],
        )
        task_facts = []
        for task, annotation_summary in source_rows:
            item_id = task.dataset_item_id
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
                    width=item.width if item else None,
                    height=item.height if item else None,
                    annotations=annotations,
                )
            )
        return ProjectTaskSourceFacts(tool_bindings, tuple(task_facts))
