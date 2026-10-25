#!/usr/bin/env python3
"""Precisely remove persistent data created by one screenshot recording.

This helper is intentionally limited to screenshot-managed inference projects.
It cleans the exact task, accepted annotations, optional Celery jobs, and their
results named by the recorder; it never scans or mutates user-owned projects.
Audit rows are immutable by design and are deliberately not bypassed here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import set_committed_value

_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.config import settings  # noqa: E402
from app.db.models.async_job import AsyncJob  # noqa: E402
from app.db.models.dataset import Dataset, ProjectDataset  # noqa: E402
from app.services.project_access import get_workspace_project  # noqa: E402
from app.services.batch import BatchService  # noqa: E402
from app.services.screenshot_seed_spec import (  # noqa: E402
    PROJECT_SPECS,
    SEED_MANAGED_BY,
)
from app.workers.celery_app import celery_app  # noqa: E402
from aap_labeling.screenshot_operations_contract import (  # noqa: E402
    CleanupOCRRows,
    DeleteOCRPredictions,
    FinishOCRCleanup,
    SCREENSHOT_OPERATIONS_PROTOCOL,
)
from aap_labeling.prediction_record_contract import (  # noqa: E402
    DeletePredictionData,
    PredictionIdentity,
)
from app.services.labeling_core_read_relations import (  # noqa: E402
    execute_core_read,
    get_core_row,
    prediction_meta_row,
    prediction_row,
    task_row,
    video_tracker_job_row,
)
from app.services.labeling_screenshot_operations import screenshot_operations_command  # noqa: E402
from app.services.labeling_prediction_records import sync_deleted_prediction_records  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-key",
        choices=("image_demo", "ocr_demo", "video_demo"),
        default="ocr_demo",
    )
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--task-id", type=uuid.UUID, required=True)
    parser.add_argument("--celery-task-id", action="append", default=[])
    parser.add_argument(
        "--video-tracker-job-id", type=uuid.UUID, action="append", default=[]
    )
    parser.add_argument("--annotation-id", type=uuid.UUID, action="append", default=[])
    parser.add_argument("--prediction-id", type=uuid.UUID, action="append", default=[])
    return parser.parse_args()


async def assert_screenshot_scope(
    db: AsyncSession,
    *,
    project_key: str,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
) -> str:
    project = await get_workspace_project(db, project_id=project_id)
    expected_display_id = PROJECT_SPECS[project_key].display_id
    if project is None or project.display_id != expected_display_id:
        raise RuntimeError(
            f"cleanup scope is not screenshot project {expected_display_id}: {project_id}"
        )

    task = await get_core_row(db, task_row, task_id)
    if task is None or task.project_id != project_id:
        raise RuntimeError(
            f"cleanup task does not belong to screenshot project: {task_id}"
        )

    datasets = (
        (
            await db.execute(
                select(Dataset)
                .join(
                    ProjectDataset,
                    ProjectDataset.dataset_id == Dataset.id,
                )
                .where(ProjectDataset.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    managed = any(
        isinstance(dataset.metadata_, dict)
        and isinstance(dataset.metadata_.get("seed"), dict)
        and dataset.metadata_["seed"].get("managed_by") == SEED_MANAGED_BY
        and dataset.metadata_["seed"].get("logical_key") == project_key
        for dataset in datasets
    )
    if not managed:
        raise RuntimeError("cleanup refused: project is not screenshot-seed managed")

    task_spec = next(
        (
            candidate
            for candidate in PROJECT_SPECS[project_key].tasks
            if candidate.file_path == task.file_path
        ),
        None,
    )
    if task_spec is None:
        raise RuntimeError("cleanup refused: task is not declared by screenshot seed")
    return task_spec.status


async def cleanup(args: argparse.Namespace) -> dict[str, int]:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        async with session_factory() as db:
            expected_task_status = await assert_screenshot_scope(
                db,
                project_key=args.project_key,
                project_id=args.project_id,
                task_id=args.task_id,
            )
            tracker_job_ids = set(args.video_tracker_job_id)
            tracker_jobs = (
                (
                    await execute_core_read(
                        db,
                        select(
                            video_tracker_job_row.id,
                            video_tracker_job_row.task_id,
                            video_tracker_job_row.annotation_id,
                            video_tracker_job_row.prompt,
                            video_tracker_job_row.celery_task_id,
                        ).where(video_tracker_job_row.id.in_(tracker_job_ids)),
                    )
                ).all()
                if tracker_job_ids
                else []
            )
            found_tracker_job_ids = {job.id for job in tracker_jobs}
            if found_tracker_job_ids and found_tracker_job_ids != tracker_job_ids:
                raise RuntimeError("cleanup video tracker job set is incomplete")
            if any(job.task_id != args.task_id for job in tracker_jobs):
                raise RuntimeError(
                    "cleanup video tracker job is outside the screenshot task"
                )

            expected_source_ids = set(args.annotation_id)
            for job in tracker_jobs:
                seeds = (
                    job.prompt.get("seeds", []) if isinstance(job.prompt, dict) else []
                )
                source_ids = {
                    uuid.UUID(str(seed["source_annotation_id"]))
                    for seed in seeds
                    if isinstance(seed, dict) and seed.get("source_annotation_id")
                }
                # 单轨迹传播把源标注存在 job.annotation_id；批量传播
                # 才把各源写入 prompt.seeds[].source_annotation_id。
                if job.annotation_id is not None:
                    source_ids.add(job.annotation_id)
                if source_ids != expected_source_ids:
                    raise RuntimeError(
                        "cleanup video tracker sources do not match supplied annotations"
                    )

            celery_task_ids = list(
                dict.fromkeys(
                    [
                        *args.celery_task_id,
                        *[
                            job.celery_task_id
                            for job in tracker_jobs
                            if job.celery_task_id
                        ],
                    ]
                )
            )
            # Successful recordings are terminal already. On an interrupted flow,
            # revoke any still-queued work before removing its isolated test row;
            # terminate=False keeps the dedicated screenshot worker alive.
            for celery_task_id in celery_task_ids:
                try:
                    celery_app.AsyncResult(celery_task_id).revoke(terminate=False)
                except Exception as exc:  # noqa: BLE001 - scope checks remain authoritative
                    print(
                        f"[cleanup-screenshot-inference] WARN celery revoke failed: {exc}",
                        file=sys.stderr,
                    )

            source_cleanup = await screenshot_operations_command(
                db,
                CleanupOCRRows(
                    schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                    operation="cleanup_ocr_rows",
                    project_key=args.project_key,
                    project_id=args.project_id,
                    task_id=args.task_id,
                    tracker_job_ids=tuple(sorted(tracker_job_ids, key=str)),
                    annotation_ids=tuple(sorted(args.annotation_id, key=str)),
                ),
            )
            video_tracker_jobs = source_cleanup.counts["video_tracker_jobs"]
            annotation_count = source_cleanup.counts["annotations"]
            task = await get_core_row(db, task_row, args.task_id)
            if task is None:  # scope assertion above already guarantees this
                raise RuntimeError(f"cleanup task disappeared: {args.task_id}")
            if args.prediction_id:
                prediction_ids = set(args.prediction_id)
                predictions = (
                    await execute_core_read(
                        db,
                        select(
                            prediction_row.id,
                            prediction_row.task_id,
                            prediction_row.project_id,
                            prediction_row.created_at,
                        ).where(prediction_row.id.in_(prediction_ids)),
                    )
                ).all()
                found_prediction_ids = {prediction.id for prediction in predictions}
                if found_prediction_ids and found_prediction_ids != prediction_ids:
                    raise RuntimeError("cleanup prediction set is incomplete")
                if any(
                    prediction.task_id != args.task_id
                    or prediction.project_id != args.project_id
                    for prediction in predictions
                ):
                    raise RuntimeError(
                        "cleanup prediction is outside the screenshot task"
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
                prediction_cleanup = await screenshot_operations_command(
                    db,
                    DeleteOCRPredictions(
                        schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                        operation="delete_ocr_predictions",
                        project_key=args.project_key,
                        project_id=args.project_id,
                        task_id=args.task_id,
                        prediction_ids=tuple(sorted(prediction_ids, key=str)),
                    ),
                )
                prediction_counts = {
                    "predictions": prediction_cleanup.counts["predictions"],
                    "failed_predictions": 0,
                    "ai_annotations_deactivated": 0,
                }
                sync_deleted_prediction_records(
                    db,
                    DeletePredictionData(
                        operation="delete_for_tasks",
                        predictions=prediction_counts["predictions"],
                        failed_predictions=0,
                        deleted_predictions=[
                            PredictionIdentity(id=row.id, created_at=row.created_at)
                            for row in predictions
                        ],
                        deleted_failed_prediction_ids=[],
                        deleted_prediction_meta_ids=meta_ids,
                    ),
                )
                set_committed_value(
                    task,
                    "total_predictions",
                    prediction_cleanup.counts["remaining_predictions"],
                )
            else:
                prediction_counts = await BatchService(db).clean_task_predictions(
                    [args.task_id]
                )
            await screenshot_operations_command(
                db,
                FinishOCRCleanup(
                    schema=SCREENSHOT_OPERATIONS_PROTOCOL,
                    operation="finish_ocr_cleanup",
                    project_key=args.project_key,
                    project_id=args.project_id,
                    task_id=args.task_id,
                    expected_status=expected_task_status,
                ),
            )
            task = await get_core_row(
                db, task_row, args.task_id, populate_existing=True
            )
            if task is None:
                raise RuntimeError(f"cleanup task disappeared: {args.task_id}")
            if celery_task_ids:
                job_result = await db.execute(
                    delete(AsyncJob).where(
                        AsyncJob.celery_task_id.in_(celery_task_ids),
                    )
                )
                async_jobs = job_result.rowcount or 0
            else:
                async_jobs = 0
            await db.commit()
    finally:
        await engine.dispose()

    # Celery result backend is outside PostgreSQL. Forgetting an already-expired
    # result is harmless and keeps the recorder idempotent for the afterAll retry.
    for celery_task_id in celery_task_ids:
        try:
            celery_app.AsyncResult(celery_task_id).forget()
        except Exception as exc:  # noqa: BLE001 - DB cleanup remains authoritative
            print(
                f"[cleanup-screenshot-inference] WARN celery forget failed: {exc}",
                file=sys.stderr,
            )

    return {
        **prediction_counts,
        "annotations": annotation_count,
        "async_jobs": async_jobs,
        "video_tracker_jobs": video_tracker_jobs,
    }


def main() -> int:
    if settings.environment == "production":
        print(
            "[cleanup-screenshot-inference] refusing to run in production",
            file=sys.stderr,
        )
        return 2
    args = parse_args()
    counts = asyncio.run(cleanup(args))
    print(f"[cleanup-screenshot-inference] {json.dumps(counts, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
