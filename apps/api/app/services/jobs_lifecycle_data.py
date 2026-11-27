"""Jobs row capabilities and lifecycle commands with caller-owned transactions.

Payload updates merge shallowly; result projections replace. State writers do
not commit or publish notices. Creation and track_job explicitly flush; dispatch
failure uses a conditional SQL update; other state writers defer to the caller's
flush. Default cancellation allows pending/running; specialized workers supply
their own precedence.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Collection
from datetime import datetime, timezone

from sqlalchemy import inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from yoao_jobs.lifecycle import JobsLifecycleManager
from yoao_jobs.row_lock_contract import (
    JOBS_EXECUTION_SNAPSHOT_FUNCTION,
    JOBS_ROW_LOCK_FUNCTIONS,
)

from app.db.jobs_lifecycle import encode_jobs_json, observe_job_changes
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.owner_orm_compat import (
    mark_missing_identity,
    synchronize_owner_deleted_ids,
    synchronize_owner_fetch,
)


def job_lock_statement(job_id, *, mode, populate_existing=False):
    """Keep the caller's ORM result, refresh flag and autoflush around a fixed lock."""
    function = JOBS_ROW_LOCK_FUNCTIONS[mode]
    return (
        select(AsyncJob)
        .from_statement(
            text(
                "SELECT job.*, "
                "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                "AS execution_snapshot_id, progress.last_progress_at, "
                "progress.progress_observation FROM "
                f"{function}("
                "CAST(:job_id AS pg_catalog.uuid)) AS job "
                "CROSS JOIN LATERAL yoao_jobs_v1.read_job_progress(job.id) "
                "AS progress"
            )
        )
        .params(job_id=job_id)
        .execution_options(populate_existing=populate_existing)
    )


class JobsLifecyclePostgresData:
    def __init__(self, db):
        self.db = db

    changes = staticmethod(observe_job_changes)

    @staticmethod
    def _reset_snapshot_binding(row):
        """Keep the owner-bound snapshot out of generic lifecycle payload flushes.

        ``execution_snapshot_id`` is written only by
        ``yoao_jobs_v1.bind_prediction_execution_snapshot``. A public read must
        therefore return the database value as committed instead of re-submitting
        an unsupported direct change through the lifecycle command channel.
        """
        state = inspect(row)
        if (
            not state.persistent
            or "execution_snapshot_id" not in state.mapper.column_attrs
        ):
            return
        key = "execution_snapshot_id"
        committed = state.committed_state.get(key, attributes.NO_VALUE)
        if committed is attributes.NO_VALUE:
            if key not in state.dict:
                return
            committed = state.dict[key]
        attributes.set_committed_value(row, key, committed)

    async def create_job(self, **values):
        if "execution_snapshot_id" in values:
            raise ValueError(
                "execution_snapshot_id must be bound after creation through "
                "yoao_jobs_v1.bind_prediction_execution_snapshot"
            )
        row = AsyncJob(**values)
        self.db.add(row)
        await self.db.flush()
        return row

    async def flush(self):
        await self.db.flush()

    async def read_execution_snapshot_id(self, job_id):
        """Read the immutable snapshot binding after the caller read/locked the Job."""
        await self._autoflush()
        return await self.db.scalar(
            text(
                f"SELECT {JOBS_EXECUTION_SNAPSHOT_FUNCTION}("
                "CAST(:job_id AS pg_catalog.uuid))"
            ),
            {"job_id": job_id},
        )

    async def get_job(self, job_id, *, populate_existing=False):
        session = self.db.sync_session
        identity = inspect(AsyncJob).identity_key_from_primary_key((job_id,))
        cached = session.identity_map.get(identity)
        state = inspect(cached) if cached is not None else None
        if not populate_existing and state is not None and not state.expired:
            self._reset_snapshot_binding(cached)
            return cached
        result = await self.db.execute(
            select(AsyncJob)
            .from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    "progress.progress_observation FROM yoao_jobs_v1.read_job("
                    "CAST(:job_id AS pg_catalog.uuid)) AS job "
                    "CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            )
            .execution_options(populate_existing=True),
            {"job_id": job_id},
        )
        row = result.scalar_one_or_none()
        if row is None and state is not None and state.expired:
            mark_missing_identity(session, state)
        if row is not None:
            self._reset_snapshot_binding(row)
        return row

    async def find_by_execution_id(self, execution_id):
        if not execution_id:
            return None
        result = await self.db.execute(
            select(AsyncJob).from_statement(
                text(
                    "SELECT job.*, "
                    "yoao_jobs_v1.read_job_execution_snapshot(job.id) "
                    "AS execution_snapshot_id, progress.last_progress_at, "
                    "progress.progress_observation FROM "
                    "yoao_jobs_v1.jobs_by_execution_id("
                    "CAST(:execution_id AS pg_catalog.text)) AS job "
                    "CROSS JOIN LATERAL "
                    "yoao_jobs_v1.read_job_progress(job.id) AS progress"
                )
            ),
            {"execution_id": execution_id},
        )
        row = result.scalar_one_or_none()
        if row is not None:
            self._reset_snapshot_binding(row)
        return row

    async def find_archive_deliveries(self, execution_id: str):
        result = await self.db.execute(
            text(
                "SELECT id, status, result FROM yoao_jobs_v1.jobs_by_execution_id("
                "CAST(:execution_id AS pg_catalog.text)) "
                "WHERE kind = 'audit_archive' ORDER BY created_at, id LIMIT 2"
            ),
            {"execution_id": execution_id},
        )
        return result.all()

    async def insert_video_summary(self, **values):
        await self._autoflush()
        await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.insert_video_summary("
                "CAST(:id AS pg_catalog.uuid), CAST(:kind AS pg_catalog.text), "
                "CAST(:user_id AS pg_catalog.uuid), "
                "CAST(:project_id AS pg_catalog.uuid), "
                "CAST(:payload AS pg_catalog.jsonb), "
                "CAST(:celery_task_id AS pg_catalog.text))"
            ),
            {**values, "payload": encode_jobs_json(values["payload"])},
        )

    async def _autoflush(self):
        if self.db.autoflush:
            await self.db.flush()

    def _synchronize(self, statement, affected_ids):
        synchronize_owner_fetch(self.db.sync_session, AsyncJob, statement, affected_ids)

    async def mark_dispatch_failed(self, job_id, *, failed_at, error):
        await self._autoflush()
        changed = await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.mark_dispatch_failed("
                "CAST(:job_id AS pg_catalog.uuid), "
                "CAST(:failed_at AS pg_catalog.timestamptz), "
                "CAST(:error AS pg_catalog.text))"
            ),
            {"job_id": job_id, "failed_at": failed_at, "error": error},
        )
        if changed:
            self._synchronize(
                update(AsyncJob).values(
                    status="failed", completed_at=failed_at, error_message=error
                ),
                (job_id,),
            )
        return bool(changed)

    async def delete_batch_predict_jobs(self, batch_id, *, completed_at):
        await self._autoflush()
        cancelled = await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.cancel_batch_predict_jobs("
                "CAST(:batch_id AS pg_catalog.uuid), "
                "CAST(:completed_at AS pg_catalog.timestamptz))"
            ),
            {"batch_id": batch_id, "completed_at": completed_at},
        )
        if cancelled:
            self._synchronize(
                update(AsyncJob).values(status="cancelled", completed_at=completed_at),
                cancelled,
            )
        await self._autoflush()
        deleted = await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.delete_batch_predict_jobs("
                "CAST(:batch_id AS pg_catalog.uuid))"
            ),
            {"batch_id": batch_id},
        )
        synchronize_owner_deleted_ids(self.db.sync_session, AsyncJob, deleted)
        return len(deleted)

    async def delete_terminal_jobs_before(self, cutoff, *, statuses):
        await self._autoflush()
        deleted = await self.db.scalar(
            text(
                "SELECT yoao_jobs_v1.delete_terminal_jobs_before("
                "CAST(:cutoff AS pg_catalog.timestamptz), "
                "CAST(:statuses AS pg_catalog.text[]))"
            ),
            {"cutoff": cutoff, "statuses": list(statuses)},
        )
        synchronize_owner_deleted_ids(self.db.sync_session, AsyncJob, deleted)
        return list(deleted)


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
    """创建 async_jobs 行。调用方负责 commit。

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
    """pending → running，记录 started_at。幂等。"""
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
    if job is None:
        raise ValueError("execution snapshot binding requires a Job row")
    bound = await db.scalar(
        text(
            "SELECT yoao_jobs_v1.bind_prediction_execution_snapshot("
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
    """Complete a live job; repeated or conflicting terminal writes are ignored."""
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
    """Fail a live job, preserving any existing terminal outcome.

    A supplied result records failure statistics for consumers such as batch retries.
    """
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
    """请求协作取消，调用方负责终态转换。"""
    job = await get_job(db, job_id)
    if job is not None:
        _manager(db).request_cancel(job)


async def find_by_celery_task_id(
    db: AsyncSession, celery_task_id: str
) -> AsyncJob | None:
    """Celery signals 兜底用，通过 task_id 反查 async_job。"""
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
    """上下文管理器：自动 create+running，退出时 complete/failed。

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
