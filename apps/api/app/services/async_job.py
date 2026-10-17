"""Generic async-job row commands with caller-owned transactions.

Payload updates merge shallowly; result projections replace. State writers do
not commit or publish notices. Delete commands return affected IDs or counts
without committing. Creation and track_job explicitly flush; other state writers
do not. Default cancellation allows only pending/running; specialized workers
supply their own precedence.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Collection
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobKind, AsyncJobStatus

logger = logging.getLogger(__name__)


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


async def delete_batch_predict_jobs(db: AsyncSession, batch_id: uuid.UUID) -> int:
    """Remove batch_predict history for a batch; the caller owns the transaction."""
    result = await db.execute(
        delete(AsyncJob).where(
            AsyncJob.kind == AsyncJobKind.BATCH_PREDICT.value,
            AsyncJob.payload["batch_id"].astext == str(batch_id),
        )
    )
    return int(result.rowcount or 0)


async def delete_terminal_jobs_before(
    db: AsyncSession, cutoff: datetime, *, statuses: Collection[str]
) -> list[uuid.UUID]:
    """Delete selected terminal jobs older than cutoff; caller owns commit."""
    result = await db.execute(
        delete(AsyncJob)
        .where(
            AsyncJob.status.in_(statuses),
            AsyncJob.created_at < cutoff,
        )
        .returning(AsyncJob.id)
    )
    return list(result.scalars().all())


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


def bind_execution_id(job: AsyncJob, execution_id: str | None) -> None:
    """Set or clear the execution ID; the caller owns lookup, commit, and ordering."""
    job.celery_task_id = execution_id


def _merge_payload(job: AsyncJob, updates: dict) -> None:
    job.payload = {**(job.payload or {}), **updates}


async def update_payload(db: AsyncSession, job_id: uuid.UUID, updates: dict) -> None:
    """Merge job-specific payload fields without committing."""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    _merge_payload(job, updates)


async def update_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    pct: int,
    *,
    extra_payload: dict | None = None,
    result: dict | None = None,
) -> None:
    """Write clamped progress, merge supplied payload, and replace supplied result."""
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return
    if result is not None:
        job.result = result
    job.progress_pct = max(0, min(100, int(pct)))
    if extra_payload:
        _merge_payload(job, extra_payload)


async def replace_result(db: AsyncSession, job_id: uuid.UUID, *, result: dict) -> None:
    """Replace the result projection without changing the job lifecycle."""
    job = await db.get(AsyncJob, job_id)
    if job is not None:
        job.result = result


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
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    result: dict | None = None,
    allowed_statuses: Collection[str] | None = (
        AsyncJobStatus.PENDING.value,
        AsyncJobStatus.RUNNING.value,
    ),
    progress_pct: int | None = None,
) -> AsyncJob | None:
    """Write cancelled fields when the caller's status policy permits it.

    By default, only pending/running jobs can be cancelled. Callers may pass a
    different status collection, or ``None`` to write regardless of status.
    The caller owns commit and any terminal notification.
    """
    job = await db.get(AsyncJob, job_id)
    if job is None:
        return None
    if allowed_statuses is not None and job.status not in allowed_statuses:
        return job
    job.status = AsyncJobStatus.CANCELLED.value
    job.completed_at = datetime.now(timezone.utc)
    if progress_pct is not None:
        job.progress_pct = max(0, min(100, int(progress_pct)))
    if result is not None:
        job.result = result
    return job


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
