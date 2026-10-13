from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_tracker_job import (
    VideoTrackerJob,
    VideoTrackerJobKind,
    VideoTrackerJobStatus,
)
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from app.services.video_tracking.runner import run_tracker_job
from app.workers.celery_app import celery_app


async def _run_video_tracker_job(job_id: str, celery_task_id: str | None) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    dispatch_context_factory = build_gpu_dispatch_context_factory(SessionLocal)
    try:
        async with SessionLocal() as db:
            job = await db.get(VideoTrackerJob, uuid.UUID(job_id))
            if job is not None and celery_task_id and not job.celery_task_id:
                job.celery_task_id = celery_task_id
                await db.commit()

            # v0.10.16 · async_jobs 双写（汇总索引层；失败不阻断主流程）
            async_job_id: uuid.UUID | None = None
            project_id: uuid.UUID | None = None
            if job is not None:
                task = await db.get(Task, job.task_id)
                project_id = task.project_id if task else None
                # Reauthorize the initiating actor before running inference: a
                # revoked account/project must not keep producing tracker work.
                if task is not None and project_id is not None:
                    from fastapi import HTTPException
                    from app.db.models.user import User
                    from app.services.project_access import resolve_project_access

                    actor = (
                        await db.get(User, job.created_by)
                        if job.created_by is not None
                        else None
                    )
                    project = await db.get(Project, project_id)
                    try:
                        if actor is None or not actor.is_active or project is None:
                            raise HTTPException(status_code=403, detail="revoked")
                        await resolve_project_access(db, user=actor, project=project)
                    except HTTPException:
                        job.status = VideoTrackerJobStatus.FAILED.value
                        job.error_message = "permission_revoked"
                        await db.commit()
                        return
                try:
                    project = await db.get(Project, project_id) if project_id else None
                    aj = await async_job_svc.create_job(
                        db,
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
                    await async_job_svc.mark_running(
                        db, aj.id, celery_task_id=celery_task_id
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

                result_job = await run_tracker_job(
                    db,
                    uuid.UUID(job_id),
                    shadow_session_factory=SessionLocal,
                    dispatch_context_factory=dispatch_context_factory,
                    failure_recorder=_record_gpu_arbiter_failure,
                )
            except Exception as e:
                gpu_arbiter_error = gpu_arbiter_error or gpu_arbiter_failure_record(e)
                if async_job_id is not None:
                    try:
                        await async_job_svc.mark_failed(
                            db,
                            async_job_id,
                            error=(
                                gpu_arbiter_error["message"]
                                if gpu_arbiter_error is not None
                                else str(e)
                            ),
                            result=(
                                {"gpu_arbiter_error": gpu_arbiter_error}
                                if gpu_arbiter_error is not None
                                else None
                            ),
                        )
                        await notify_job_terminal(db, job_id=async_job_id)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                raise
            else:
                # v0.10.49 · run_tracker_job 内部消化取消/失败（不抛异常），按专表最终状态
                # 同步 async_jobs 索引层，避免把 cancelled/failed 误标 completed（双写漂移）。
                if async_job_id is not None:
                    final_status = result_job.status if result_job is not None else None
                    try:
                        if final_status == VideoTrackerJobStatus.CANCELLED.value:
                            await async_job_svc.mark_cancelled(db, async_job_id)
                        elif final_status == VideoTrackerJobStatus.FAILED.value:
                            await async_job_svc.mark_failed(
                                db,
                                async_job_id,
                                error=(
                                    result_job.error_message or "tracker job failed"
                                ),
                                result=(
                                    {"gpu_arbiter_error": gpu_arbiter_error}
                                    if gpu_arbiter_error is not None
                                    else None
                                ),
                            )
                        else:
                            await async_job_svc.mark_complete(db, async_job_id)
                        await notify_job_terminal(db, job_id=async_job_id)
                        await db.commit()
                    except Exception:
                        await db.rollback()
    finally:
        await engine.dispose()


@celery_app.task(bind=True, max_retries=1, default_retry_delay=30, queue="gpu")
def run_video_tracker_job(self, job_id: str) -> None:
    asyncio.run(_run_video_tracker_job(job_id, getattr(self.request, "id", None)))
