"""Application commands for associating recognized training versions with task datasets."""

from __future__ import annotations

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import aliased

from app.db.models.project import Project
from app.services import training_dataset_data as data
from app.services.project_task_dataset import lock_project
from app.services.workspace_read_relations import project_facts


async def list_project_ids(db, *, project_id=None, after=None):
    project = aliased(Project, project_facts(), adapt_on_names=True)
    statement = select(project.id).order_by(project.id).limit(100)
    if project_id is not None:
        statement = statement.where(project.id == project_id)
    elif after is not None:
        statement = statement.where(project.id > after)
    return list(await db.scalars(statement))


async def backfill_batch(db, project_id, *, mode, batch_size=25, after=None):
    """Inspect one bounded page; the CLI owns whether to commit the batch."""
    if mode not in {"dry-run", "apply", "verify"}:
        raise ValueError("unknown backfill mode")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if mode == "apply":
        await lock_project(db, project_id)

    if db.autoflush:
        await db.flush()
    version = data.dataset_lineage_row
    metadata = version.c
    stream = data.stream_rows(project_id)
    statement = (
        select(
            metadata.id,
            metadata.created_at,
            metadata.recognized_task,
            metadata.task_dataset_id,
            stream.c.task,
        )
        .select_from(version.outerjoin(stream, stream.c.id == metadata.task_dataset_id))
        .where(metadata.project_id == project_id)
    )
    if after is not None:
        statement = statement.where(tuple_(metadata.created_at, metadata.id) > after)
    rows = (
        await db.execute(
            statement.order_by(metadata.created_at, metadata.id).limit(batch_size)
        )
    ).all()
    report = {
        "scanned": len(rows),
        "associated": 0,
        "already_associated": 0,
        "pending": 0,
        "unrecognized": 0,
        "invalid_associations": 0,
        "findings": [],
    }
    for version in rows:
        if version.recognized_task is None:
            report["unrecognized"] += 1
            report["findings"].append(
                {
                    "version_id": str(version.id),
                    "code": "unknown_data_contract",
                    "reason": "Frozen task/geometry/bridge contract is not recognized; unchanged.",
                }
            )
            if version.task_dataset_id:
                report["invalid_associations"] += 1
            continue
        if version.task_dataset_id:
            if version.task != version.recognized_task:
                report["invalid_associations"] += 1
                report["findings"].append(
                    {
                        "version_id": str(version.id),
                        "code": "task_dataset_membership_mismatch",
                        "reason": "Membership project/task differs from frozen contract; unchanged.",
                    }
                )
            else:
                report["already_associated"] += 1
            continue
        if mode == "apply":
            membership = await data.ensure_membership(db, project_id, version.id)
            report["associated"] += 1
            report["findings"].append(
                {
                    "version_id": str(version.id),
                    "task_dataset_id": str(membership.task_dataset_id),
                    "ordinal": membership.ordinal,
                    "code": "associated",
                }
            )
        else:
            report["pending"] += 1
            report["findings"].append(
                {
                    "version_id": str(version.id),
                    "code": "pending_membership",
                    "reason": "Recognized frozen version has no membership.",
                }
            )
    cursor = (rows[-1].created_at, rows[-1].id) if rows else after
    return report, cursor


async def verify_high_water(db, project_id):
    """Report invalid counters without repairing them or reducing issued ordinals."""
    source = func.yoao_learning_v1.training_dataset_high_water_violations(
        project_id
    ).table_valued("task_dataset_id")
    return [
        {
            "task_dataset_id": str(stream_id),
            "code": "task_dataset_high_water_invalid",
            "reason": "next_ordinal must exceed every retained ordinal; unchanged.",
        }
        for stream_id in await db.scalars(select(source.c.task_dataset_id))
    ]
