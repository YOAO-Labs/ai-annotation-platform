"""Celery signal fallback that flips crashed / revoked async jobs terminal.

Design:

* This is a **fallback only**.  On the normal path ``batch_predict`` /
  ``video_tracker`` / ``predictions_import`` and the mask / quality jobs mark
  their own terminal state; these handlers only cover the paths a worker did
  not reach (process crash, Celery revoke, an uncaught raise).
* The generic ``async_jobs`` row is looked up through ``celery_task_id``; a
  task without a matching ``async_jobs`` row (``ml_health`` / ``cleanup`` and
  other high-frequency housekeeping) is silently ignored.
* A job-kind-specific domain ledger is reconciled by
  :mod:`app.services.async_job_terminal`; the special terminal values
  (``partial``, ``rollback_failed``) are owned there, not flattened here.
* Signal callbacks are synchronous and cannot ``await``, so each callback
  drives the async work with ``asyncio.run`` (which builds and disposes an
  ``AsyncEngine`` per call).  The shared skeleton is connect -> find job ->
  apply terminal state -> reconcile the domain ledger -> notify -> commit ->
  dispose.
"""

from __future__ import annotations

import asyncio
import logging

from celery.signals import task_failure, task_revoked
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models.async_job import AsyncJobStatus
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import reconcile_domain_terminal

log = logging.getLogger(__name__)


async def _apply_terminal(
    celery_task_id: str,
    *,
    cancelled: bool,
    error: str | None = None,
) -> None:
    """Run the shared fallback skeleton for one celery task id."""

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    try:
        async with SessionLocal() as db:
            aj = await async_job_svc.find_by_celery_task_id(db, celery_task_id)
            if aj is None:
                return
            if cancelled:
                await async_job_svc.mark_cancelled(db, aj.id)
            else:
                # A late failure signal must never overwrite a job that already
                # reached a terminal state on its own path.
                if aj.status in {
                    AsyncJobStatus.COMPLETED.value,
                    AsyncJobStatus.FAILED.value,
                    AsyncJobStatus.CANCELLED.value,
                }:
                    return
                await async_job_svc.mark_failed(db, aj.id, error=error or "")
            await reconcile_domain_terminal(db, aj, cancelled=cancelled, error=error)
            await notify_job_terminal(db, job_id=aj.id)
            await db.commit()
    finally:
        await engine.dispose()


async def _mark_failed(celery_task_id: str, error: str) -> None:
    await _apply_terminal(celery_task_id, cancelled=False, error=error)


async def _mark_cancelled(celery_task_id: str) -> None:
    await _apply_terminal(celery_task_id, cancelled=True)


@task_failure.connect
def _on_task_failure(  # noqa: ARG001
    sender=None,
    task_id=None,
    exception=None,
    einfo=None,
    **kwargs,
):
    """Fallback: an exception the task body did not catch."""
    if not task_id:
        return
    try:
        error = f"{type(exception).__name__}: {exception}" if exception else "unknown"
        asyncio.run(_mark_failed(task_id, error))
    except Exception:
        log.exception("async_jobs signal _on_task_failure failed")


@task_revoked.connect
def _on_task_revoked(  # noqa: ARG001
    sender=None,
    request=None,
    terminated=None,
    signum=None,
    expired=None,
    **kwargs,
):
    """Fallback: Celery revoke / terminate / expired."""
    task_id = getattr(request, "id", None) if request else None
    if not task_id:
        return
    try:
        asyncio.run(_mark_cancelled(task_id))
    except Exception:
        log.exception("async_jobs signal _on_task_revoked failed")
