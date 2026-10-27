"""Generic async-job row commands with caller-owned transactions.

Payload updates merge shallowly; result projections replace. State writers do
not commit or publish notices. Delete commands return affected IDs or counts
without committing. Creation and track_job explicitly flush; dispatch failure
uses a conditional SQL update; other state writers defer to the caller's flush.
Default cancellation allows only pending/running; specialized workers supply
their own precedence.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Collection
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from aap_jobs.lifecycle import JobsLifecycleManager

from app.db.models.async_job import AsyncJob, AsyncJobKind, AsyncJobStatus
from app.services.jobs_lifecycle_data import JobsLifecyclePostgresData


def _manager(db):
    return JobsLifecycleManager(
        JobsLifecyclePostgresData(db), clock=lambda: datetime.now(timezone.utc)
    )


async def get_job(
    db: AsyncSession, job_id: uuid.UUID, *, populate_existing: bool = False
) -> AsyncJob | None:
    return await JobsLifecyclePostgresData(db).get_job(
        job_id, populate_existing=populate_existing
    )


async def execution_snapshot_id(
    db: AsyncSession, job_id: uuid.UUID
) -> uuid.UUID | None:
    """Read the immutable binding after the Job row was read or locked."""
    return await JobsLifecyclePostgresData(db).read_execution_snapshot_id(job_id)


async def create_job(
    db: AsyncSession,
    *,
    kind: str,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    payload: dict | None = None,
    celery_task_id: str | None = None,
) -> AsyncJob:
    """v0.10.16 · 创建 async_jobs 行。调用方负责 commit。

    The execution snapshot is not a creation field: the closed creation view
    omits it. Call ``bind_execution_snapshot`` after the pending row exists.
    """
    return await _manager(db).create_job(
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload,
        celery_task_id=celery_task_id,
    )


async def ensure_video_tracker_summary(
    db: AsyncSession,
    *,
    tracker_job_id: uuid.UUID,
    kind: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    payload: dict,
    celery_task_id: str | None,
) -> AsyncJob:
    """Retain one best-effort index row across tracker worker redeliveries.

    The canonical tracker UUID is also its summary UUID in the separate index.
    Reuse never resets lifecycle or progress and never supplies execution rights.
    """
    if kind not in {
        AsyncJobKind.VIDEO_TRACKER.value,
        AsyncJobKind.VIDEO_CORRECTION.value,
    } or (payload.get("video_tracker_job_id") != str(tracker_job_id)):
        raise ValueError("invalid video tracker summary")
    payload = {**payload, "video_execution_protocol": "aap-video-execution-data/v1"}
    await JobsLifecyclePostgresData(db).insert_video_summary(
        id=tracker_job_id,
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload,
        celery_task_id=celery_task_id,
    )
    summary = await get_job(db, tracker_job_id, populate_existing=True)
    if summary is None or (
        summary.kind != kind
        or summary.user_id != user_id
        or summary.project_id != project_id
        or (summary.payload or {}).get("video_tracker_job_id") != str(tracker_job_id)
        or (summary.payload or {}).get("video_execution_protocol")
        != "aap-video-execution-data/v1"
    ):
        raise ValueError("video tracker summary identity conflict")
    return summary


async def delete_batch_predict_jobs(db: AsyncSession, batch_id: uuid.UUID) -> int:
    """Cancel live batch_predict jobs, then remove history in the caller's transaction.

    The status transition lets durable result owners revoke execution leases and
    retire pending snapshots before the guarded history delete runs.
    """
    return await JobsLifecyclePostgresData(db).delete_batch_predict_jobs(
        batch_id, completed_at=datetime.now(timezone.utc)
    )


async def delete_terminal_jobs_before(
    db: AsyncSession, cutoff: datetime, *, statuses: Collection[str]
) -> list[uuid.UUID]:
    """Delete selected terminal jobs older than cutoff; caller owns commit."""
    return await JobsLifecyclePostgresData(db).delete_terminal_jobs_before(
        cutoff, statuses=statuses
    )


async def mark_running(
    db: AsyncSession, job_id: uuid.UUID, *, celery_task_id: str | None = None
) -> None:
    """v0.10.16 · pending → running，记录 started_at。幂等。"""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).mark_running(job, celery_task_id=celery_task_id)


def bind_execution_id(job: AsyncJob, execution_id: str | None) -> None:
    """Set or clear the execution ID; the caller owns lookup, commit, and ordering."""
    _manager(None).bind_execution_id(job, execution_id)


async def bind_execution_snapshot(
    db: AsyncSession,
    job: AsyncJob,
    *,
    actor_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> None:
    """Bind the immutable execution snapshot through the Jobs owner capability.

    ``execution_snapshot_id`` is deliberately absent from the generic lifecycle
    command surface, so this narrow CAS is the only writer. It validates the
    current owner, project and pending state, binds once and stays idempotent
    for a redelivery of the same admitted Job.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import attributes

    if job is None:
        raise ValueError("execution snapshot binding requires a Job row")
    bound = await db.scalar(
        text(
            "SELECT aap_jobs_v1.bind_prediction_execution_snapshot("
            "CAST(:job_id AS pg_catalog.uuid), "
            "CAST(:actor_id AS pg_catalog.uuid), "
            "CAST(:snapshot_id AS pg_catalog.uuid))"
        ),
        {"job_id": job.id, "actor_id": actor_id, "snapshot_id": snapshot_id},
    )
    if bound is not True:
        raise ValueError("execution snapshot binding rejected")
    attributes.set_committed_value(job, "execution_snapshot_id", snapshot_id)


async def update_payload(db: AsyncSession, job_id: uuid.UUID, updates: dict) -> None:
    """Merge job-specific payload fields without committing."""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).update_payload(job, updates)


async def update_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    pct: int,
    *,
    extra_payload: dict | None = None,
    result: dict | None = None,
) -> None:
    """Write clamped progress, merge supplied payload, and replace supplied result."""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).update_progress(
            job, pct, extra_payload=extra_payload, result=result
        )


async def replace_result(db: AsyncSession, job_id: uuid.UUID, *, result: dict) -> None:
    """Replace the result projection without changing the job lifecycle."""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).replace_result(job, result=result)


async def mark_complete(
    db: AsyncSession, job_id: uuid.UUID, *, result: dict | None = None
) -> None:
    """v0.10.16 · 完成态。幂等：若已 cancelled/failed 不覆盖。"""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).mark_complete(job, result=result)


async def mark_dispatch_failed(
    db: AsyncSession, job_id: uuid.UUID, *, error: str
) -> bool:
    """Fail only an unclaimed job; a broker error may follow successful delivery.

    Check the database state atomically, even when this session still caches
    pending. A worker that has claimed or settled the job keeps its state.
    The caller owns commit.
    """
    return await JobsLifecyclePostgresData(db).mark_dispatch_failed(
        job_id, failed_at=datetime.now(timezone.utc), error=(error or "")[:4000]
    )


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
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).mark_failed(job, error=error, result=result)


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
    job = await get_job(db, job_id)
    if job is None:
        return None
    return _manager(db).mark_cancelled(
        job, result=result, allowed_statuses=allowed_statuses, progress_pct=progress_pct
    )


async def request_cancel(db: AsyncSession, job_id: uuid.UUID) -> None:
    """v0.10.51 · 请求协作取消，调用方负责终态转换。"""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).request_cancel(job)


async def find_by_celery_task_id(
    db: AsyncSession, celery_task_id: str
) -> AsyncJob | None:
    """v0.10.16 · Celery signals 兜底用，通过 task_id 反查 async_job。"""
    return await JobsLifecyclePostgresData(db).find_by_execution_id(celery_task_id)


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
    async with _manager(db).track_job(
        kind=kind,
        user_id=user_id,
        project_id=project_id,
        payload=payload,
        celery_task_id=celery_task_id,
    ) as job:
        yield job
