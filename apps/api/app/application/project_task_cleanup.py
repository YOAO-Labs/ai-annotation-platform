from __future__ import annotations

import uuid

from sqlalchemy import bindparam, func, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import FromClause

from app.application.project_workspace import lock_managed_project
from app.db.models.project import Project
from app.services.active_learning_references import guard_source
from app.services.annotation import AnnotationService
from app.services.bug_report import BugReportService
from app.services.labeling_tasks import LabelingTaskService
from app.services.labeling_task_maintenance import LabelingTaskMaintenanceService
from aap_labeling.task_maintenance_contract import (
    TASK_MAINTENANCE_PROTOCOL,
    LockSourceLessTasks,
    ReadSourceLessTaskCandidates,
)
from aap_workspace_contract import derive_attribute_keys, derive_classes_list


def _task_ids_query(task_ids: list[uuid.UUID]) -> FromClause:
    return select(
        func.unnest(
            bindparam(
                "task_ids",
                value=task_ids,
                type_=ARRAY(PGUUID(as_uuid=True)),
            )
        ).label("id")
    ).subquery("orphan_task_ids")


async def delete_source_less_tasks(
    db: AsyncSession, project_id: uuid.UUID
) -> dict[str, int]:
    maintenance = LabelingTaskMaintenanceService(db)
    candidates = await maintenance.command(
        ReadSourceLessTaskCandidates(
            schema=TASK_MAINTENANCE_PROTOCOL,
            operation="read_source_less_task_candidates",
            project_id=project_id,
        )
    )
    if not candidates.task_ids:
        return {"deleted_tasks": 0, "deleted_annotations": 0}

    locked = await maintenance.command(
        LockSourceLessTasks(
            schema=TASK_MAINTENANCE_PROTOCOL,
            operation="lock_source_less_tasks",
            project_id=project_id,
            task_ids=candidates.task_ids,
        ),
        flush_before=True,
    )
    task_ids = locked.task_ids
    if not task_ids:
        return {"deleted_tasks": 0, "deleted_annotations": 0}

    await guard_source(db, task_ids=task_ids)
    task_id_relation = _task_ids_query(task_ids)

    labeling = LabelingTaskService(db)
    annotation_count = await labeling.delete_source_less_task_children(
        task_ids, project_id=project_id
    )
    await BugReportService(db).detach_task_references(task_id_relation)
    await labeling.delete_source_less_task_records(project_id, task_ids)
    await labeling.recalculate_source_less_task_counters(project_id)
    return {
        "deleted_tasks": len(task_ids),
        "deleted_annotations": annotation_count,
    }


async def cleanup_annotation_orphans(
    db: AsyncSession,
    *,
    project: Project,
    actor_id: uuid.UUID,
    dry_run: bool,
) -> dict[str, int | dict[str, int]]:
    current_project = (
        project
        if dry_run
        else await lock_managed_project(
            db,
            actor_id=actor_id,
            project_id=project.id,
        )
    )

    return await AnnotationService(db).cleanup_schema_orphan_annotations(
        project_id=current_project.id,
        class_names=set(derive_classes_list(current_project.tool_bindings)),
        attribute_keys=derive_attribute_keys(current_project.tool_bindings),
        actor_id=actor_id,
        dry_run=dry_run,
    )
