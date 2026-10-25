"""One native copy owner, sorted durable permits and close-before-publish lifecycle."""

import asyncio
from contextlib import AsyncExitStack, ExitStack
from contextvars import copy_context
from functools import partial
import logging
from pathlib import Path
import tempfile
import threading
import time
import uuid

from botocore.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.storage_migration_copy import (
    BASELINE_LIMIT,
    CATALOG_LIMIT,
    ROW_LIMIT,
)
from app.services import storage_access, storage_migration_copies as service
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import StorageService
from app.services.storage_migration_inventory import InventoryStopped
from app.services.storage_migration_copy import (
    Checkpoint,
    CopyStopped,
    Limits,
    execute as copy_execute,
    plan,
    plan_generation,
)
from app.services.storage_migration_reference_collector import Spool, collect
from app.services.storage_migration_references import (
    Budget,
    Limits as ReferenceLimits,
    write_catalog,
)
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
        copy_storage.apply_async(
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
        log.warning("Storage migration copy dispatch unavailable job=%s", job_id)


class GuardedClient:
    """Check current authority before each SDK call; ACK saves precede next I/O."""

    def __init__(self, client, checkpoint):
        self.client, self.checkpoint = client, checkpoint

    def __getattr__(self, name):
        call = getattr(self.client, name)

        def guarded(*args, **kwargs):
            self.checkpoint()
            return call(*args, **kwargs)

        return guarded


def native(
    operation,
    values,
    access,
    checkpoint,
    save,
    directory,
    metadata=None,
    reference_limits=None,
    retry_failed=False,
    retry_object_ids=None,
):
    """Own all local/SDK lifetimes in the one registered native thread."""
    checkpoint()
    root = Path(directory)
    source_path = root / "source.jsonl"
    with storage_access.closing_registered(source_path.open("wb")) as stream:
        stream.write(values["source_report"])
    source_path.chmod(0o600)
    if operation == "prepare":
        catalog_path, path = root / "catalog.jsonl", root / "checkpoint.sqlite"
        budget = Budget(reference_limits, checkpoint, strict_closure=True)
        write_catalog(
            catalog_path, root / "references.jsonl", metadata, source_path, root, budget
        )
        options = dict(
            handling=values["handling"],
            limits=Limits(**values["limits"]),
            checkpoint_hook=checkpoint,
            max_bytes=BASELINE_LIMIT,
            max_rows=ROW_LIMIT,
            strict_closure=True,
        )
        with ExitStack() as stack:
            if values["parent_copy_id"] is None:
                result = plan(
                    path, source_path, catalog_path, values["target"], **options
                )
            else:
                parent_root = root / "parent"
                parent_root.mkdir(mode=0o700)
                parent_path = service.materialize(parent_root, values["parent"])
                parent = stack.enter_context(
                    storage_access.closing_registered(
                        Checkpoint(
                            parent_path, max_bytes=BASELINE_LIMIT, strict_closure=True
                        )
                    )
                )
                result = plan_generation(
                    path,
                    source_path,
                    catalog_path,
                    values["target"],
                    parent=parent,
                    parent_evidence_sha256=values["parent_evidence_sha256"],
                    **options,
                )
            # Verify the generated checkpoint independently of the returned summary.
            with storage_access.closing_registered(
                Checkpoint(path, max_bytes=BASELINE_LIMIT, strict_closure=True)
            ) as prepared:
                if (
                    prepared.meta.get("generation", 1) != values["generation"]
                    or prepared.meta.get("parent_evidence_sha256")
                    != values["parent_evidence_sha256"]
                ):
                    raise CopyStopped("copy_checkpoint_integrity")
        checkpoint()
        # The core closed both sealed outputs before bytes become publishable.
        with storage_access.closing_registered(path.open("rb")) as stream:
            baseline = stream.read(BASELINE_LIMIT + 1)
        with storage_access.closing_registered(catalog_path.open("rb")) as stream:
            catalog = stream.read(CATALOG_LIMIT + 1)
        if (
            not 0 < len(baseline) <= BASELINE_LIMIT
            or not 0 < len(catalog) <= CATALOG_LIMIT
        ):
            raise CopyStopped("copy_checkpoint_budget")
        return (baseline, catalog, result), None
    path = service.materialize(root, values)
    with ExitStack() as stack:
        state = stack.enter_context(
            storage_access.closing_registered(
                Checkpoint(
                    path, on_save=save, max_bytes=BASELINE_LIMIT, strict_closure=True
                )
            )
        )
        try:
            configured = StorageService(
                client_config=Config(
                    connect_timeout=values["limits"]["connect_timeout_seconds"],
                    read_timeout=values["limits"]["read_timeout_seconds"],
                    retries={"total_max_attempts": 1},
                    s3={"addressing_style": "path", "payload_signing_enabled": False},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                )
            )
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        source = stack.enter_context(
            storage_access.closing_registered(configured.client)
        )
        if source.meta.endpoint_url.rstrip("/") != values["source"]["endpoint"].rstrip(
            "/"
        ):
            raise CopyStopped("copy_source_changed")
        target = stack.enter_context(
            access.client(read_timeout=values["limits"]["read_timeout_seconds"])
        )
        result = copy_execute(
            state,
            GuardedClient(source, checkpoint),
            GuardedClient(target, checkpoint),
            retry_failed=retry_failed,
            retry_object_ids=retry_object_ids,
            verify_only=operation == "verify",
            stopped=lambda: checkpoint() or False,
            anonymous_staging=True,
            strict_closure=True,
        )
    return None, result


async def execute(db, job_id, task_id):
    a = await service.claim(db, job_id, task_id)
    await db.commit()
    if a is None:
        await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return False
    copy_id, operation, retry_failed, selected = (
        a.copy_id,
        a.operation,
        a.retry_failed,
        a.retry_object_ids,
    )
    revisions = dict(a.scope["maintenance"])
    values = await service.inputs(db, copy_id)
    bind, loop, stop = (
        storage_access.engine(db),
        asyncio.get_running_loop(),
        threading.Event(),
    )
    deadline = time.monotonic() + values["limits"]["max_seconds"]
    stop_code = ["copy_cancelled"]

    def local_check():
        if stop.is_set():
            raise CopyStopped(stop_code[0])
        if time.monotonic() >= deadline:
            raise CopyStopped("time_budget")

    async def fresh():
        async with AsyncSession(
            bind=bind, expire_on_commit=False, autoflush=False
        ) as owned:
            return await service.checkpoint(owned, job_id, task_id)

    def checkpoint():
        local_check()
        try:
            asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(fresh(), 5), loop
            ).result()
        except Exception as exc:
            raise CopyStopped(service.safe_error(exc)) from None
        local_check()

    def save(data, checksum):
        # A late cancellation or authority revocation cannot discard an ACK.
        async def persist():
            async with AsyncSession(
                bind=bind, expire_on_commit=False, autoflush=False
            ) as owned:
                await service.save_delta(owned, job_id, task_id, data, checksum)

        try:
            asyncio.run_coroutine_threadsafe(persist(), loop).result()
        except Exception:
            raise CopyStopped("copy_checkpoint_commit_failed") from None

    async def monitor():
        while not stop.is_set():
            try:
                await fresh()
            except Exception as exc:
                stop_code[0] = service.safe_error(exc)
                stop.set()
                return
            await asyncio.sleep(0.2)

    async def perform():
        async with AsyncExitStack() as stack:
            permits = []
            for location in sorted(
                {
                    service.inventories.LEGACY_LOCATION_ID,
                    uuid.UUID(values["target"]["location_id"]),
                }
            ):
                permits.append(
                    await stack.enter_async_context(
                        await storage_access.acquire(
                            db,
                            location,
                            service.KIND,
                            expected_revision=revisions[str(location)],
                            node_id=f"migration:{__import__('os').getpid()}",
                        )
                    )
                )
            await service.acquired(db, job_id, task_id, permits)
            access = await fresh()
            with tempfile.TemporaryDirectory(prefix="aap-copy-attempt-") as directory:
                metadata, reference_limits = (
                    None,
                    ReferenceLimits(
                        max_output_bytes=CATALOG_LIMIT,
                        max_source_bytes=32 * 1024**2,
                        max_source_entries=ROW_LIMIT,
                        max_rows=ROW_LIMIT,
                        max_expanded=ROW_LIMIT,
                        max_seconds=values["limits"]["max_seconds"],
                    ),
                )
                if operation == "prepare":
                    budget = Budget(reference_limits, local_check, strict_closure=True)
                    spool = Spool(Path(directory) / "references.jsonl", budget)
                    try:
                        metadata = await collect(bind, spool, budget)
                    finally:
                        storage_access.close_registered(spool.stream)
                await db.rollback()
                future = loop.run_in_executor(
                    None,
                    copy_context().run,
                    partial(
                        native,
                        operation,
                        values,
                        access,
                        checkpoint,
                        save,
                        directory,
                        metadata,
                        reference_limits,
                        retry_failed,
                        selected,
                    ),
                )
                try:
                    return await asyncio.shield(future)
                except asyncio.CancelledError as exc:
                    stop.set()
                    try:
                        await storage_access.finish(future)
                    except BaseException as late:
                        if getattr(late, "storage_operation_unsettled", False):
                            storage_access.mark_unsettled(exc)
                    raise

    prepared, result, failure, cancelled = None, None, None, False
    monitoring = asyncio.create_task(monitor())
    physical = asyncio.create_task(perform())
    try:
        prepared, result = await asyncio.shield(physical)
    except asyncio.CancelledError:
        cancelled = True
        stop.set()
        try:
            await storage_access.finish(physical)
        except BaseException:
            pass
    except (CopyStopped, InventoryStopped) as exc:
        failure = exc.code
    except Exception as exc:
        failure = service.safe_error(exc)
    finally:
        monitoring.cancel()
        try:
            await monitoring
        except asyncio.CancelledError:
            pass

    async def finalize():
        await db.rollback()
        changed = await service.publish(
            db,
            job_id,
            task_id,
            prepared=prepared,
            result=result,
            failure=failure,
            cancelled=cancelled,
        )
        if changed:
            await notify_job_terminal(db, job_id=job_id)
        await db.commit()
        return changed

    publishing = asyncio.create_task(finalize())
    try:
        changed = await asyncio.shield(publishing)
    except asyncio.CancelledError:
        cancelled = True
        changed = await storage_access.finish(publishing)
    if cancelled:
        raise asyncio.CancelledError()
    return changed


async def _run(job_id, task_id):
    async with task_session() as db:
        return await execute(db, job_id, task_id)


@celery_app.task(bind=True, name="app.workers.storage_migration_copy.copy_storage")
def copy_storage(self, job_id):
    return asyncio.run(_run(uuid.UUID(str(job_id)), str(self.request.id or "")))
