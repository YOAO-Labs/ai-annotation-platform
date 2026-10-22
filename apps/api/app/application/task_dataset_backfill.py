"""Application commands for associating recognized training versions with task datasets."""

from __future__ import annotations

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import aliased, defer

from app.db.models.project import Project
from app.db.models.project_task_dataset import (
    ProjectTaskDataset,
    TaskDatasetVersionMembership,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services.project_task_dataset import (
    ensure_membership,
    lock_project,
    recognized_task_expression,
)
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

    statement = (
        select(
            TrainingDatasetVersion,
            recognized_task_expression().label("recognized_task"),
            TaskDatasetVersionMembership,
            ProjectTaskDataset,
        )
        .outerjoin(
            TaskDatasetVersionMembership,
            TaskDatasetVersionMembership.version_id == TrainingDatasetVersion.id,
        )
        .outerjoin(
            ProjectTaskDataset,
            ProjectTaskDataset.id == TaskDatasetVersionMembership.task_dataset_id,
        )
        .options(
            defer(TrainingDatasetVersion.snapshot),
            defer(TrainingDatasetVersion.manifest),
            defer(TrainingDatasetVersion.preparation_report),
            defer(TrainingDatasetVersion.class_count),
        )
        .where(TrainingDatasetVersion.project_id == project_id)
    )
    if after is not None:
        statement = statement.where(
            tuple_(TrainingDatasetVersion.created_at, TrainingDatasetVersion.id) > after
        )
    rows = (
        await db.execute(
            statement.order_by(
                TrainingDatasetVersion.created_at, TrainingDatasetVersion.id
            ).limit(batch_size)
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
    for version, task, membership, stream in rows:
        if task is None:
            report["unrecognized"] += 1
            report["findings"].append(
                {
                    "version_id": str(version.id),
                    "code": "unknown_data_contract",
                    "reason": "Frozen task/geometry/bridge contract is not recognized; unchanged.",
                }
            )
            if membership:
                report["invalid_associations"] += 1
            continue
        if membership:
            if stream.project_id != version.project_id or stream.task != task:
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
            await db.refresh(version, attribute_names=["snapshot"])
            membership = await ensure_membership(db, version, project_locked=True)
            db.expire(version, ["snapshot"])
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
    cursor = (rows[-1][0].created_at, rows[-1][0].id) if rows else after
    return report, cursor


async def verify_high_water(db, project_id):
    """Report invalid counters without repairing them or reducing issued ordinals."""
    rows = (
        await db.execute(
            select(
                ProjectTaskDataset.id,
                ProjectTaskDataset.next_ordinal,
                func.max(TaskDatasetVersionMembership.ordinal),
            )
            .outerjoin(
                TaskDatasetVersionMembership,
                TaskDatasetVersionMembership.task_dataset_id == ProjectTaskDataset.id,
            )
            .where(ProjectTaskDataset.project_id == project_id)
            .group_by(ProjectTaskDataset.id)
        )
    ).all()
    return [
        {
            "task_dataset_id": str(stream_id),
            "code": "task_dataset_high_water_invalid",
            "reason": "next_ordinal must exceed every retained ordinal; unchanged.",
        }
        for stream_id, next_ordinal, maximum in rows
        if next_ordinal <= (maximum or 0)
    ]
