"""Read-only source scan with one durable claim and real native settlement."""

import asyncio
import io
import logging
import threading
import time
import uuid

from botocore.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.storage_migration_inventory import REPORT_LIMIT
from app.services import storage_access, storage_migration_inventories as service
from app.services import storage_migration_source_read as source_read
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import StorageService
from app.services.storage_migration_inventory import (
    InventoryLimits,
    InventoryStopped,
    write_inventory,
)
from app.services.storage_migration_observations import collect_observations
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
        inventory_configured_source.apply_async(
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
        log.warning("Storage migration inventory dispatch unavailable job=%s", job_id)


class ReportSink(io.BytesIO):
    """Retain bytes only after the real sink has closed successfully."""

    value = None

    def close(self):
        if self.closed:
            return
        value = self.getvalue()
        super().close()
        self.value = value


def scan(source, limits, handling, observations, checkpoint):
    """The registered thread owns client, every Body and output sink lifetime."""
    checkpoint()
    configured = StorageService(
        client_config=Config(
            connect_timeout=min(10, limits["read_timeout_seconds"]),
            read_timeout=limits["read_timeout_seconds"],
            retries={"total_max_attempts": 1},
        )
    )
    with storage_access.closing_registered(configured.client):
        actual = {
            purpose: getattr(configured, attribute)
            for purpose, attribute in service.PURPOSE_BUCKET_ATTRIBUTES.items()
        }
        if actual != source[
            "purpose_buckets"
        ] or configured.client.meta.endpoint_url.rstrip("/") != source[
            "endpoint"
        ].rstrip("/"):
            raise InventoryStopped("inventory_source_changed")
        sink = ReportSink()
        with storage_access.closing_registered(sink):
            write_inventory(
                None,
                source_read.SourceRead(
                    configured.client, source, owner_kind="inventory"
                ),
                actual,
                source={
                    "label": "Configured platform storage",
                    "endpoint": source["endpoint"],
                },
                handling=handling,
                observations=observations,
                limits=InventoryLimits(
                    **{
                        key: value
                        for key, value in limits.items()
                        if key != "max_seconds"
                    }
                ),
                sink=sink,
                max_output_bytes=REPORT_LIMIT,
                checkpoint=checkpoint,
                strict_closure=True,
            )
    checkpoint()
    return sink.value


async def execute(db, job_id, task_id):
    row = await service.claim(db, job_id, task_id)
    await db.commit()
    if row is None:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return False
    source, limits, handling, revision = (
        dict(row.source),
        dict(row.limits),
        dict(row.handling),
        row.maintenance_revision,
    )
    window_id, window_revision = row.window_id, row.window_revision
    bind, loop, stop = (
        storage_access.engine(db),
        asyncio.get_running_loop(),
        threading.Event(),
    )
    deadline = time.monotonic() + limits["max_seconds"]

    async def fresh():
        async with AsyncSession(
            bind=bind, expire_on_commit=False, autoflush=False
        ) as owned:
            await service.checkpoint(owned, job_id, task_id)

    def checkpoint():
        if stop.is_set():
            raise InventoryStopped("inventory_cancelled")
        if time.monotonic() >= deadline:
            raise InventoryStopped("inventory_time_budget")
        try:
            asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(fresh(), 5), loop
            ).result()
            if time.monotonic() >= deadline:
                raise InventoryStopped("inventory_time_budget")
        except Exception as exc:
            raise InventoryStopped(service.safe_error(exc)) from None

    async def acquired(permit):
        await service.acquired(db, job_id, task_id, permit)

    report, failure, cancelled = None, None, False
    reading = None
    try:
        observations = await collect_observations(
            bind,
            source["purpose_buckets"],
            source["endpoint"],
            str(service.LEGACY_LOCATION_ID),
        )
        if window_id is not None:
            observations = {
                **observations,
                "migration_window": {
                    "window_id": str(window_id),
                    "window_revision": window_revision,
                    "source_quiescence_proven": False,
                },
            }
        # No transaction or Session crosses the native thread. The fresh checks
        # each use their own short, read-only session on this event loop.
        await db.rollback()
        if window_id is not None:
            read = source_read.run(
                db,
                window_id,
                "inventory",
                job_id,
                task_id,
                scan,
                source,
                limits,
                handling,
                observations,
                checkpoint,
                on_acquired=acquired,
            )
        else:
            read = storage_access.run(
                db,
                service.LEGACY_LOCATION_ID,
                service.KIND,
                scan,
                source,
                limits,
                handling,
                observations,
                checkpoint,
                expected_revision=revision,
                on_acquired=acquired,
            )
        reading = asyncio.create_task(read)
        report = await asyncio.shield(reading)
    except asyncio.CancelledError:
        cancelled = True
        stop.set()
        if reading is not None:
            try:
                await storage_access.finish(reading)
            except BaseException:
                pass
    except InventoryStopped as exc:
        failure = service.safe_error(exc)
    except Exception as exc:
        failure = service.safe_error(exc)

    async def finalize():
        await db.rollback()
        changed = await service.publish(
            db, job_id, task_id, report=report, error_code=failure, cancelled=cancelled
        )
        if changed:
            await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return changed

    finalizing = asyncio.create_task(finalize())
    try:
        changed = await asyncio.shield(finalizing)
    except asyncio.CancelledError:
        cancelled = True
        changed = await storage_access.finish(finalizing)
    if cancelled:
        raise asyncio.CancelledError()
    return changed


async def _run(job_id, task_id):
    async with task_session() as db:
        return await execute(db, job_id, task_id)


@celery_app.task(
    bind=True,
    name="app.workers.storage_migration_inventory.inventory_configured_source",
)
def inventory_configured_source(self, job_id):
    return asyncio.run(_run(uuid.UUID(str(job_id)), str(self.request.id or "")))
