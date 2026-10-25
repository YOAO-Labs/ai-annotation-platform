"""Fenced single-version deletion, late receipt persistence and DB-only reconciliation."""

import asyncio
import logging
import uuid

from fastapi import HTTPException

from app.services import managed_original_reclaim as service, storage_access
from app.services import prediction_inputs as drained
from app.services.async_job_notify import notify_job_terminal
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


def _publish(job_id, task_id, *, reference=False):
    task = reconcile_references if reference else reclaim_managed_original
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        task.apply_async(
            args=[str(job_id)],
            task_id=task_id,
            queue="default",
            countdown=1,
            connection=connection,
            retry=False,
        )


async def dispatch(db, job):
    identity, task_id, pending, reference = (
        job.id,
        job.celery_task_id,
        job.status == "pending",
        job.kind == service.REFERENCE_KIND,
    )
    await db.commit()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_publish, identity, task_id, reference=reference), 3
            )
        except Exception:
            log.warning("Managed original task dispatch unavailable job=%s", identity)


def safe_error(exc):
    if isinstance(exc, TimeoutError):
        return "reclaim_timeout"
    code = (
        exc.detail.get("code")
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
        else getattr(exc, "code", None)
    )
    return (
        code
        if isinstance(code, str) and service._SAFE_CODE.fullmatch(code)
        else "reclaim_failed"
    )


async def execute(db, job_id, task_id):
    row = await service.claim(db, job_id, task_id)
    await db.commit()
    if row is None:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return False
    from app.services.managed_original_reclaim_io import ReclaimInput, run_reclaim

    loop = asyncio.get_running_loop()
    attempt_id, target = row.id, dict(row.target)
    recovered = await service.recover_may_delete(db, row)
    await db.rollback()
    candidate = ReclaimInput(
        bucket=target["bucket"],
        object_key=target["object_key"],
        version_id=target["version_id"],
        storage_identity=target["storage_identity"],
        sha256=target["sha256"],
        size_bytes=target["size_bytes"],
        endpoint_url=target["endpoint_url"],
        recover_may_delete=recovered,
    )
    permit, failure, holder = None, None, {}

    def checkpoint(phase, payload):
        future = asyncio.run_coroutine_threadsafe(
            service.checkpoint(db, attempt_id, task_id, phase, payload), loop
        )
        # This callback is part of the physical thread. It must settle before
        # that thread can proceed or finish, including while its owner cancels.
        future.result()

    def native(options):
        result = run_reclaim(candidate, checkpoint=checkpoint, **options)
        holder["result"] = result
        return result

    try:
        permit = await service.acquire(db, attempt_id, task_id)

        # Use the saved target, not an expired ORM row after rollback.
        class Saved:
            pass

        saved = Saved()
        saved.target = target
        options = await service.io_options(db, saved)
        await db.rollback()
        await drained.settled_io(native, options)
    except BaseException as exc:
        failure = exc
    finally:
        if permit is not None and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            try:
                await drained._async(permit._settle())
            except BaseException as late:
                if failure is None or not isinstance(late, Exception):
                    if failure is not None:
                        drained._uncertainty(late, failure)
                    failure = late
                else:
                    drained._uncertainty(failure, late)
        if permit is not None and not permit._settled:
            failure = storage_access.mark_unsettled(
                failure or RuntimeError("reclaim operation unsettled")
            )

    async def finalize():
        await db.rollback()
        changed = await service.publish(
            db,
            job_id,
            task_id,
            result=holder.get("result"),
            error_code=safe_error(failure) if failure else None,
            cancelled=isinstance(failure, asyncio.CancelledError),
            settled=permit is None or permit._settled,
        )
        if changed:
            await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return changed

    changed = await drained._async(finalize())
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return changed


async def _run(job_id, task_id, *, reference=False):
    async with task_session() as db:
        if reference:
            return await service.reconcile(db, job_id, task_id)
        return await execute(db, job_id, task_id)


@celery_app.task(
    bind=True, name="app.workers.managed_original_reclaim.reclaim_managed_original"
)
def reclaim_managed_original(self, job_id):
    return asyncio.run(_run(uuid.UUID(str(job_id)), str(self.request.id or "")))


@celery_app.task(
    bind=True, name="app.workers.managed_original_reclaim.reconcile_references"
)
def reconcile_references(self, job_id):
    return asyncio.run(
        _run(uuid.UUID(str(job_id)), str(self.request.id or ""), reference=True)
    )


async def _schedule():
    async with task_session() as db:
        job = await service.start_reconciliation(db)
        await dispatch(db, job)
        return str(job.id)


@celery_app.task(
    name="app.workers.managed_original_reclaim.schedule_reference_reconciliation"
)
def schedule_reference_reconciliation():
    return asyncio.run(_schedule())
