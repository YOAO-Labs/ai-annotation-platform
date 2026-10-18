"""Celery signal fallback that flips crashed / revoked async jobs terminal.

Design:

* This is a **fallback only**.  On the normal path ``batch_predict`` /
  ``video_tracker`` / ``predictions_import`` and the mask / quality jobs mark
  their own terminal state; these handlers only cover the paths a worker did
  not reach (process crash, Celery revoke, an uncaught raise).
* The generic ``async_jobs`` row is looked up through ``celery_task_id``; a
  task without a matching ``async_jobs`` row (``ml_health`` / ``cleanup`` and
  other high-frequency housekeeping) is silently ignored.
* Application workflow :mod:`app.application.job_terminal` binds domain ledger
  handlers and interrupted-inference rules. Domains own special terminal values
  such as ``partial`` and ``rollback_failed``; the signal never flattens them.
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

from app.application.job_terminal import apply_signal_terminal
from app.config import settings
from app.services import async_job as async_job_svc

log = logging.getLogger(__name__)

_ASSET_CLEANUP_TASK = "app.workers.asset_storage_cleanup.retry_asset_storage_deletions"


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
            if await apply_signal_terminal(db, aj, cancelled=cancelled, error=error):
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
    # Assets cleanup owns a durable outbox and has no AsyncJob row.
    if not task_id or getattr(sender, "name", None) == _ASSET_CLEANUP_TASK:
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
    task = getattr(request, "task", None)
    task_name = getattr(task, "name", task) or getattr(request, "name", None)
    if not task_id or task_name == _ASSET_CLEANUP_TASK:
        return
    try:
        asyncio.run(_mark_cancelled(task_id))
    except Exception:
        log.exception("async_jobs signal _on_task_revoked failed")
