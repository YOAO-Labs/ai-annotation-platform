"""Associate recognized frozen versions without modifying their frozen inputs.

Run from apps/api with its existing virtual environment. Choose exactly one
target (--project-id UUID or --all-projects) and one mode (--dry-run, --apply,
--verify). Output is JSON Lines containing management IDs/counts only.
Dry-run and verify use database-enforced read-only transactions. Apply commits
bounded batches; interrupted runs resume by simply running the same command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent.parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from sqlalchemy import func, select, text, tuple_  # noqa: E402
from sqlalchemy.orm import defer  # noqa: E402

from app.db.models.project import Project  # noqa: E402
from app.db.models.project_task_dataset import (  # noqa: E402
    ProjectTaskDataset,
    TaskDatasetVersionMembership,
)
from app.db.models.training_dataset import TrainingDatasetVersion  # noqa: E402
from app.services.project_task_dataset import (  # noqa: E402
    ensure_membership,
    lock_project,
    recognized_task_expression,
)


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--project-id", type=uuid.UUID)
    target.add_argument("--all-projects", action="store_true")
    mode = command.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run")
    mode.add_argument("--apply", dest="mode", action="store_const", const="apply")
    mode.add_argument("--verify", dest="mode", action="store_const", const="verify")
    command.add_argument(
        "--batch-size", type=int, default=25, choices=range(1, 101), metavar="1..100"
    )
    return command


async def backfill_batch(db, project_id, *, mode, batch_size=25, after=None):
    """One bounded transaction; the caller commits only apply mode."""
    if mode not in {"dry-run", "apply", "verify"}:
        raise ValueError("unknown backfill mode")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if mode == "apply":
        await lock_project(db, project_id)
    stmt = (
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
        stmt = stmt.where(
            tuple_(TrainingDatasetVersion.created_at, TrainingDatasetVersion.id) > after
        )
    rows = (
        await db.execute(
            stmt.order_by(
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
    """Deleted numbers remain issued; never reduce the saved counter."""
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


async def run(args, session_factory, emit):
    totals = {
        key: 0
        for key in (
            "scanned",
            "associated",
            "already_associated",
            "pending",
            "unrecognized",
            "invalid_associations",
        )
    }
    high_water_errors = 0
    last_project = None
    while True:
        async with session_factory() as db:
            if args.mode != "apply":
                await db.execute(text("SET TRANSACTION READ ONLY"))
            project_query = select(Project.id).order_by(Project.id).limit(100)
            if args.project_id:
                project_query = project_query.where(Project.id == args.project_id)
            elif last_project:
                project_query = project_query.where(Project.id > last_project)
            project_ids = list(await db.scalars(project_query))
        if args.project_id and not project_ids:
            raise ValueError("Selected project does not exist")
        if not project_ids:
            break
        for project_id in project_ids:
            after = None
            while True:
                async with session_factory() as db:
                    if args.mode != "apply":
                        await db.execute(text("SET TRANSACTION READ ONLY"))
                    report, after = await backfill_batch(
                        db,
                        project_id,
                        mode=args.mode,
                        batch_size=args.batch_size,
                        after=after,
                    )
                    if args.mode == "apply":
                        await db.commit()
                    for key in totals:
                        totals[key] += report[key]
                    if report["scanned"]:
                        emit(
                            {
                                "type": "batch",
                                "mode": args.mode,
                                "project_id": str(project_id),
                                **report,
                            }
                        )
                if report["scanned"] < args.batch_size:
                    break
            async with session_factory() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                findings = await verify_high_water(db, project_id)
                high_water_errors += len(findings)
                if findings:
                    emit(
                        {
                            "type": "integrity",
                            "project_id": str(project_id),
                            "findings": findings,
                        }
                    )
        last_project = project_ids[-1]
        if args.project_id:
            break
    emit(
        {
            "type": "summary",
            "mode": args.mode,
            **totals,
            "high_water_errors": high_water_errors,
        }
    )
    failed = (
        totals["invalid_associations"]
        or high_water_errors
        or (args.mode == "verify" and totals["pending"])
    )
    return 1 if failed else 0


async def main(args):
    from app.db.base import async_session, engine

    try:
        return await run(
            args, async_session, lambda event: print(json.dumps(event, sort_keys=True))
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    arguments = parser().parse_args()
    try:
        result = asyncio.run(main(arguments))
    except Exception as error:
        # Driver error strings may include connection credentials or SQL bodies.
        print(
            json.dumps(
                {
                    "type": "error",
                    "error_type": type(error).__name__,
                    "reason": "Backfill did not finish; completed apply batches remain resumable.",
                }
            ),
            file=sys.stderr,
        )
        result = 2
    raise SystemExit(result)
