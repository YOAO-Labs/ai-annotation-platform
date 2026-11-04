#!/usr/bin/env python3
"""Verify a real screenshot export without bypassing Jobs or output retirement."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.config import settings  # noqa: E402
from app.db.models.async_job import AsyncJob  # noqa: E402
from app.db.models.export_artifact import ExportArtifact  # noqa: E402
from app.db.models.export_media_bundle import ExportMediaBundle  # noqa: E402
from app.db.models.export_output import ExportOutput  # noqa: E402
from scripts.screenshot_fixture_owner import (  # noqa: E402
    assert_screenshot_scope,
    close_screenshot_owner,
    screenshot_owner,
)


ASSET_ID = "background-export-download"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "cleanup"))
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--task-id", type=uuid.UUID, required=True)
    parser.add_argument("--job-id", type=uuid.UUID)
    parser.add_argument("--running-hold-seconds", type=float, default=6.0)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    run_id, owner_url = await screenshot_owner()
    engine = create_async_engine(owner_url, echo=False, hide_parameters=True)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    failure = None
    try:
        async with sessions() as db:
            await assert_screenshot_scope(
                db,
                run_id=run_id,
                project_key="image_demo",
                project_id=args.project_id,
                task_id=args.task_id,
            )
            if args.action == "run":
                if args.job_id is None:
                    raise RuntimeError("run requires --job-id")
                deadline = asyncio.get_running_loop().time() + max(
                    0.0, args.running_hold_seconds
                )
                while True:
                    job = await db.get(AsyncJob, args.job_id, populate_existing=True)
                    if (
                        job is None
                        or job.project_id != args.project_id
                        or job.kind != "export"
                    ):
                        raise RuntimeError(
                            "export job is outside the screenshot project"
                        )
                    targets = (job.payload or {}).get("targets")
                    if targets != ["coco", "native_json"]:
                        raise RuntimeError(
                            f"unexpected recording export targets: {targets!r}"
                        )
                    if job.status == "running":
                        return {
                            "asset_id": ASSET_ID,
                            "job_id": str(job.id),
                            "status": job.status,
                        }
                    if job.status != "pending":
                        raise RuntimeError(
                            f"export job {job.id} reached {job.status} before running was observed: "
                            f"{job.error_message}"
                        )
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"export job {job.id} did not start within the existing wait"
                        )
                    await asyncio.sleep(min(0.2, remaining))

            jobs = list(
                await db.scalars(
                    select(AsyncJob.id).where(
                        AsyncJob.project_id == args.project_id,
                        AsyncJob.kind == "export",
                    )
                )
            )
            artifact = await db.scalar(
                select(ExportArtifact.id)
                .where(ExportArtifact.project_id == args.project_id)
                .limit(1)
            )
            bundle = await db.scalar(
                select(ExportMediaBundle.id)
                .where(ExportMediaBundle.project_id == args.project_id)
                .limit(1)
            )
            output = await db.scalar(
                select(ExportOutput.id)
                .where(ExportOutput.project_id == args.project_id)
                .limit(1)
            )
            if jobs or artifact is not None or bundle is not None or output is not None:
                raise RuntimeError(
                    "export Jobs or output anchors remain; exact execution and retention "
                    "retirement is not proved, so cleanup is blocked"
                )
            return {"asset_id": ASSET_ID, "async_jobs": 0, "export_artifacts": 0}
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await close_screenshot_owner(engine, failure)


def main() -> int:
    if settings.environment == "production":
        print(
            "[background-export-download] refusing production database", file=sys.stderr
        )
        return 2
    print(json.dumps(asyncio.run(run(parse_args())), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
