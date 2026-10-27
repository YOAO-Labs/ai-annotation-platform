"""Maintain audit partitions and publish independently verified cold archives."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from sqlalchemy import select, text

from app.config import settings
from app.services.audit_partition_service import AuditPartitionService
from app.workers._db import task_session
from app.workers.celery_app import celery_app


log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.audit_partition.ensure_future_audit_partitions")
def ensure_future_audit_partitions(months_ahead: int = 3) -> dict:
    return asyncio.run(_ensure_async(months_ahead))


async def _ensure_async(months_ahead: int) -> dict:
    async with task_session() as db:
        created = await AuditPartitionService.ensure_future_partitions(
            db, months_ahead=months_ahead
        )
        await db.commit()
    log.info("ensure_future_audit_partitions: created=%d %s", len(created), created)
    return {"created": created}


@celery_app.task(
    bind=True,
    name="app.workers.audit_partition.archive_old_audit_partitions",
)
def archive_old_audit_partitions(self) -> dict:
    retain = settings.audit_retention_months
    return asyncio.run(_archive_async(retain, getattr(self.request, "id", None)))


async def _archive_async(retain_months: int, celery_task_id: str | None = None) -> dict:
    """Commit job ownership before I/O and preserve each published month."""
    from app.db.models.async_job import AsyncJob
    from app.db.models.user import User
    from app.services import async_job as async_job_svc
    from app.services import storage_access
    from app.services.async_job_notify import notify_job_terminal

    async with task_session() as db:
        if celery_task_id:
            key = int.from_bytes(
                hashlib.sha256(celery_task_id.encode()).digest()[:4],
                "big",
                signed=True,
            )
            await db.execute(
                text("SELECT pg_advisory_xact_lock(727279, :key)"), {"key": key}
            )
            previous = list(
                await db.scalars(
                    select(AsyncJob)
                    .where(
                        AsyncJob.kind == "audit_archive",
                        AsyncJob.celery_task_id == celery_task_id,
                    )
                    .order_by(AsyncJob.created_at, AsyncJob.id)
                    .limit(2)
                )
            )
            if len(previous) > 1:
                raise RuntimeError("audit_archive_job_identity_conflict")
            if previous:
                # A redelivery cannot prove the original physical attempt ended.
                job = previous[0]
                result = {
                    **(job.result or {}),
                    "job_id": str(job.id),
                    "status": job.status,
                    "deduplicated": True,
                }
                await db.commit()
                return result
        job = await async_job_svc.create_job(
            db,
            kind="audit_archive",
            payload={"retain_months": retain_months},
            celery_task_id=celery_task_id,
        )
        job_id = job.id
        await async_job_svc.mark_running(db, job_id)
        await db.commit()

        async def terminal(*, error=None, cancelled=False, completed=None):
            await db.rollback()
            # Notifications take a users FK lock. Match cancellation's User ->
            # Job order before locking a historically user-owned archive job.
            owner_id = await db.scalar(
                select(AsyncJob.user_id).where(AsyncJob.id == job_id)
            )
            if owner_id is not None:
                await db.scalar(
                    select(User.id)
                    .where(User.id == owner_id)
                    .with_for_update(read=True, key_share=True)
                )
            current = await async_job_svc.lock_job(db, job_id)
            if current is None:
                raise RuntimeError("audit_archive_job_missing")
            if current.user_id is not None and current.user_id != owner_id:
                raise RuntimeError("audit_archive_job_owner_changed")
            if current.status in {"pending", "running"}:
                if cancelled or (current.payload or {}).get("cancel_requested"):
                    await async_job_svc.mark_cancelled(db, job_id)
                elif error is not None:
                    await async_job_svc.mark_failed(db, job_id, error=error)
                else:
                    await async_job_svc.mark_complete(db, job_id, result=completed)
                await notify_job_terminal(db, job_id=job_id)
            result = {
                **(current.result or {}),
                "job_id": str(job_id),
                "status": current.status,
            }
            await db.commit()
            return result

        try:
            result = await AuditPartitionService.archive_old_partitions(
                db, retain_months=retain_months, job_id=job_id
            )
            result = await terminal(completed=result)
        except asyncio.CancelledError:
            # Source I/O has already drained; repeated cancellation cannot abandon
            # the transaction that records which months actually committed.
            await storage_access.finish(asyncio.create_task(terminal(cancelled=True)))
            raise
        except Exception as exc:
            code = getattr(exc, "code", "audit_archive_failed")
            if not isinstance(code, str) or not re.fullmatch(
                r"audit_archive_[a-z0-9_]{1,100}", code
            ):
                code = "audit_archive_failed"
            try:
                result = await terminal(
                    error=code, cancelled=code == "audit_archive_cancelled"
                )
            except Exception:
                log.error("audit_archive terminal unconfirmed: job=%s", job_id)
                raise RuntimeError("audit_archive_commit_unconfirmed") from None
            if result["status"] in {"cancelled", "completed"}:
                return result
            raise RuntimeError(code) from None
    log.info("audit archive finished: job=%s status=%s", job_id, result["status"])
    return result
