"""Bind domain terminal rules without moving their state machines into jobs.

HTTP cancellation owns notification and commit. Signal fallback composes generic
state, domain reconciliation and notification here, but its caller still owns
commit and connection lifetime. Interrupted inference retains its own fencing and
non-terminal cancellation rules instead of taking the generic transition.
"""

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.services import (
    active_learning,
    jobs_lifecycle_data as jobs,
    sample_evaluations,
)
from app.services import (
    dataset_quality,
    dataset_quality_review,
    mask_repair,
    training_dataset_data,
    training_datasets,
)
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import JobTerminal
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.mask_formats import service as mask_formats
from app.services.mask_qc import service as mask_qc
from app.services.point_cloud_quality import service as point_cloud_quality

_DomainReconciler = Callable[[AsyncSession, JobTerminal], Awaitable[None]]


async def _reconcile_dataset_quality(db: AsyncSession, terminal: JobTerminal) -> None:
    await dataset_quality.reconcile_terminal(
        db, terminal.job_id, cancelled=terminal.cancelled
    )


async def _reconcile_dataset_quality_review(
    db: AsyncSession, terminal: JobTerminal
) -> None:
    await dataset_quality_review.reconcile_terminal(
        db, terminal.job_id, cancelled=terminal.cancelled
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


def _training_execution(args, kwargs):
    """Use the actual task envelope, never infer a signal's attempt from live rows."""
    names = ("version_id", "job_id", "attempt_id")
    args = () if args is None else args
    kwargs = {} if kwargs is None else kwargs
    if (
        not isinstance(args, (list, tuple))
        or len(args) > len(names)
        or not isinstance(kwargs, dict)
        or set(kwargs) != set(names[len(args) :])
    ):
        return None
    try:
        return tuple(
            uuid.UUID(str(value))
            for value in (*args, *(kwargs[n] for n in names[len(args) :]))
        )
    except (ValueError, TypeError, AttributeError):
        return None


async def signal_training_dataset(
    db, job_id, celery_task_id, *, task_args=None, task_kwargs=None, cancelled, error
):
    """Match the worker's Version -> Job lock order and exact execution fence."""
    expected = _training_execution(task_args, task_kwargs)
    if expected is None or expected[1] != job_id:
        return False
    version_id, expected_job_id, attempt_id = expected
    version = await training_dataset_data.read_version_facts(db, version_id, lock=True)
    if (
        version is None
        or version.async_job_id != expected_job_id
        or version.attempt_id != attempt_id
        or version.status not in {"pending", "running"}
    ):
        return False
    job = await db.scalar(
        job_lock_statement(expected_job_id, mode="update", populate_existing=True)
    )
    if (
        job is None
        or job.kind != "training_dataset"
        or job.celery_task_id != celery_task_id
        or job.project_id != version.project_id
        or job.user_id != version.creator_id
        or not isinstance(job.payload, dict)
        or job.payload.get("training_dataset_id") != str(version.id)
        or job.status not in {"pending", "running"}
    ):
        return False
    if cancelled:
        await jobs.mark_cancelled(db, job.id)
    else:
        await jobs.mark_failed(db, job.id, error=error or "")
    await training_datasets.finish_interrupted_version(
        db, version, expected_job_id, cancelled=cancelled
    )
    return True
