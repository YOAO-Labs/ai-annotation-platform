"""Domain terminal reconciliation for user-visible async jobs.

Normal worker completion / failure paths update their own domain ledger
explicitly (``mask_qc``, ``point_cloud_quality``, ``mask_repair``,
``mask_format_import``).  This module owns the *reconciliation* step used when
the generic ``async_jobs`` row reaches a terminal state through a path that did
not run the worker's own domain update:

* the Celery signal fallback in :mod:`app.workers.signals` (worker crash,
  revoke, uncaught raise);
* the hard-cancel request path for jobs whose worker cannot be asked to finish
  the transition itself.

Only the domain ledger is reconciled here.  The caller owns the generic
``AsyncJob`` transition, the terminal notification and the transaction commit,
because those differ per path (the signal fallback notifies pre-commit through
:func:`app.services.async_job_notify.notify_job_terminal`, the request path
commits immediately).

Each job kind keeps its own rule on purpose.  A cancelled masked-format import
with committed items becomes ``partial``; a failed rollback job keeps
``rollback_failed``; a cancelled repair with completed shards becomes
``partial``.  There is deliberately no single "set everything to failed"
helper, because flattening those values would erase the difference between
"cancelled after committing part of the work" and "failed cleanly".
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.mask_qc import MaskQCRun
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.point_cloud_quality import PointCloudQualityRun


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _locks_open(status: str) -> bool:
    """Whether a non-terminal domain row may still be reconciled."""

    return status in {"pending", "running"}


async def reconcile_mask_qc(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskQCRun`` when its generic job turned terminal."""

    run = (
        await db.execute(
            select(MaskQCRun).where(MaskQCRun.async_job_id == job.id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or not _locks_open(run.status):
        return
    if cancelled:
        run.status = "cancelled"
    else:
        run.status = "failed"
        run.error_message = (error or "")[:4000]
    run.completed_at = _now()


async def reconcile_point_cloud_quality(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``PointCloudQualityRun`` when its generic job turned terminal."""

    run = (
        await db.execute(
            select(PointCloudQualityRun)
            .where(PointCloudQualityRun.async_job_id == job.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or not _locks_open(run.status):
        return
    if cancelled:
        run.status = "cancelled"
    else:
        run.status = "failed"
        run.error_message = (error or "")[:4000]
    run.completed_at = _now()


def _completed_repair_shards(batch: MaskRepairBatch) -> int:
    return sum(
        1
        for value in ((batch.result_json or {}).get("shards") or {}).values()
        if isinstance(value, dict) and value.get("status") == "completed"
    )


def _committed_import_items(batch: MaskFormatImport) -> int:
    return sum(
        1
        for value in ((batch.result_json or {}).get("items") or {}).values()
        if isinstance(value, dict) and value.get("status") == "committed"
    )


async def reconcile_mask_repair(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskRepairBatch`` for ``mask_repair`` / rollback jobs.

    A cancelled forward repair keeps an already-committed shard as ``partial``.
    The fallback cancellation path intentionally does not reconcile a rollback
    job: rollback cancellation is driven by the requested-repair lifecycle, and
    the failed path is the one that owns ``rollback_failed``.
    """

    if cancelled:
        if job.kind != "mask_repair":
            return
        batch = (
            await db.execute(
                select(MaskRepairBatch)
                .where(MaskRepairBatch.async_job_id == job.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if batch is None or not _locks_open(batch.status):
            return
        batch.status = "partial" if _completed_repair_shards(batch) else "cancelled"
        batch.completed_at = _now()
        return

    condition = (
        MaskRepairBatch.rollback_async_job_id == job.id
        if job.kind == "mask_repair_rollback"
        else MaskRepairBatch.async_job_id == job.id
    )
    batch = (
        await db.execute(select(MaskRepairBatch).where(condition).with_for_update())
    ).scalar_one_or_none()
    if batch is None:
        return
    batch.status = "rollback_failed" if job.kind == "mask_repair_rollback" else "failed"
    batch.completed_at = _now()


async def reconcile_mask_format_import(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskFormatImport`` when its generic job turned terminal."""

    batch = (
        await db.execute(
            select(MaskFormatImport)
            .where(MaskFormatImport.async_job_id == job.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is None:
        return
    if cancelled:
        if not _locks_open(batch.status):
            return
        batch.status = "partial" if _committed_import_items(batch) else "cancelled"
    else:
        batch.status = "failed"
    batch.completed_at = _now()


#: Closed dispatch from ``AsyncJob.kind`` to its domain reconciler.  Kinds with
#: no domain ledger (batch_predict, video_tracker, create_tasks, ...) are absent
#: and only ever reconcile the generic row.
DOMAIN_TERMINAL_RECONCILERS = {
    "mask_qc": reconcile_mask_qc,
    "point_cloud_quality": reconcile_point_cloud_quality,
    "mask_repair": reconcile_mask_repair,
    "mask_repair_rollback": reconcile_mask_repair,
    "mask_format_import": reconcile_mask_format_import,
}


async def reconcile_domain_terminal(
    db: AsyncSession,
    job: AsyncJob,
    *,
    cancelled: bool,
    error: str | None = None,
) -> None:
    """Apply the domain terminal rule for ``job``'s kind, if it has one."""

    reconciler = DOMAIN_TERMINAL_RECONCILERS.get(job.kind)
    if reconciler is None:
        return
    await reconciler(db, job, cancelled=cancelled, error=error)
