"""One durable claim, bounded read, real settlement and fenced publication."""

import asyncio
import logging
import uuid

from fastapi import HTTPException

from app.services import managed_original_verification as service, storage_access
from app.services.async_job_notify import notify_job_terminal
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


def _publish(job_id, task_id):
    with celery_app.connection_for_write(
        connect_timeout=1,
        transport_options={
            "socket_connect_timeout": 1,
            "socket_timeout": 1,
            "max_retries": 0,
        },
    ) as connection:
        verify_managed_original.apply_async(
            args=[str(job_id)],
            task_id=task_id,
            queue="default",
            countdown=1,
            connection=connection,
            retry=False,
        )


async def dispatch(db, job):
    job_id, task_id, pending = job.id, job.celery_task_id, job.status == "pending"
    await db.commit()
    if not pending:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(_publish, job_id, task_id), 3)
    except Exception:
        # An uncertain publish can still arrive. The same task ID and durable
        # pending->running claim make explicit redispatch safe without a TTL.
        log.warning("Managed original verification dispatch unavailable job=%s", job_id)


def safe_error(exc):
    if isinstance(exc, TimeoutError):
        return "verification_timeout"
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        code = exc.detail.get("code")
    else:
        code = getattr(exc, "code", None)
    return (
        code
        if isinstance(code, str) and service._SAFE_CODE.fullmatch(code)
        else "verification_read_failed"
    )


async def execute(db, job_id, task_id):
    row = await service.claim(db, job_id, task_id)
    await db.commit()
    if row is None:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return False
    # Import only at execution: the shared reader owns SDK timeouts and byte
    # budgets, not image decoding or arbitrary historical endpoint construction.
    from app.services.managed_original_probe import ProbeInput, run_probe

    verification_id, revision = row.id, row.maintenance_revision
    request = ProbeInput(
        bucket=row.anchor["bucket"],
        object_key=row.anchor["object_key"],
        storage_identity=row.anchor["storage_identity"],
        sha256=row.expected_sha256,
        size_bytes=row.expected_size_bytes,
        endpoint_url=(row.configuration.get("saved_ref") or {}).get("endpoint_url"),
    )

    async def on_acquired(permit):
        await service.acquired(db, verification_id, permit)

    result, failure, cancelled = None, None, False
    try:
        options = await service.probe_options(db, row)
        result = await storage_access.run(
            db,
            uuid.UUID(row.anchor["location_id"]),
            "managed_original_verification",
            run_probe,
            request,
            expected_revision=revision,
            timeout=service.TIMEOUT_SECONDS + 10,
            on_acquired=on_acquired,
            **options,
        )
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:
        failure = safe_error(exc)

    # A failed callback may still own a short transaction. Do not publish from
    # its old snapshot or retain any of its locks across the final fence.
    async def finalize():
        await db.rollback()
        changed = await service.publish(
            db, job_id, task_id, result=result, error_code=failure, cancelled=cancelled
        )
        if changed:
            await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return changed

    finalizing = asyncio.create_task(finalize())
    changed = await storage_access.finish(finalizing)
    if cancelled:
        raise asyncio.CancelledError()
    return changed


async def _run(job_id, task_id):
    async with task_session() as db:
        return await execute(db, job_id, task_id)


@celery_app.task(
    bind=True, name="app.workers.managed_original_verification.verify_managed_original"
)
def verify_managed_original(self, job_id):
    return asyncio.run(_run(uuid.UUID(str(job_id)), str(self.request.id or "")))
