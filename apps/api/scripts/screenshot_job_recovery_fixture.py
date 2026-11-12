#!/usr/bin/env python3
"""Create or remove the isolated jobs-retry-recovery recording fixture.

The helper is fail-closed to the screenshot-managed OCR project.  It marks every
row it owns, derives retry jobs and predictions from the original failed id, and
never scans or mutates user projects.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.config import settings  # noqa: E402
from app.db.models.async_job import AsyncJob, AsyncJobStatus  # noqa: E402
from app.db.models.fixture_run import (  # noqa: E402
    FixturePresentationJob,
    FixtureRun,
    FixtureRunResource,
)
from app.db.models.ml_backend_registry import MLBackendRegistry  # noqa: E402
from app.db.models.notification import Notification  # noqa: E402
from app.services.project_access import get_workspace_project  # noqa: E402
from app.services.identity_commands import IdentityCommandService  # noqa: E402
from app.services.prediction import PredictionService  # noqa: E402
from scripts.screenshot_fixture_owner import (  # noqa: E402
    assert_screenshot_scope,
    close_screenshot_owner,
    screenshot_owner,
)
from yoao_labeling.screenshot_operations_contract import (  # noqa: E402
    CleanupRecoveryRows,
    SCREENSHOT_OPERATIONS_PROTOCOL,
)
from yoao_labeling.prediction_record_contract import (  # noqa: E402
    DeletePredictionData,
)
from app.services.labeling_core_read_relations import (  # noqa: E402
    execute_core_read,
    failed_prediction_row,
    get_core_row,
    task_row,
)
from app.services.labeling_screenshot_operations import (  # noqa: E402
    screenshot_operations_command,
)
from app.services.labeling_prediction_records import (  # noqa: E402
    sync_deleted_prediction_records,
)


ASSET_ID = "jobs-retry-recovery"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "cleanup"))
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--task-id", type=uuid.UUID, required=True)
    parser.add_argument("--user-email")
    return parser.parse_args()


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def _owned_rows(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[list[AsyncJob], set[uuid.UUID]]:
    original_jobs = list(
        (
            await db.execute(
                select(AsyncJob).where(
                    AsyncJob.project_id == project_id,
                    AsyncJob.payload["recording_asset_id"].astext == ASSET_ID,
                )
            )
        ).scalars()
    )
    failed_ids = {
        failed_id
        for job in original_jobs
        for value in (job.result or {}).get("failed_prediction_ids", [])
        if (failed_id := _uuid(value)) is not None
    }
    marked_failed = list(
        (
            await execute_core_read(
                db,
                select(failed_prediction_row.id).where(
                    failed_prediction_row.project_id == project_id,
                    failed_prediction_row.task_id == task_id,
                    failed_prediction_row.extra["recording_asset_id"].astext
                    == ASSET_ID,
                ),
            )
        ).scalars()
    )
    failed_ids.update(marked_failed)

    return original_jobs, failed_ids


async def cleanup_owned(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
) -> dict[str, int]:
    run = await db.scalar(
        select(FixtureRun).where(FixtureRun.id == run_id).with_for_update(nowait=True)
    )
    if run is None or run.family != "screenshot" or run.state != "open":
        raise RuntimeError("screenshot run is not open")
    jobs, failed_ids = await _owned_rows(
        db,
        project_id=project_id,
        task_id=task_id,
    )
    job_ids = {job.id for job in jobs}
    await db.execute(
        text(
            "SELECT aap_labeling_v1.guard_fixture_presentation_failures("
            "CAST(:run AS uuid), CAST(:project AS uuid), CAST(:failures AS uuid[]))"
        ),
        {"run": run_id, "project": project_id, "failures": sorted(failed_ids, key=str)},
    )
    job_count = await db.scalar(
        text(
            "SELECT aap_jobs_v1.delete_fixture_presentation_jobs("
            "CAST(:run AS uuid), CAST(:project AS uuid), CAST(:jobs AS uuid[]))"
        ),
        {"run": run_id, "project": project_id, "jobs": sorted(job_ids, key=str)},
    )
    failed_count = 0
    remaining_predictions = None
    owner_calls = 0

    notification_targets = [*job_ids, *failed_ids]
    if notification_targets:
        notification_result = await db.execute(
            delete(Notification).where(Notification.target_id.in_(notification_targets))
        )
    else:
        notification_result = None
    if failed_ids:
        result = await screenshot_operations_command(
            db,
            CleanupRecoveryRows(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="cleanup_recovery_rows",
                run_id=run_id,
                project_id=project_id,
                task_id=task_id,
                failed_prediction_ids=tuple(sorted(failed_ids, key=str)),
                prediction_ids=(),
            ),
        )
        failed_count = result.counts["failed_predictions"]
        remaining_predictions = result.counts["remaining_predictions"]
        sync_deleted_prediction_records(
            db,
            DeletePredictionData(
                operation="delete_for_tasks",
                predictions=0,
                failed_predictions=failed_count,
                deleted_predictions=[],
                deleted_failed_prediction_ids=list(failed_ids),
                deleted_prediction_meta_ids=[],
            ),
        )
        owner_calls += 1

    if owner_calls == 0:
        result = await screenshot_operations_command(
            db,
            CleanupRecoveryRows(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="cleanup_recovery_rows",
                run_id=run_id,
                project_id=project_id,
                task_id=task_id,
                failed_prediction_ids=(),
                prediction_ids=(),
            ),
        )
        remaining_predictions = result.counts["remaining_predictions"]
    task = await get_core_row(db, task_row, task_id, populate_existing=True)
    if task is None or task.project_id != project_id:
        raise RuntimeError("cleanup task disappeared from screenshot project")
    set_committed_value(task, "total_predictions", int(remaining_predictions or 0))
    await db.flush()
    return {
        "async_jobs": job_count,
        "failed_predictions": failed_count,
        "notifications": notification_result.rowcount
        if notification_result is not None
        else 0,
        "predictions": 0,
    }


async def seed(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    user_email: str,
) -> dict[str, str]:
    await cleanup_owned(db, run_id=run_id, project_id=project_id, task_id=task_id)
    task = await get_core_row(db, task_row, task_id)
    project = await get_workspace_project(db, project_id=project_id)
    if task is None or project is None or task.project_id != project.id:
        raise RuntimeError("screenshot OCR task or project disappeared")
    user_id = await IdentityCommandService(db).lookup_seed_account_id(user_email)
    if user_id is None:
        raise RuntimeError(f"recording user not found: {user_email}")
    user_owner = await db.get(FixtureRunResource, (run_id, "user", user_id))
    if user_owner is None or user_owner.state != "published":
        raise RuntimeError("recording user is outside the screenshot run")

    stages = project.preannotate_pipeline or []
    stage = stages[0] if stages and isinstance(stages[0], dict) else None
    backend_id = _uuid((stage or {}).get("ml_backend_id"))
    if backend_id is None:
        raise RuntimeError("OCR screenshot project has no default backend stage")
    backend = await db.get(MLBackendRegistry, backend_id)
    if backend is None or "rapidocr" not in backend.name.casefold():
        raise RuntimeError("jobs-retry-recovery requires the real RapidOCR backend")

    request_context = {
        "type": str((stage or {}).get("task_type") or "ocr"),
        "model_id": str((stage or {}).get("model_id") or "ocr-e2e"),
    }
    failed = await PredictionService(db).create_failed(
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=backend.id,
        error_type="BackendTimeout",
        message="RapidOCR 推理节点短暂超时，服务已恢复",
        extra={
            "recording_asset_id": ASSET_ID,
            "request_context": request_context,
        },
    )
    now = datetime.now(timezone.utc)
    job_id = uuid.uuid4()
    db.add(
        FixturePresentationJob(
            job_id=job_id,
            run_id=run_id,
            project_id=project_id,
            kind="batch_predict",
            state="admitted",
        )
    )
    await db.flush()
    job = AsyncJob(
        id=job_id,
        kind="batch_predict",
        project_id=project_id,
        user_id=user_id,
        status=AsyncJobStatus.FAILED.value,
        progress_pct=100,
        payload={
            "recording_asset_id": ASSET_ID,
            "task_id": str(task_id),
            "task_display_id": task.display_id,
            "project_display_id": project.display_id,
            "project_name": project.name,
            "ml_backend_id": str(backend.id),
            "ml_backend_name": backend.name,
            "model_id": request_context["model_id"],
            "task_type": request_context["type"],
            "total_tasks": 1,
        },
        result={
            "success_count": 0,
            "failed_count": 1,
            "failed_prediction_ids": [str(failed.id)],
            "duration_ms": 8_420,
            "total_cost": "0.0000",
        },
        error_message="RapidOCR 推理节点连接超时；健康检查已恢复，可重试失败项。",
        started_at=None,
        completed_at=now,
        created_at=now - timedelta(seconds=9),
        updated_at=now,
    )
    db.add(job)
    await db.flush()
    return {
        "asset_id": ASSET_ID,
        "project_id": str(project_id),
        "task_id": str(task_id),
        "backend_id": str(backend.id),
        "failed_prediction_id": str(failed.id),
        "async_job_id": str(job.id),
    }


async def run(args: argparse.Namespace) -> dict:
    run_id, owner_url = await screenshot_owner()
    engine = create_async_engine(owner_url, echo=False, hide_parameters=True)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    failure = None
    try:
        async with session_factory() as db:
            await assert_screenshot_scope(
                db,
                run_id=run_id,
                project_key="ocr_demo",
                project_id=args.project_id,
                task_id=args.task_id,
            )
            if args.action == "seed":
                if not args.user_email:
                    raise RuntimeError("seed requires --user-email")
                result = await seed(
                    db,
                    run_id=run_id,
                    project_id=args.project_id,
                    task_id=args.task_id,
                    user_email=args.user_email,
                )
            else:
                result = await cleanup_owned(
                    db,
                    run_id=run_id,
                    project_id=args.project_id,
                    task_id=args.task_id,
                )
            await db.commit()
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await close_screenshot_owner(engine, failure)

    return result


def main() -> int:
    if settings.environment == "production":
        print("[jobs-retry-recovery] refusing production database", file=sys.stderr)
        return 2
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
