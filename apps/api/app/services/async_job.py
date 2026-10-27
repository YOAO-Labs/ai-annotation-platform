"""v0.10.16 · async_jobs 服务层（ROADMAP §1.7）。

- create_job / mark_running / update_progress / mark_complete / mark_failed
  五个原子方法，所有调用方手动控制 commit（与现有 service 风格一致）。
- track_job 上下文管理器封装 mark_running + mark_complete/mark_failed，
  适合短任务；长任务（batch_predict 等）应显式调用 update_progress。
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus

logger = logging.getLogger(__name__)


async def lock_job(db: AsyncSession, job_id: uuid.UUID) -> AsyncJob | None:
    """Serialize a short terminal/result transaction against cancellation.

    Always refresh an already loaded row after acquiring the lock. Callers own
    the status policy and commit; never hold this lock during inference or I/O.
    """
    return await db.scalar(
        select(AsyncJob)
        .where(AsyncJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def create_job(
    db: AsyncSession,
    *,
    kind: str,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    payload: dict | None = None,
    celery_task_id: str | None = None,
) -> AsyncJob:
    """v0.10.16 · 创建 async_jobs 行。调用方负责 commit。"""
    job = AsyncJob(
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload or {},
        celery_task_id=celery_task_id,
        status=AsyncJobStatus.PENDING.value,
    )
    db.add(job)
    await db.flush()
    return job


async def mark_running(
    db: AsyncSession, job_id: uuid.UUID, *, celery_task_id: str | None = None
) -> None:
    """v0.10.16 · pending → running，记录 started_at。幂等。"""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status == AsyncJobStatus.PENDING.value:
        job.status = AsyncJobStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
    if celery_task_id and not job.celery_task_id:
        job.celery_task_id = celery_task_id


async def update_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    pct: int,
    *,
    extra_payload: dict | None = None,
) -> None:
    """v0.10.16 · 写入进度百分比（0-100），自动 clamp。

    调用方可塞 extra_payload（仅 merge 到 job.payload；result 留到 mark_complete）。
    """
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    job.progress_pct = max(0, min(100, int(pct)))
    if extra_payload:
        job.payload = {**(job.payload or {}), **extra_payload}


async def mark_complete(
    db: AsyncSession, job_id: uuid.UUID, *, result: dict | None = None
) -> None:
    """v0.10.16 · 完成态。幂等：若已 cancelled/failed 不覆盖。"""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status in {
        AsyncJobStatus.CANCELLED.value,
        AsyncJobStatus.FAILED.value,
    }:
        return
    job.status = AsyncJobStatus.COMPLETED.value
    job.progress_pct = 100
    job.completed_at = datetime.now(timezone.utc)
    if result is not None:
        job.result = result


async def mark_failed(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    error: str,
    result: dict | None = None,
) -> None:
    """v0.10.16 · 失败态。幂等。

    B-45 · 可选 result：当 batch_predict 全部子项失败需标记 job 为 failed 时，
    仍要把统计（success/failed_count、failed_prediction_ids）写进 result，
    以便前端失败重试链路可用。"""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status in {
        AsyncJobStatus.COMPLETED.value,
        AsyncJobStatus.CANCELLED.value,
    }:
        return
    job.status = AsyncJobStatus.FAILED.value
    job.completed_at = datetime.now(timezone.utc)
    job.error_message = (error or "")[:4000]
    if result is not None:
        job.result = result


async def mark_cancelled(
    db: AsyncSession, job_id: uuid.UUID, *, result: dict | None = None
) -> None:
    """v0.10.16 · 标记取消（终态）。仅 pending/running 可取消。"""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status in {AsyncJobStatus.PENDING.value, AsyncJobStatus.RUNNING.value}:
        job.status = AsyncJobStatus.CANCELLED.value
        job.completed_at = datetime.now(timezone.utc)
        if result is not None:
            job.result = result


async def request_cancel(db: AsyncSession, job_id: uuid.UUID) -> None:
    """v0.10.51 · 请求协作取消，调用方负责终态转换。"""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if job.status not in {AsyncJobStatus.PENDING.value, AsyncJobStatus.RUNNING.value}:
        return
    job.payload = {
        **(job.payload or {}),
        "cancel_requested": True,
        "cancel_requested_at": datetime.now(timezone.utc).isoformat(),
    }


async def find_by_celery_task_id(
    db: AsyncSession, celery_task_id: str
) -> AsyncJob | None:
    """v0.10.16 · Celery signals 兜底用，通过 task_id 反查 async_job。"""
    if not celery_task_id:
        return None
    res = await db.execute(
        select(AsyncJob).where(AsyncJob.celery_task_id == celery_task_id)
    )
    return res.scalar_one_or_none()


@contextlib.asynccontextmanager
async def track_job(
    db: AsyncSession,
    *,
    kind: str,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    payload: dict | None = None,
    celery_task_id: str | None = None,
) -> AsyncIterator[AsyncJob]:
    """v0.10.16 · 上下文管理器：自动 create+running，退出时 complete/failed。

    适合短任务（无中间进度上报）。长任务用 create_job + 显式 update_progress。
    """
    job = await create_job(
        db,
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload,
        celery_task_id=celery_task_id,
    )
    await mark_running(db, job.id)
    await db.flush()
    try:
        yield job
    except Exception as e:
        await mark_failed(db, job.id, error=str(e))
        await db.flush()
        raise
    else:
        await mark_complete(db, job.id)
        await db.flush()
