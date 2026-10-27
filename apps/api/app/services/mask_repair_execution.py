"""Fence repair attempts and serialize their durable results with cancellation."""

from dataclasses import dataclass
import uuid

from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import reconcile_mask_repair
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.mask_repair import (
    begin_repair_execution,
    begin_repair_rollback,
    canonical_digest,
    load_repair_batch,
)


async def authorize_repair_task(db, task, actor, *, ai=False):
    """Read-only repair policy, shared before I/O and under final owner locks."""
    from app.services import task_access
    from app.services.content_snapshot import ContentError
    from app.services.identity_account_lock_data import IdentityAccountLockData
    from app.services.labeling_core_read_relations import get_core_row, batch_row
    from app.services.project_access import (
        get_workspace_project,
        resolve_project_access,
    )
    from app.services.project_write_guard import assert_phase_write_allowed
    from app.services.raster_mask_storage import assert_raster_mask_write_enabled
    from app.services.task_lock import TaskLockService

    actor = await IdentityAccountLockData(db).read_actor_status(actor.id)
    project = await get_workspace_project(
        db, actor_id=actor.id, project_id=task.project_id, populate_existing=True
    )
    if actor is None or not actor.is_active or project is None:
        raise ContentError("repair_actor_unavailable", 403)
    if task.batch_id:
        await get_core_row(db, batch_row, task.batch_id, populate_existing=True)
    access = await resolve_project_access(db, actor_id=actor.id, project=project)
    if not access.has("review.write"):
        raise ContentError("repair_authority_revoked", 403)
    await task_access.assert_task_visible(db, task, actor.id, access=access)
    await assert_phase_write_allowed(
        db, task, actor, action="repair mask", lock_membership=False
    )
    if any(
        lock.user_id != actor.id
        for lock in await TaskLockService(db).active_locks(task.id, cleanup=False)
    ):
        raise ContentError("task_lock_conflict")
    if task.file_type == "image":
        assert_raster_mask_write_enabled(project)
    if ai and project.ai_interactive_enabled is False:
        raise ContentError("ai_interactive_disabled", 403)
    return actor


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
    return await load_repair_batch(db, batch_id=batch_id, lock=True)


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
    if not celery_task_id:
        return None
    if job_id is None:
        batch = await load_repair_batch(db, batch_id=uuid.UUID(str(batch_id)))
        if batch is None:
            return None
        job_id = batch.rollback_async_job_id if rollback else batch.async_job_id
        if job_id is None:
            return None
    job = await db.scalar(
        job_lock_statement(
            uuid.UUID(str(job_id)), mode="update", populate_existing=True
        )
    )
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
    if rollback:
        await begin_repair_rollback(
            db, batch_id=batch.id, celery_task_id=celery_task_id
        )
    else:
        await begin_repair_execution(
            db, batch_id=batch.id, celery_task_id=celery_task_id
        )
    return RepairAttempt(
        batch.id, job.id, job.user_id, celery_task_id, batch.plan_digest, rollback
    )


async def lock_attempt(db, attempt):
    job = await db.scalar(
        job_lock_statement(attempt.job_id, mode="update", populate_existing=True)
    )
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
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
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
