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

from app.application.task_dataset_backfill import (  # noqa: E402
    backfill_batch,
    list_project_ids,
    verify_high_water,
)

from sqlalchemy import text  # noqa: E402


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
            project_ids = await list_project_ids(
                db, project_id=args.project_id, after=last_project
            )
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
