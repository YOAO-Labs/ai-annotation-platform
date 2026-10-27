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

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.services.jobs_lifecycle_data import job_lock_statement


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _locks_open(status: str) -> bool:
    """Whether a non-terminal domain row may still be reconciled."""

    return status in {"pending", "running"}


async def reconcile_mask_qc(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskQCRun`` through the QC owner command."""
    from app.services.mask_qc.service import reconcile_job_terminal

    await reconcile_job_terminal(
        db,
        JobTerminal(
            job_id=job.id,
            kind=job.kind,
            cancelled=cancelled,
            error=error,
        ),
    )


async def reconcile_point_cloud_quality(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``PointCloudQualityRun`` through the spatial owner command."""
    from app.services.point_cloud_quality.service import reconcile_job_terminal

    await reconcile_job_terminal(
        db,
        JobTerminal(
            job_id=job.id,
            kind=job.kind,
            cancelled=cancelled,
            error=error,
        ),
    )


def committed_repair_units(batch: MaskRepairBatch) -> int:
    return sum(
        1
        for section in ("shards", "candidates")
        for value in ((batch.result_json or {}).get(section) or {}).values()
        if isinstance(value, dict)
        and value.get("status") in {"completed", "dispatch_pending"}
    )


async def reconcile_mask_repair(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskRepairBatch`` through the repair owner command.

    Forward repair preserves committed shards, candidates and child dispatch
    intents as ``partial``. Interrupted rollback preserves its checkpoints and
    becomes ``rollback_failed`` so another attempt can continue them. The repair
    owner decides both, so this module never reads or writes its batch directly.
    """
    from app.services.mask_repair import reconcile_job_terminal

    await reconcile_job_terminal(
        db,
        JobTerminal(
            job_id=job.id,
            kind=job.kind,
            cancelled=cancelled,
            error=error,
        ),
    )


async def reconcile_mask_format_import(
    db: AsyncSession, job: AsyncJob, *, cancelled: bool, error: str | None
) -> None:
    """Reconcile a ``MaskFormatImport`` through the transfer owner command."""
    from app.services.mask_formats.service import reconcile_job_terminal

    await reconcile_job_terminal(
        db,
        JobTerminal(
            job_id=job.id,
            kind=job.kind,
            cancelled=cancelled,
            error=error,
        ),
    )


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


async def reconcile_training_dataset(db, job, *, cancelled, error):
    from app.db.models.training_dataset import TrainingDatasetVersion

    version = (
        await db.execute(
            select(TrainingDatasetVersion)
            .where(TrainingDatasetVersion.async_job_id == job.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is not None and _locks_open(version.status):
        _finish_training_dataset(version, cancelled=cancelled)


def _finish_training_dataset(version, *, cancelled):
    version.status = "cancelled" if cancelled else "failed"
    version.error_code = "cancelled" if cancelled else "worker_failed"
    version.error_message = (
        "训练数据构建已取消" if cancelled else "构建进程异常终止，可重试同一快照"
    )
    version.completed_at = _now()


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
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.services import async_job as jobs

    expected = _training_execution(task_args, task_kwargs)
    if expected is None or expected[1] != job_id:
        return False
    version_id, expected_job_id, attempt_id = expected
    version = await db.scalar(
        select(TrainingDatasetVersion)
        .where(TrainingDatasetVersion.id == version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        version is None
        or version.async_job_id != expected_job_id
        or version.attempt_id != attempt_id
        or not _locks_open(version.status)
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
        or not _locks_open(job.status)
    ):
        return False
    if cancelled:
        await jobs.mark_cancelled(db, job.id)
    else:
        await jobs.mark_failed(db, job.id, error=error or "")
    _finish_training_dataset(version, cancelled=cancelled)
    return True


DOMAIN_TERMINAL_RECONCILERS["training_dataset"] = reconcile_training_dataset


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


async def reconcile_dataset_quality(db, job, *, cancelled, error):
    from app.db.models.dataset_quality import DatasetQualityRun
    from app.services.dataset_quality import now

    run = await db.scalar(
        select(DatasetQualityRun)
        .where(DatasetQualityRun.async_job_id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is not None and _locks_open(run.status):
        run.status = "cancelled" if cancelled else "failed"
        run.error_code = "cancelled" if cancelled else "worker_failed"
        run.completed_at, run.lease_until = now(), None
        run.generation += 1


DOMAIN_TERMINAL_RECONCILERS["dataset_quality"] = reconcile_dataset_quality


async def reconcile_dataset_quality_review(db, job, *, cancelled, error):
    from app.db.models.dataset_quality_review import DatasetQualityReviewBatch
    from app.services.dataset_quality_review import now

    batch = await db.scalar(
        select(DatasetQualityReviewBatch)
        .where(DatasetQualityReviewBatch.async_job_id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is not None and _locks_open(batch.status):
        batch.status = "cancelled" if cancelled else "failed"
        batch.error_code = "cancelled" if cancelled else "worker_failed"
        batch.completed_at, batch.lease_until = now(), None
        batch.generation += 1


DOMAIN_TERMINAL_RECONCILERS["dataset_quality_review"] = reconcile_dataset_quality_review


@dataclass(frozen=True, slots=True)
class JobTerminal:
    """Transport-independent terminal facts consumed by domain adapters."""

    job_id: UUID
    kind: str
    cancelled: bool
    error: str | None = None
