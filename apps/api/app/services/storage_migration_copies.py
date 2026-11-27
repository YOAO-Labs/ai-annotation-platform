"""Frozen copy ownership, explicit attempts and independent object receipt commits."""

import asyncio
from dataclasses import replace
import hashlib
import logging
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from yoao_jobs.lifecycle import JobsLifecycleManager

from app.db.models.storage_migration_copy import BASELINE_LIMIT
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyGenerationSummary,
    StorageMigrationCopyItem,
    StorageMigrationCopyHistory,
)
from app.services import (
    jobs_lifecycle_data as jobs,
    connector_guard,
    storage_access,
    storage_job_progress as progress,
    storage_migration_copy_data as data,
    storage_migration_copy_target as targets,
    storage_migration_inventories as inventories,
    storage_registry_data as registry,
)
from app.services.audit import AuditService
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.jobs_lifecycle_data import (
    JobsLifecyclePostgresData,
    job_lock_statement,
)
from app.services.storage_migration_checkpoint import materialize
from app.services.storage_migration_guards import frozen_copy
from app.services.storage_migration_window_access import copy_scope
from app.services.storage_migration_copy import (
    Checkpoint,
    CopyError,
    digest,
    packed,
)

KIND = "storage_migration_copy"
ACTIVE = {"pending", "running"}
STATES = ("verified", "pending", "writing", "uncertain", "failed", "skipped")
row_digest = digest
authority = inventories.authority
reject = inventories.reject
now = inventories.now
log = logging.getLogger(__name__)


def safe_error(exc):
    code = inventories.safe_error(exc)
    return "copy_failed" if code == "inventory_read_failed" else code


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def process_identity():
    """Linux process identity is positive evidence; unavailable proof stays unknown."""
    try:
        return {
            "host": socket.gethostname(),
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid_namespace": os.stat("/proc/self/ns/pid").st_ino,
            "pid": os.getpid(),
            "start": Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        }
    except (OSError, ValueError, IndexError):
        return None


def proven_dead(proof):
    current = process_identity()
    if (
        not proof
        or not current
        or any(proof.get(k) != current[k] for k in ("host", "boot", "pid_namespace"))
    ):
        return False
    pid = proof.get("pid")
    if type(pid) is not int or pid < 1 or not isinstance(proof.get("start"), str):
        return False
    try:
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return start != proof["start"]
    except FileNotFoundError:
        return True
    except (OSError, ValueError, IndexError):
        return False


async def owner(db, copy_id, *, lock=False, private=False):
    row = await data.plan(db, copy_id, lock=_write_lock(lock), private=private)
    if row is None:
        raise HTTPException(404, "Copy not found")
    return row


def _write_lock(lock):
    return "update" if lock is True else lock or "read"


async def last(db, row, *, lock=False):
    return await data.attempt(
        db, attempt_id=row.last_attempt_id, lock=_write_lock(lock)
    )


async def by_job(db, job_id, *, lock=False):
    return await data.attempt(db, job_id=job_id, lock=_write_lock(lock))


async def by_attempt(db, attempt_id, *, lock=False):
    return await data.attempt(
        db, attempt_id=attempt_id, lock="update_nowait" if lock else "read"
    )


async def window_copy(db, copy_id, *, lock=False):
    return await data.plan(db, copy_id, lock="share_nowait" if lock else "read")


async def successor(db, copy_id):
    return await data.successor(db, copy_id)


async def writable(db, row):
    if await successor(db, row.id) is not None or await frozen_copy(db, row.id):
        reject("copy_read_only")


async def item(db, row):
    try:
        state = sa_inspect(row)
    except Exception:
        state = None
    if state is not None and state.persistent:
        unloaded = state.unloaded - {"source_report", "catalog", "baseline"}
        if unloaded:
            await db.refresh(row, attribute_names=sorted(unloaded))
    a = await last(db, row)
    location = await registry.get_location(db, row.target_location_id)
    connection = (
        await registry.get_connection(db, location.connection_id)
        if location is not None and location.connection_id is not None
        else None
    )
    summary, result = row.summary or {}, (a.result or {}) if a else {}
    successor_id = await successor(db, row.id)
    return StorageMigrationCopyItem(
        copy_id=row.id,
        window_id=row.window_id,
        window_revision=row.window_revision,
        parent_copy_id=row.parent_copy_id,
        generation=row.generation,
        successor_copy_id=successor_id,
        parent_evidence_sha256=row.parent_evidence_sha256,
        read_only=successor_id is not None or await frozen_copy(db, row.id),
        generation_summary=summary.get("generation_summary"),
        created_at=row.created_at,
        updated_at=row.updated_at,
        source_inventory_job_id=row.source_inventory_job_id,
        source_report_sha256=row.source_report_sha256,
        source_complete=row.source_complete,
        target_location_id=row.target_location_id,
        target_name=connection.name if connection is not None else None,
        handling=row.handling,
        limits=row.limits,
        prepared=row.baseline_sha256 is not None,
        job_id=a.async_job_id if a else None,
        operation=a.operation if a else None,
        status=a.status if a else None,
        cancel_requested=a.cancel_requested_at is not None if a else False,
        counts=summary.get("counts"),
        decisions=summary.get("decisions"),
        source_summary=summary.get("source_summary"),
        catalog_summary=summary.get("catalog_summary"),
        copy_subset_complete=summary.get("copy_subset_complete"),
        error_code=a.error_code if a else None,
        io=result.get("io"),
        checkpoint_available=row.baseline_sha256 is not None,
        attempt_subset_complete=result.get("attempt_subset_complete"),
        selected_count=result.get("selected_count"),
        selected_verified_count=result.get("selected_verified_count"),
    )


async def view(db, copy_id, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id)
    value = await item(db, await owner(db, copy_id))
    if (await authority(db, actor, api_key_id))[1] != proof:
        reject("copy_authority_changed", 403)
    return value


async def history(db, actor, *, api_key_id=None, limit=20, offset=0):
    actor, proof = await authority(db, actor, api_key_id)
    total, rows = await data.history(db, limit, offset)
    values = [await item(db, row) for row in rows]
    if (await authority(db, actor, api_key_id))[1] != proof:
        reject("copy_authority_changed", 403)
    return StorageMigrationCopyHistory(
        items=values, total=total, limit=limit, offset=offset
    )


async def candidates(
    db, inventory_job_id, actor, *, api_key_id=None, limit=20, offset=0
):
    await authority(db, actor, api_key_id)
    report = await inventories.by_job(db, inventory_job_id)
    if report is None or report.report_sha256 is None:
        reject("copy_source_report_unavailable")
    total, rows = await data.candidates(db, limit, offset)
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def scope(db, row, *, lock=False):
    if lock:
        await targets.lock_scope(db, row.target_location_id)
    identity, revisions, _ = await targets.facts(db, row.target_location_id)
    if identity != row.target:
        reject("copy_target_changed")
    source = await inventories.source_scope(db, lock=False)
    if {k: source[k] for k in ("endpoint", "purpose_buckets")} != {
        k: row.source[k] for k in ("endpoint", "purpose_buckets")
    }:
        reject("copy_source_changed")
    window_id, window_revision = (
        getattr(row, "window_id", None),
        getattr(row, "window_revision", None),
    )
    if window_id is not None:
        window = await copy_scope(db, window_id, lock=lock)
        if (
            window is None
            or window.parent_copy_id != row.parent_copy_id
            or window.target_location_id != row.target_location_id
            or window.target != row.target
        ):
            reject("copy_window_changed")
    maintenance = {}
    for location in sorted({inventories.LEGACY_LOCATION_ID, row.target_location_id}):
        if location == inventories.LEGACY_LOCATION_ID and window_id is not None:
            maintenance[str(location)] = await inventories.source_revision(
                db, row.source, window_id, window_revision, lock=lock
            )
        else:
            maintenance[str(location)] = await inventories.gate_revision(
                db, location, lock=lock
            )
    allowlist = await connector_guard.get_allowlist(db)
    result = {
        "target": revisions,
        "source": source["location_revision"],
        "maintenance": maintenance,
        "allowlist": sorted(allowlist),
        "target_location_id": str(row.target_location_id),
    }
    if window_id is not None:
        result.update(window_id=str(window_id), window_revision=window_revision)
    return result, identity


async def current(db, row, a, *, lock=False):
    await writable(db, row)
    actor = await IdentityAccountLockData(db).read_actor_status(a.requested_by)
    if actor is None:
        reject("copy_authority_changed", 403)
    _, proof = await authority(db, actor, a.authority.get("api_key_id"), lock=lock)
    if proof != a.authority.get("key_proof"):
        reject("copy_authority_changed", 403)
    actual, identity = await scope(db, row, lock=lock)
    if actual != a.scope:
        reject("copy_context_changed")
    return identity


def matches(row, a, job):
    return bool(
        a
        and job
        and row.last_attempt_id == a.id
        and a.copy_id == row.id
        and job.id == a.async_job_id
        and job.kind == KIND
        and job.celery_task_id == a.task_id
        and job.status == a.status
        and job.user_id == a.requested_by
        and job.payload.get("copy_id") == str(row.id)
        and job.payload.get("operation") == a.operation
    )


def public_result(row, a, *, stage=None):
    summary = row.summary or {}
    return {
        "copy_id": str(row.id),
        **(
            {"window_id": str(row.window_id), "window_revision": row.window_revision}
            if row.window_id is not None
            else {}
        ),
        "parent_copy_id": str(row.parent_copy_id) if row.parent_copy_id else None,
        "generation": row.generation,
        "operation": a.operation,
        "stage": stage or a.status,
        "prepared": row.baseline_sha256 is not None,
        "migration_ready": False,
        **summary,
        "error_code": a.error_code,
        "cancel_requested": a.cancel_requested_at is not None,
        **{
            key: (a.result or {}).get(key)
            for key in (
                "io",
                "attempt_subset_complete",
                "selected_count",
                "selected_verified_count",
            )
        },
    }


async def new_attempt(
    db, row, actor, proof, key_id, body, *, request=None, recovering=False
):
    await writable(db, row)
    attempt_scope, _ = await scope(db, row, lock=True)
    task_id = str(uuid.uuid4())
    job = await jobs.create_job(
        db,
        kind=KIND,
        user_id=actor.id,
        celery_task_id=task_id,
        payload={
            "copy_id": str(row.id),
            "operation": body.operation,
            "migration_ready": False,
        },
    )
    attempt_id, at = uuid.uuid4(), now()
    if not await data.queue(
        db,
        row,
        attempt_id,
        job.id,
        task_id,
        actor.id,
        uuid.UUID(str(key_id)) if key_id else None,
        proof,
        attempt_scope,
        body,
        recovering,
        at,
    ):
        reject("copy_context_changed")
    row = replace(row, last_attempt_id=attempt_id, updated_at=at)
    a = await last(db, row)
    await jobs.replace_result(db, job.id, result=public_result(row, a, stage="queued"))
    await db.flush()
    await AuditService.log(
        db,
        actor=actor,
        action="storage.migration_copy_start",
        target_type="async_job",
        target_id=str(job.id),
        request=request,
        status_code=202,
        detail={
            "copy_id": str(row.id),
            "operation": a.operation,
            "migration_ready": False,
        },
    )
    return row, job


async def start(db, actor, body, *, api_key_id=None, request=None):
    from app.schemas.storage_migration_copy import StorageMigrationCopyAttemptRequest

    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await data.serialize_planning(db)
    report_sha = (
        await inventories.download(
            db, body.inventory_job_id, actor, api_key_id=api_key_id
        )
    )[1]
    inventory = await inventories.by_job(db, body.inventory_job_id)
    if inventory is None:
        raise HTTPException(404, "Inventory not found")
    if inventory.window_id is not None:
        reject("copy_window_requires_generation")
    source = await inventories.source_scope(db, lock=False)
    if {k: inventory.source[k] for k in ("endpoint", "purpose_buckets")} != {
        k: source[k] for k in ("endpoint", "purpose_buckets")
    }:
        reject("copy_source_changed")
    identity, _, _ = await targets.facts(db, body.target_location_id)
    await scope(
        db,
        SimpleNamespace(
            target_location_id=body.target_location_id, target=identity, source=source
        ),
        lock=True,
    )
    if (
        identity["endpoint_url"].rstrip("/") == source["endpoint"].rstrip("/")
        and identity["bucket"] in source["purpose_buckets"].values()
    ):
        reject("target_is_source")
    limits = body.limits.model_dump()
    fingerprint = inventories.digest([report_sha, identity, body.handling, limits])
    row = await data.plan_by_input(db, fingerprint)
    if row:
        a = await last(db, row, lock=True)
        if a and a.status in ACTIVE and a.requested_by != actor.id:
            reject("copy_active_conflict")
        job = (
            await db.scalar(
                job_lock_statement(
                    a.async_job_id, mode="update", populate_existing=True
                )
            )
            if a and a.async_job_id
            else None
        )
        return row, job, True
    copy_id = uuid.uuid4()
    if not await data.freeze_plan(
        db,
        copy_id,
        body.inventory_job_id,
        report_sha,
        fingerprint,
        source,
        body.target_location_id,
        identity,
        body.handling,
        limits,
    ):
        reject("copy_context_changed")
    row, job = await new_attempt(
        db,
        await owner(db, copy_id),
        actor,
        proof,
        api_key_id,
        StorageMigrationCopyAttemptRequest(operation="prepare"),
        request=request,
    )
    return row, job, False


def evidence(values):
    """Hash validated logical evidence, independently of SQLite page layout."""
    with tempfile.TemporaryDirectory(prefix="copy-parent-") as directory:
        path = materialize(directory, values)
        with Checkpoint(path, max_bytes=BASELINE_LIMIT, strict_closure=True) as state:
            return state.evidence_digest()


async def settled_parent(db, row):
    state = await data.parent_settlement(db, row.id)
    if state == "active":
        reject("copy_active_conflict")
    if state != "settled":
        reject("copy_execution_unsettled")


async def generation(db, parent_id, actor, body, *, api_key_id=None, request=None):
    from app.schemas.storage_migration_copy import StorageMigrationCopyAttemptRequest

    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await data.serialize_planning(db)
    report_sha = (
        await inventories.download(
            db, body.inventory_job_id, actor, api_key_id=api_key_id
        )
    )[1]
    inventory = await inventories.by_job(db, body.inventory_job_id)
    if inventory is None:
        raise HTTPException(404, "Inventory not found")
    initial = await owner(db, parent_id)
    if await frozen_copy(db, initial.id):
        reject("copy_read_only")
    bound = (
        SimpleNamespace(
            parent_copy_id=initial.id,
            source=inventory.source,
            target_location_id=initial.target_location_id,
            target=initial.target,
            window_id=inventory.window_id,
            window_revision=inventory.window_revision,
        )
        if inventory.window_id is not None
        else initial
    )
    await scope(db, bound, lock=True)
    parent = await owner(db, parent_id, lock=True)
    if await frozen_copy(db, parent.id):
        reject("copy_read_only")
    child_id = await successor(db, parent.id)
    if child_id is not None:
        child = await owner(db, child_id, lock=True)
        if (
            child.source_report_sha256 != report_sha
            or child.window_id != inventory.window_id
            or child.window_revision != inventory.window_revision
        ):
            reject("copy_successor_exists")
        a = await last(db, child, lock=True)
        if a and a.status in ACTIVE and a.requested_by != actor.id:
            reject("copy_active_conflict")
        job = (
            await db.scalar(
                job_lock_statement(
                    a.async_job_id, mode="update", populate_existing=True
                )
            )
            if a and a.async_job_id
            else None
        )
        return child, job, True
    if parent.baseline_sha256 is None:
        reject("copy_not_prepared")
    if parent.source_report_sha256 == report_sha:
        reject("copy_generation_unchanged")
    if {k: inventory.source[k] for k in ("endpoint", "purpose_buckets")} != {
        k: parent.source[k] for k in ("endpoint", "purpose_buckets")
    }:
        reject("copy_source_changed")
    await settled_parent(db, parent)
    values = await input_values(db, await owner(db, parent_id, private=True))
    native = asyncio.create_task(asyncio.to_thread(evidence, values))
    try:
        parent_digest = await asyncio.shield(native)
    except asyncio.CancelledError:
        await storage_access.finish(native)
        raise
    except (CopyError, ValueError) as exc:
        reject(safe_error(exc))
    copy_id = uuid.uuid4()
    fingerprint = inventories.digest(
        ["generation", str(parent.id), parent_digest, report_sha]
        + (
            [str(inventory.window_id), inventory.window_revision]
            if inventory.window_id
            else []
        )
    )
    if not await data.reserve_successor(
        db,
        copy_id,
        parent.id,
        body.inventory_job_id,
        report_sha,
        parent_digest,
        fingerprint,
        now(),
    ):
        reject("copy_context_changed")
    row, job = await new_attempt(
        db,
        await owner(db, copy_id),
        actor,
        proof,
        api_key_id,
        StorageMigrationCopyAttemptRequest(operation="prepare"),
        request=request,
    )
    return row, job, False


async def settle_dead(db, a):
    existing = await data.permits_open(db, a, lock=True)
    if existing is None:
        reject("copy_execution_unsettled")
    if a.status != "running" and not existing:
        return
    if not proven_dead(a.process):
        reject("copy_execution_unsettled")
    job = (
        await db.scalar(
            job_lock_statement(a.async_job_id, mode="update", populate_existing=True)
        )
        if a.status == "running" and a.async_job_id
        else None
    )
    at = now()
    try:
        async with db.begin_nested():
            settled = await data.settle_process(db, a, at)
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != "23503":
            raise
        reject("copy_execution_unsettled")
    if not settled:
        reject("copy_execution_unsettled")
    if job:
        JobsLifecycleManager(
            JobsLifecyclePostgresData(db), clock=lambda: at
        ).mark_failed(job, error="copy_process_ended")
    await db.flush()


async def attempt(db, copy_id, actor, body, *, api_key_id=None, request=None):
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    initial = await owner(db, copy_id)
    await scope(db, initial, lock=True)
    row = await owner(db, copy_id, lock=True)
    await writable(db, row)
    a = await last(db, row, lock=True)
    recovering = body.operation == "recover"
    if recovering:
        from app.schemas.storage_migration_copy import (
            StorageMigrationCopyAttemptRequest,
        )

        if a is None or a.status == "pending" or not proven_dead(a.process):
            reject("copy_execution_unsettled")
        body = StorageMigrationCopyAttemptRequest(
            operation=a.operation,
            retry_failed=a.retry_failed,
            retry_object_ids=a.retry_object_ids,
        )
    if a and a.status in ACTIVE:
        if a.status == "pending" or not proven_dead(a.process):
            if (
                a.requested_by == actor.id
                and a.operation == body.operation
                and a.retry_failed == body.retry_failed
                and (a.retry_object_ids or None)
                == (sorted(body.retry_object_ids) if body.retry_object_ids else None)
            ):
                return (
                    row,
                    await db.scalar(
                        job_lock_statement(
                            a.async_job_id, mode="update", populate_existing=True
                        )
                    ),
                    True,
                )
            reject("copy_active_conflict")
        await settle_dead(db, a)
    elif a:
        await settle_dead(db, a)
    if (body.operation == "prepare") == (row.baseline_sha256 is not None):
        reject(
            "copy_already_prepared"
            if body.operation == "prepare"
            else "copy_not_prepared"
        )
    if (
        body.retry_object_ids
        and not recovering
        and not await data.failed_selection(db, row.id, body.retry_object_ids)
    ):
        reject("selection_changed")
    row, job = await new_attempt(
        db, row, actor, proof, api_key_id, body, request=request, recovering=recovering
    )
    return row, job, False


async def redispatch(db, copy_id, actor, *, api_key_id=None):
    await authority(db, actor, api_key_id, lock=True)
    row = await owner(db, copy_id, lock=True)
    await writable(db, row)
    a = await last(db, row, lock=True)
    job = (
        await db.scalar(
            job_lock_statement(a.async_job_id, mode="update", populate_existing=True)
        )
        if a and a.async_job_id
        else None
    )
    if not matches(row, a, job) or a.status != "pending":
        reject("copy_not_pending")
    if row.window_id is not None:
        await current(db, row, a, lock=True)
    return row, job


async def failed_objects(db, copy_id, actor, *, api_key_id=None, limit=20, offset=0):
    await authority(db, actor, api_key_id)
    await owner(db, copy_id)
    total, rows = await data.failed_objects(db, copy_id, limit, offset)
    return {
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def finish(db, row, a, job, status, error_code=None):
    at = now()
    if not await data.finish(db, a, status, error_code, a.result, at):
        return False
    a = replace(
        a,
        status=status,
        completed_at=at,
        error_code=error_code,
        cancel_requested_at=(a.cancel_requested_at or at)
        if status == "cancelled"
        else a.cancel_requested_at,
    )
    if job:
        lifecycle = JobsLifecycleManager(
            JobsLifecyclePostgresData(db), clock=lambda: at
        )
        result = public_result(row, a)
        if status == "completed":
            lifecycle.mark_complete(job, result=result)
        elif status == "cancelled":
            lifecycle.mark_cancelled(job, result=result)
        else:
            lifecycle.mark_failed(job, error=error_code, result=result)
    await db.flush()
    return True


async def cancel(db, job_id):
    initial = await by_job(db, job_id)
    if initial is None:
        raise HTTPException(404, "Copy attempt not found")
    row = await owner(db, initial.copy_id, lock=True)
    await writable(db, row)
    a = await by_job(db, job_id, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, a, job):
        reject("copy_job_unavailable")
    if a.status not in ACTIVE:
        if a.status == "cancelled":
            return "cancelled"
        reject("copy_terminal")
    if a.status == "pending":
        if not await finish(db, row, a, job, "cancelled"):
            reject("copy_context_changed")
        return "cancelled"
    at = await data.request_cancel(db, a, now())
    if at is None:
        reject("copy_context_changed")
    a = replace(a, cancel_requested_at=at)
    await jobs.request_cancel(db, job_id)
    await jobs.replace_result(db, job.id, result=public_result(row, a))
    await db.flush()
    return "cancel_requested"


async def claim(db, job_id, task_id):
    a = await by_job(db, job_id)
    if a is None:
        return None
    row = await owner(db, a.copy_id)
    failure = None
    try:
        await current(db, row, a, lock=True)
    except (HTTPException, ValueError) as exc:
        failure = safe_error(exc)
    row = await owner(db, row.id, lock=True)
    if await successor(db, row.id) is not None:
        failure = "copy_read_only"
    a = await by_job(db, job_id, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, a, job) or a.status != "pending" or a.task_id != task_id:
        return None
    if a.cancel_requested_at or failure:
        await finish(
            db,
            row,
            a,
            job,
            "cancelled" if a.cancel_requested_at else "failed",
            None if a.cancel_requested_at else failure,
        )
        return None
    at, process = now(), process_identity()
    if not await data.claim(db, a, process, at):
        return None
    a = replace(a, status="running", started_at=at, process=process)
    result = public_result(
        row,
        a,
        stage={"prepare": "preparing", "run": "copying", "verify": "verifying"}[
            a.operation
        ],
    )
    lifecycle = JobsLifecycleManager(JobsLifecyclePostgresData(db), clock=lambda: at)
    lifecycle.mark_running(job)
    lifecycle.replace_result(job, result=result)
    await db.flush()
    return a


async def checkpoint(db, job_id, task_id):
    a = await by_job(db, job_id)
    if (
        a is None
        or a.status != "running"
        or a.task_id != task_id
        or a.cancel_requested_at
    ):
        reject(
            "copy_cancelled" if a and a.cancel_requested_at else "copy_context_changed"
        )
    row = await owner(db, a.copy_id)
    if row.last_attempt_id != a.id:
        reject("copy_context_changed")
    return await current(db, row, a)


async def acquired(db, job_id, task_id, permits):
    a = await by_job(db, job_id)
    if a is None:
        reject("copy_context_changed")
    row = await owner(db, a.copy_id)
    await current(db, row, a, lock=True)
    row = await owner(db, row.id, lock=True)
    await writable(db, row)
    a = await by_job(db, job_id, lock=True)
    if (
        a is None
        or a.status != "running"
        or a.task_id != task_id
        or row.last_attempt_id != a.id
        or a.cancel_requested_at
        or a.source_operation_id
        or a.target_operation_id
    ):
        reject("copy_context_changed")
    by_location = {p.location_id: p for p in permits}
    if set(by_location) != {inventories.LEGACY_LOCATION_ID, row.target_location_id}:
        reject("copy_context_changed")
    if not await data.attach_permits(
        db,
        a,
        by_location[inventories.LEGACY_LOCATION_ID].id,
        by_location[row.target_location_id].id,
    ):
        reject("copy_context_changed")
    await db.commit()


async def input_values(db, row):
    values = {
        name: getattr(row, name)
        for name in (
            "source_report",
            "source_report_sha256",
            "catalog",
            "catalog_sha256",
            "baseline",
            "baseline_sha256",
            "target",
            "source",
            "limits",
            "handling",
            "generation",
            "parent_copy_id",
            "parent_evidence_sha256",
            "window_id",
            "window_revision",
        )
    }
    values["deltas"] = await data.deltas(db, row.id)
    for field in ("source_report", "catalog", "baseline"):
        if (
            values[field] is not None
            and sha(values[field]) != values[field + "_sha256"]
        ):
            reject("copy_checkpoint_integrity")
    return values


async def inputs(db, copy_id):
    row = await owner(db, copy_id, lock=True, private=True)
    values = await input_values(db, row)
    if row.parent_copy_id is not None and row.baseline_sha256 is None:
        parent = await owner(db, row.parent_copy_id, private=True)
        values["parent"] = await input_values(db, parent)
    await db.rollback()
    return values


def safe_summary(result):
    summary = {
        "counts": {state: result["counts"].get(state, 0) for state in STATES},
        "decisions": result["decisions"],
        "source_summary": {
            k: v for k, v in result["source"].items() if type(v) in (int, bool)
        },
        "catalog_summary": {
            k: result["catalog"][k]
            for k in (
                "complete",
                "enumeration_complete",
                "resolution_complete",
                "counts",
            )
        },
        "copy_subset_complete": result["copy_subset_complete"],
    }
    if result.get("generation_summary") is not None:
        summary["generation_summary"] = (
            StorageMigrationCopyGenerationSummary.model_validate(
                result["generation_summary"]
            ).model_dump()
        )
    return summary


async def save_delta(db, job_id, task_id, delta, checksum):
    """Persist physical facts independently of current permission/cancellation."""
    a = await by_job(db, job_id)
    if a is None:
        reject("copy_context_changed")
    row = await owner(db, a.copy_id, lock=True)
    a = await by_job(db, job_id, lock=True)
    if (
        a is None
        or a.task_id != task_id
        or row.last_attempt_id != a.id
        or row.baseline_sha256 is None
        or a.operation == "prepare"
    ):
        reject("copy_context_changed")
    receipt = await data.record_delta(db, a, packed(delta), checksum, now())
    if receipt["error_code"]:
        reject(receipt["error_code"])
    if receipt["changed"]:
        try:
            async with db.begin_nested():
                job = await db.scalar(
                    job_lock_statement(job_id, mode="update", populate_existing=True)
                )
                if a.status == "running" and matches(row, a, job):
                    await current(db, row, a)
                    await progress.record(
                        db,
                        job,
                        task_id=task_id,
                        stage="verifying" if a.operation == "verify" else "copying",
                        unit="objects",
                        completed=receipt["completed"],
                        marker=f"{a.id}:{delta['id']}:{checksum}",
                        sequence=receipt["sequence"],
                    )
        except Exception:
            log.warning("Copy progress observation failed: job=%s", job_id)
    await db.commit()


async def publish(
    db, job_id, task_id, *, prepared=None, result=None, failure=None, cancelled=False
):
    a = await by_job(db, job_id)
    if a is None:
        return False
    row = await owner(db, a.copy_id)
    auth_failure = None
    try:
        await current(db, row, a, lock=True)
    except (HTTPException, ValueError) as exc:
        auth_failure = safe_error(exc)
    row = await owner(db, row.id, lock=True)
    a = await by_job(db, job_id, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, a, job) or a.status != "running" or a.task_id != task_id:
        return False
    if (
        not a.source_operation_id
        or not a.target_operation_id
        or await data.permits_open(db, a) is not False
    ):
        return await finish(db, row, a, job, "failed", "storage_operation_unsettled")
    if result is not None:
        a = replace(
            a,
            result={
                k: result.get(k)
                for k in (
                    "io",
                    "attempt_subset_complete",
                    "selected_count",
                    "selected_verified_count",
                )
            },
        )
    if cancelled or a.cancel_requested_at:
        return await finish(db, row, a, job, "cancelled")
    if failure or auth_failure:
        return await finish(db, row, a, job, "failed", auth_failure or failure)
    if prepared is not None:
        if a.operation != "prepare" or row.baseline_sha256:
            reject("copy_context_changed")
        baseline, catalog, summary = prepared
        summary = safe_summary(summary)
        baseline_sha = await data.publish_plan(db, a, baseline, catalog, summary)
        if baseline_sha is None:
            reject("copy_context_changed")
        row = replace(row, baseline_sha256=baseline_sha, summary=summary)
        await progress.record(
            db,
            job,
            task_id=task_id,
            stage="preparing",
            unit="objects",
            completed=sum(row.summary["counts"].values()),
            marker=f"{a.id}:{row.baseline_sha256}",
            sequence=1,
        )
        return await finish(db, row, a, job, "completed")
    if a.operation == "prepare" or result is None:
        return await finish(db, row, a, job, "failed", "copy_invalid_result")
    selected_complete = result.get("attempt_subset_complete")
    complete = (
        selected_complete
        if a.retry_object_ids is not None
        else (row.summary or {}).get("copy_subset_complete")
    )
    return await finish(
        db,
        row,
        a,
        job,
        "completed" if complete and not result.get("paused") else "failed",
        None
        if complete and not result.get("paused")
        else result.get("paused") or "copy_subset_incomplete",
    )


def snapshot(values):
    with tempfile.TemporaryDirectory(prefix="copy-download-") as directory:
        path = materialize(directory, values)
        with path.open("rb") as stream:
            raw = stream.read(BASELINE_LIMIT + 1)
        if len(raw) > BASELINE_LIMIT:
            raise CopyError("copy_checkpoint_budget")
    return raw


async def download(db, copy_id, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id)
    actor_id = actor.id
    values = await inputs(db, copy_id)
    if values["baseline"] is None:
        reject("copy_not_prepared")
    task = asyncio.create_task(asyncio.to_thread(snapshot, values))
    try:
        raw = await asyncio.shield(task)
    except asyncio.CancelledError:
        await storage_access.finish(task)
        raise
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or (await authority(db, actor, api_key_id))[1] != proof:
        reject("copy_authority_changed", 403)
    await db.rollback()
    return raw, sha(raw)


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    a = await by_job(db, job_id)
    if a is None:
        return False
    row = await owner(db, a.copy_id, lock=True)
    a = await by_job(db, job_id, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, a, job) or a.status not in ACTIVE or a.task_id != task_id:
        return False
    return await finish(
        db,
        row,
        a,
        job,
        "cancelled" if cancelled and a.status == "pending" else "failed",
        None if cancelled and a.status == "pending" else "copy_interrupted",
    )
