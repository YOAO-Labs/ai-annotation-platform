"""Bind domain terminal rules without moving their state machines into jobs.

HTTP cancellation owns notification and commit. Signal fallback composes generic
state, domain reconciliation and notification here, but its caller still owns
commit and connection lifetime. Interrupted inference retains its own fencing and
non-terminal cancellation rules instead of taking the generic transition.
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.services import active_learning, async_job as jobs, sample_evaluations
from app.services import mask_repair, training_datasets
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import (
    JobTerminal,
    reconcile_dataset_quality,
    reconcile_dataset_quality_review,
)
from app.services.mask_formats import service as mask_formats
from app.services.mask_qc import service as mask_qc
from app.services.point_cloud_quality import service as point_cloud_quality

_DomainReconciler = Callable[[AsyncSession, JobTerminal], Awaitable[None]]


# Kinds without a separate domain ledger keep only the generic transition.
async def _reconcile_dataset_quality(db: AsyncSession, terminal: JobTerminal) -> None:
    from types import SimpleNamespace

    await reconcile_dataset_quality(
        db,
        SimpleNamespace(id=terminal.job_id, kind=terminal.kind),
        cancelled=terminal.cancelled,
        error=terminal.error,
    )


async def _reconcile_dataset_quality_review(
    db: AsyncSession, terminal: JobTerminal
) -> None:
    from types import SimpleNamespace

    await reconcile_dataset_quality_review(
        db,
        SimpleNamespace(id=terminal.job_id, kind=terminal.kind),
        cancelled=terminal.cancelled,
        error=terminal.error,
    )


_DOMAIN_RECONCILERS: dict[str, _DomainReconciler] = {
    "mask_qc": mask_qc.reconcile_job_terminal,
    "point_cloud_quality": point_cloud_quality.reconcile_job_terminal,
    "mask_repair": mask_repair.reconcile_job_terminal,
    "mask_repair_rollback": mask_repair.reconcile_job_terminal,
    "mask_format_import": mask_formats.reconcile_job_terminal,
    "training_dataset": training_datasets.reconcile_job_terminal,
    "dataset_quality": _reconcile_dataset_quality,
    "dataset_quality_review": _reconcile_dataset_quality_review,
}
_INTERRUPTION_HANDLERS = {
    "sample_evaluation": sample_evaluations.signal_interrupted,
    "active_learning_round": active_learning.signal_interrupted,
}


async def reconcile_domain_terminal(
    db: AsyncSession,
    job: AsyncJob,
    *,
    cancelled: bool,
    error: str | None = None,
) -> None:
    reconciler = _DOMAIN_RECONCILERS.get(job.kind)
    if reconciler is not None:
        await reconciler(db, JobTerminal(job.id, job.kind, cancelled, error))


async def apply_signal_terminal(
    db: AsyncSession,
    job: AsyncJob,
    *,
    cancelled: bool,
    error: str | None = None,
) -> bool:
    """Apply fallback in the caller's transaction; return whether to commit it."""
    if job.kind in {"video_tracker", "video_correction"} and (
        (job.payload or {}).get("video_execution_protocol")
        == "aap-video-execution-data/v1"
    ):
        # A broker signal has no execution capability. It cannot terminalize
        # the projection while a replacement may own the canonical tracker.
        return False
    interruption = _INTERRUPTION_HANDLERS.get(job.kind)
    if interruption is not None:
        await interruption(db, job, cancelled=cancelled)
        return True
    if cancelled:
        await jobs.mark_cancelled(db, job.id)
    else:
        if job.kind == "prediction_retry" and isinstance(
            (job.payload or {}).get("prediction_retry_request_digest"), str
        ):
            # Broker failure signals cannot prove the accepted retry lease.
            return False
        if job.status in {
            AsyncJobStatus.COMPLETED.value,
            AsyncJobStatus.FAILED.value,
            AsyncJobStatus.CANCELLED.value,
        }:
            return False
        await jobs.mark_failed(db, job.id, error=error or "")
    await reconcile_domain_terminal(db, job, cancelled=cancelled, error=error)
    await notify_job_terminal(db, job_id=job.id)
    return True
