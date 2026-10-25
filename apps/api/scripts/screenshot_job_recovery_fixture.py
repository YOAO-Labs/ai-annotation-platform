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

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.config import settings  # noqa: E402
from app.db.models.async_job import AsyncJob, AsyncJobStatus  # noqa: E402
from app.db.models.ml_backend_registry import MLBackendRegistry  # noqa: E402
from app.db.models.notification import Notification  # noqa: E402
from app.services.project_access import get_workspace_project  # noqa: E402
from app.services.identity_commands import IdentityCommandService  # noqa: E402
from app.services.prediction import PredictionService  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402
from scripts.cleanup_screenshot_ocr_flow import assert_screenshot_scope  # noqa: E402
from aap_labeling.screenshot_operations_contract import (  # noqa: E402
    CleanupRecoveryRows,
    SCREENSHOT_OPERATIONS_PROTOCOL,
)
from aap_labeling.prediction_record_contract import (  # noqa: E402
    DeletePredictionData,
    PredictionIdentity,
)
from app.services.labeling_core_read_relations import (  # noqa: E402
    execute_core_read,
    failed_prediction_row,
    get_core_row,
    prediction_meta_row,
    prediction_row,
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
) -> tuple[list[AsyncJob], set[uuid.UUID], set[uuid.UUID], list[str]]:
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

    retry_jobs: list[AsyncJob] = []
    if failed_ids:
        retry_jobs = list(
            (
                await db.execute(
                    select(AsyncJob).where(
                        AsyncJob.project_id == project_id,
                        AsyncJob.kind == "prediction_retry",
                        AsyncJob.payload["failed_prediction_id"].astext.in_(
                            [str(value) for value in failed_ids]
                        ),
                    )
                )
            ).all()
        )

    prediction_ids = {
        prediction_id
        for job in retry_jobs
        if (prediction_id := _uuid((job.result or {}).get("prediction_id"))) is not None
    }
    celery_task_ids = [
        job.celery_task_id
        for job in retry_jobs
        if isinstance(job.celery_task_id, str) and job.celery_task_id
    ]
    return [*original_jobs, *retry_jobs], failed_ids, prediction_ids, celery_task_ids


async def cleanup_owned(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> tuple[dict[str, int], list[str]]:
    jobs, failed_ids, prediction_ids, celery_task_ids = await _owned_rows(
        db,
        project_id=project_id,
        task_id=task_id,
    )
    job_ids = {job.id for job in jobs}
    prediction_count = 0
    failed_count = 0
    remaining_predictions = None
    owner_calls = 0

    if prediction_ids:
        predictions = list(
            (
                await execute_core_read(
                    db,
                    select(
                        prediction_row.id,
                        prediction_row.project_id,
                        prediction_row.task_id,
                        prediction_row.created_at,
                    ).where(prediction_row.id.in_(prediction_ids)),
                )
            ).all()
        )
        if any(
            prediction.project_id != project_id or prediction.task_id != task_id
            for prediction in predictions
        ):
            raise RuntimeError(
                "cleanup refused: retry prediction escaped screenshot task"
            )
        meta_ids = list(
            (
                await execute_core_read(
                    db,
                    select(prediction_meta_row.id).where(
                        prediction_meta_row.prediction_id.in_(prediction_ids)
                    ),
                )
            ).scalars()
        )
        result = await screenshot_operations_command(
            db,
            CleanupRecoveryRows(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="cleanup_recovery_rows",
                project_id=project_id,
                task_id=task_id,
                failed_prediction_ids=(),
                prediction_ids=tuple(sorted(prediction_ids, key=str)),
            ),
        )
        prediction_count = result.counts["predictions"]
        remaining_predictions = result.counts["remaining_predictions"]
        sync_deleted_prediction_records(
            db,
            DeletePredictionData(
                operation="delete_for_tasks",
                predictions=prediction_count,
                failed_predictions=0,
                deleted_predictions=[
                    PredictionIdentity(id=row.id, created_at=row.created_at)
                    for row in predictions
                ],
                deleted_failed_prediction_ids=[],
                deleted_prediction_meta_ids=meta_ids,
            ),
        )
        owner_calls += 1

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
    if job_ids:
        job_result = await db.execute(
            delete(AsyncJob).where(
                AsyncJob.id.in_(job_ids),
                AsyncJob.project_id == project_id,
            )
        )
    else:
        job_result = None

    if owner_calls == 0:
        result = await screenshot_operations_command(
            db,
            CleanupRecoveryRows(
                schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                operation="cleanup_recovery_rows",
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
    return (
        {
            "async_jobs": job_result.rowcount if job_result is not None else 0,
            "failed_predictions": failed_count,
            "notifications": (
                notification_result.rowcount if notification_result is not None else 0
            ),
            "predictions": prediction_count,
        },
        celery_task_ids,
    )


async def seed(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    user_email: str,
) -> dict[str, str]:
    await cleanup_owned(db, project_id=project_id, task_id=task_id)
    task = await get_core_row(db, task_row, task_id)
    project = await get_workspace_project(db, project_id=project_id)
    if task is None or project is None or task.project_id != project.id:
        raise RuntimeError("screenshot OCR task or project disappeared")
    user_id = await IdentityCommandService(db).lookup_seed_account_id(user_email)
    if user_id is None:
        raise RuntimeError(f"recording user not found: {user_email}")

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
    job = AsyncJob(
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
        started_at=now - timedelta(seconds=9),
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
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    celery_task_ids: list[str] = []
    try:
        async with session_factory() as db:
            await assert_screenshot_scope(
                db,
                project_key="ocr_demo",
                project_id=args.project_id,
                task_id=args.task_id,
            )
            if args.action == "seed":
                if not args.user_email:
                    raise RuntimeError("seed requires --user-email")
                result = await seed(
                    db,
                    project_id=args.project_id,
                    task_id=args.task_id,
                    user_email=args.user_email,
                )
            else:
                result, celery_task_ids = await cleanup_owned(
                    db,
                    project_id=args.project_id,
                    task_id=args.task_id,
                )
            await db.commit()
    finally:
        await engine.dispose()

    for celery_task_id in celery_task_ids:
        try:
            celery_app.AsyncResult(celery_task_id).forget()
        except Exception as exc:  # noqa: BLE001
            print(
                f"[jobs-retry-recovery] WARN celery forget failed: {exc}",
                file=sys.stderr,
            )
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
