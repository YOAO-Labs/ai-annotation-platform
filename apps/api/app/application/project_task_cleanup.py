from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import FromClause

from app.db.models.task import Task
from app.services.active_learning_references import guard_source
from app.services.bug_report import BugReportService
from app.services.labeling_tasks import LabelingTaskService


_ORPHAN_TASK_IDS_SQL = """
    SELECT id FROM tasks WHERE project_id = :pid AND id NOT IN (
        SELECT t.id FROM tasks t
        JOIN dataset_items di ON di.id = t.dataset_item_id
        JOIN project_datasets pd ON pd.dataset_id = di.dataset_id AND pd.project_id = t.project_id
        WHERE t.project_id = :pid
    )
"""


def _orphan_task_ids_query(project_id: uuid.UUID) -> FromClause:
    return (
        text(_ORPHAN_TASK_IDS_SQL)
        .bindparams(pid=project_id)
        .columns(id=Task.id.type)
        .subquery("orphan_task_ids")
    )


async def delete_source_less_tasks(
    db: AsyncSession, project_id: uuid.UUID
) -> dict[str, int]:
    params = {"pid": str(project_id)}
    rows = (await db.execute(text(_ORPHAN_TASK_IDS_SQL), params)).all()
    if not rows:
        return {"deleted_tasks": 0, "deleted_annotations": 0}

    task_ids = [row[0] for row in rows]
    orphan_task_ids = _orphan_task_ids_query(project_id)
    await db.execute(
        select(Task.id).where(Task.id.in_(task_ids)).order_by(Task.id).with_for_update()
    )
    await guard_source(db, task_ids=task_ids)

    labeling = LabelingTaskService(db)
    annotation_count = await labeling.delete_source_less_task_children(orphan_task_ids)
    await BugReportService(db).detach_task_references(orphan_task_ids)
    await labeling.delete_source_less_task_records(orphan_task_ids)
    await labeling.recalculate_source_less_task_counters(project_id)
    return {
        "deleted_tasks": len(task_ids),
        "deleted_annotations": annotation_count,
    }
