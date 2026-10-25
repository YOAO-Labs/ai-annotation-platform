from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.services.project_access import get_workspace_project
from app.db.models.video_tracker_job import VideoTrackerJobKind, VideoTrackerJobStatus
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from app.services.accepted_tracker_inputs import TrackerInputDeferred
from app.services.video_tracking.execution import (
    TrackerExecutionDeferred,
    TrackerExecutionLost,
    fence_tracker_write,
)
from app.services.video_tracking.control import video_control_command, video_control_job
from app.services.labeling_core_read_relations import get_core_row, task_row
from aap_labeling.video_control_contract import LockWorkerJob, ReadWorkerJob
from app.services.video_tracking.runner import run_tracker_job
from app.workers.celery_app import celery_app


async def _sync_terminal_summary(
    db, *, tracker_job_id, async_job_id, gpu_arbiter_error
):
    """Project only a locked canonical terminal fact, never a worker exception."""
    await db.rollback()
    canonical = await video_control_command(
        db,
        LockWorkerJob(
            schema="aap-labeling-video-control/v1",
            operation="lock_worker_job",
            job_id=tracker_job_id,
        ),
    )
    if canonical is None:
        return
    status, error = canonical["status"], canonical["error_message"]
    if status == VideoTrackerJobStatus.CANCELLED.value:
        await async_job_svc.mark_cancelled(db, async_job_id)
    elif status == VideoTrackerJobStatus.FAILED.value:
        await async_job_svc.mark_failed(
            db,
            async_job_id,
            error=error or "tracker job failed",
            result={"gpu_arbiter_error": gpu_arbiter_error}
            if gpu_arbiter_error
            else None,
        )
    elif status in {
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
        VideoTrackerJobStatus.ACCEPTED.value,
        VideoTrackerJobStatus.COMPLETED.value,
        VideoTrackerJobStatus.DISCARDED.value,
    }:
        await async_job_svc.mark_complete(db, async_job_id)
    else:
        return
    await notify_job_terminal(db, job_id=async_job_id)
    await db.commit()


async def _run_video_tracker_job(job_id: str, celery_task_id: str | None) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    dispatch_context_factory = build_gpu_dispatch_context_factory(SessionLocal)
    try:
        async with SessionLocal() as db:
            job = await video_control_job(
                db,
                ReadWorkerJob(
                    schema="aap-labeling-video-control/v1",
                    operation="read_worker_job",
                    job_id=uuid.UUID(job_id),
                ),
            )
            # v0.10.16 · async_jobs 双写（汇总索引层；失败不阻断主流程）
            async_job_id: uuid.UUID | None = None
            project_id: uuid.UUID | None = None
            tracker_input_ready = None
            if job is not None:
                task = await get_core_row(db, task_row, job.task_id)
                project_id = task.project_id if task else None
                try:
                    project = (
                        await get_workspace_project(db, project_id=project_id)
                        if project_id
                        else None
                    )
                    aj = await async_job_svc.ensure_video_tracker_summary(
                        db,
                        tracker_job_id=job.id,
                        kind=(
                            "video_correction"
                            if job.job_kind == VideoTrackerJobKind.CORRECTION.value
                            else "video_tracker"
                        ),
                        project_id=project_id,
                        user_id=job.created_by,
                        payload={
                            "video_tracker_job_id": str(job.id),
                            "task_id": str(job.task_id),
                            "task_display_id": task.display_id if task else None,
                            "project_display_id": (
                                project.display_id if project else None
                            ),
                            "from_frame": job.from_frame,
                            "to_frame": job.to_frame,
                            "model_key": job.model_key,
                            "direction": job.direction,
                            "job_kind": job.job_kind,
                            "correction_frame": job.correction_frame,
                        },
                        celery_task_id=celery_task_id,
                    )
                    await db.commit()
                    async_job_id = aj.id
                except Exception:
                    await db.rollback()
                    async_job_id = None

            try:
                gpu_arbiter_error: dict | None = None

                def _record_gpu_arbiter_failure(failure: dict) -> None:
                    nonlocal gpu_arbiter_error
                    gpu_arbiter_error = failure

                async def _summary_started(start_db, started_job):
                    if async_job_id is None:
                        return
                    try:
                        await fence_tracker_write(start_db, started_job.id, "progress")
                        await async_job_svc.mark_running(
                            start_db, async_job_id, celery_task_id=celery_task_id
                        )
                        await start_db.commit()
                    except TrackerExecutionLost:
                        await start_db.rollback()
                        raise
                    except Exception:
                        await start_db.rollback()

                await run_tracker_job(
                    db,
                    uuid.UUID(job_id),
                    shadow_session_factory=SessionLocal,
                    dispatch_context_factory=dispatch_context_factory,
                    failure_recorder=_record_gpu_arbiter_failure,
                    tracker_input_ready=tracker_input_ready,
                    celery_task_id=celery_task_id,
                    on_started=_summary_started,
                )
            except (TrackerInputDeferred, TrackerExecutionDeferred):
                await db.rollback()
                if async_job_id is not None:
                    try:
                        await _sync_terminal_summary(
                            db,
                            tracker_job_id=uuid.UUID(job_id),
                            async_job_id=async_job_id,
                            gpu_arbiter_error=gpu_arbiter_error,
                        )
                    except Exception:
                        await db.rollback()
                raise
            except Exception as e:
                gpu_arbiter_error = gpu_arbiter_error or gpu_arbiter_failure_record(e)
                if async_job_id is not None:
                    try:
                        await _sync_terminal_summary(
                            db,
                            tracker_job_id=uuid.UUID(job_id),
                            async_job_id=async_job_id,
                            gpu_arbiter_error=gpu_arbiter_error,
                        )
                    except Exception:
                        await db.rollback()
                raise
            else:
                if async_job_id is not None:
                    try:
                        await _sync_terminal_summary(
                            db,
                            tracker_job_id=uuid.UUID(job_id),
                            async_job_id=async_job_id,
                            gpu_arbiter_error=gpu_arbiter_error,
                        )
                    except Exception:
                        await db.rollback()
    finally:
        await engine.dispose()


@celery_app.task(
    bind=True,
    max_retries=None,
    default_retry_delay=30,
    queue="gpu",
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_video_tracker_job(self, job_id: str) -> None:
    try:
        asyncio.run(_run_video_tracker_job(job_id, getattr(self.request, "id", None)))
    except (TrackerInputDeferred, TrackerExecutionDeferred) as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=None) from exc
