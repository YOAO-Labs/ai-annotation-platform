"""Fence repair attempts and serialize their durable results with cancellation."""

from dataclasses import dataclass
import uuid

from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import reconcile_mask_repair
from app.services.mask_repair import canonical_digest


class RepairAttemptStopped(Exception):
    """This delivery no longer owns the batch's current attempt."""


@dataclass(frozen=True)
class RepairAttempt:
    batch_id: uuid.UUID
    job_id: uuid.UUID
    actor_id: uuid.UUID
    celery_task_id: str
    plan_digest: str
    rollback: bool = False

    @property
    def kind(self):
        return "mask_repair_rollback" if self.rollback else "mask_repair"


async def lock_batch(db, batch_id):
    return await db.scalar(
        select(MaskRepairBatch)
        .where(MaskRepairBatch.id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _matches(batch, job, *, rollback=False):
    return (
        batch is not None
        and job is not None
        and (batch.rollback_async_job_id if rollback else batch.async_job_id) == job.id
        and job.kind == ("mask_repair_rollback" if rollback else "mask_repair")
        and job.project_id == batch.project_id
        and (job.payload or {}).get("mask_repair_batch_id") == str(batch.id)
    )


async def start_attempt(db, batch_id, job_id, celery_task_id, *, rollback=False):
    # Old messages can only resolve their original recorded delivery, never a
    # batch's replacement pointer after resume.
    if job_id is None:
        job_id = (
            await db.scalar(
                select(AsyncJob.id).where(
                    AsyncJob.celery_task_id == celery_task_id,
                    AsyncJob.kind
                    == ("mask_repair_rollback" if rollback else "mask_repair"),
                )
            )
            if celery_task_id
            else None
        )
    if job_id is None:
        return None
    job = await jobs.lock_job(db, uuid.UUID(str(job_id)))
    batch = await lock_batch(db, uuid.UUID(str(batch_id)))
    if (
        not _matches(batch, job, rollback=rollback)
        or job.celery_task_id != celery_task_id
        or job.status != "pending"
        or batch.status != ("rolling_back" if rollback else "pending")
        or not job.user_id
    ):
        return None
    if (job.payload or {}).get("cancel_requested"):
        await jobs.mark_cancelled(db, job.id)
        await reconcile_mask_repair(db, job, cancelled=True, error=None)
        await notify_job_terminal(db, job_id=job.id)
        return None
    if canonical_digest(batch.plan_json) != batch.plan_digest:
        raise RuntimeError("repair_plan_digest_conflict")
    await jobs.mark_running(db, job.id, celery_task_id=celery_task_id)
    if not rollback:
        batch.status = "running"
    return RepairAttempt(
        batch.id, job.id, job.user_id, celery_task_id, batch.plan_digest, rollback
    )


async def lock_attempt(db, attempt):
    job = await jobs.lock_job(db, attempt.job_id)
    batch = await lock_batch(db, attempt.batch_id)
    if (
        not _matches(batch, job, rollback=attempt.rollback)
        or job.celery_task_id != attempt.celery_task_id
        or job.user_id != attempt.actor_id
        or job.status != "running"
        or batch.status != ("rolling_back" if attempt.rollback else "running")
        or batch.plan_digest != attempt.plan_digest
        or canonical_digest(batch.plan_json) != attempt.plan_digest
    ):
        raise RepairAttemptStopped()
    if (job.payload or {}).get("cancel_requested"):
        await jobs.mark_cancelled(db, job.id, result={"reason": "cancelled_by_user"})
        await reconcile_mask_repair(db, job, cancelled=True, error=None)
        await notify_job_terminal(db, job_id=job.id)
        await db.commit()
        raise RepairAttemptStopped()
    return batch, job


async def release_unpublished(db, batch_id, job_id, *, rollback=False):
    job = await jobs.lock_job(db, job_id)
    batch = await lock_batch(db, batch_id)
    if not _matches(batch, job, rollback=rollback) or job.status != "pending":
        return False
    await jobs.mark_failed(db, job.id, error="repair_dispatch_failed")
    await reconcile_mask_repair(
        db, job, cancelled=False, error="repair_dispatch_failed"
    )
    await notify_job_terminal(db, job_id=job.id)
    return True


def progress_counts(batch):
    result = batch.result_json or {}
    shards = (batch.plan_json or {}).get("shards") or []
    candidates = [
        index
        for index, item in enumerate((batch.plan_json or {}).get("items") or [])
        if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
    ]
    statuses = [
        ((result.get("shards") or {}).get(str(row["id"])) or {}).get("status")
        for row in shards
    ]
    statuses += [
        ((result.get("candidates") or {}).get(str(index)) or {}).get("status")
        for index in candidates
    ]
    success = sum(value == "completed" for value in statuses)
    return success, len(statuses) - success, len(statuses)
