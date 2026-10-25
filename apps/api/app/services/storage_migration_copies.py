"""Frozen copy ownership, explicit attempts and independent object receipt commits."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import undefer

from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_migration_copy import (
    BASELINE_LIMIT,
    StorageMigrationCopy as Copy,
    StorageMigrationCopyAttempt as Attempt,
    StorageMigrationCopyDelta as Delta,
)
from app.db.models.storage_migration_inventory import StorageMigrationInventory
from app.db.models.user import User
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyGenerationSummary,
    StorageMigrationCopyItem,
    StorageMigrationCopyHistory,
)
from app.services import (
    async_job as jobs,
    storage_access,
    storage_migration_inventories as inventories,
    storage_migration_copy_target as targets,
)
from app.services.audit import AuditService
from app.services.storage_migration_copy import (
    Checkpoint,
    CopyError,
    digest as row_digest,
    packed,
)

KIND = "storage_migration_copy"
ACTIVE = {"pending", "running"}
STATES = ("verified", "pending", "writing", "uncertain", "failed", "skipped")
authority = inventories.authority
reject = inventories.reject
now = inventories.now


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
    q = select(Copy).where(Copy.id == copy_id).execution_options(populate_existing=True)
    if lock:
        q = q.with_for_update()
    if private:
        q = q.options(
            undefer(Copy.source_report), undefer(Copy.catalog), undefer(Copy.baseline)
        )
    row = await db.scalar(q)
    if row is None:
        raise HTTPException(404, "Copy not found")
    return row


async def last(db, row, *, lock=False):
    q = (
        select(Attempt)
        .where(Attempt.id == row.last_attempt_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        q = q.with_for_update()
    return await db.scalar(q)


async def by_job(db, job_id, *, lock=False):
    q = (
        select(Attempt)
        .where(Attempt.async_job_id == job_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        q = q.with_for_update()
    return await db.scalar(q)


async def successor(db, copy_id):
    return await db.scalar(select(Copy.id).where(Copy.parent_copy_id == copy_id))


async def writable(db, row):
    if await successor(db, row.id) is not None:
        reject("copy_read_only")


async def item(db, row):
    a = await last(db, row)
    name = await db.scalar(
        select(StorageConnection.name)
        .join(StorageLocation, StorageLocation.connection_id == StorageConnection.id)
        .where(StorageLocation.id == row.target_location_id)
    )
    summary, result = row.summary or {}, (a.result or {}) if a else {}
    successor_id = await successor(db, row.id)
    return StorageMigrationCopyItem(
        copy_id=row.id,
        parent_copy_id=row.parent_copy_id,
        generation=row.generation,
        successor_copy_id=successor_id,
        parent_evidence_sha256=row.parent_evidence_sha256,
        read_only=successor_id is not None,
        generation_summary=summary.get("generation_summary"),
        created_at=row.created_at,
        updated_at=row.updated_at,
        source_inventory_job_id=row.source_inventory_job_id,
        source_report_sha256=row.source_report_sha256,
        source_complete=row.source_complete,
        target_location_id=row.target_location_id,
        target_name=name,
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
    total = await db.scalar(select(func.count()).select_from(Copy))
    rows = await db.scalars(
        select(Copy)
        .order_by(Copy.created_at.desc(), Copy.id.desc())
        .limit(limit)
        .offset(offset)
    )
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
    report = await db.get(StorageMigrationInventory, inventory_job_id)
    if report is None or report.report_sha256 is None:
        reject("copy_source_report_unavailable")
    q = (
        select(
            StorageLocation.id.label("location_id"),
            StorageConnection.name,
            StorageLocation.config_revision,
        )
        .join(StorageConnection, StorageConnection.id == StorageLocation.connection_id)
        .where(
            StorageLocation.enabled,
            StorageLocation.kind == "s3",
            StorageConnection.kind == "s3",
            StorageConnection.scope == "global",
            StorageConnection.project_id.is_(None),
            StorageConnection.secret_enc.is_not(None),
        )
    )
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = await db.execute(
        q.order_by(StorageConnection.name, StorageLocation.id)
        .limit(limit)
        .offset(offset)
    )
    return {
        "items": [dict(row._mapping) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def scope(db, row, *, lock=False):
    if lock:
        locations = sorted({inventories.LEGACY_LOCATION_ID, row.target_location_id})
        connection_ids = list(
            await db.scalars(
                select(StorageLocation.connection_id).where(
                    StorageLocation.id.in_(locations),
                    StorageLocation.connection_id.is_not(None),
                )
            )
        )
        if connection_ids:
            await db.execute(
                select(StorageConnection.id)
                .where(StorageConnection.id.in_(connection_ids))
                .order_by(StorageConnection.id)
                .with_for_update(read=True)
            )
        await db.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id.in_(locations))
            .order_by(StorageLocation.id)
            .with_for_update(read=True)
        )
    identity, revisions, access, _ = await targets.resolve(
        db, row.target_location_id, lock=False
    )
    if identity != row.target:
        reject("copy_target_changed")
    source = await inventories.source_scope(db, lock=False)
    if {k: source[k] for k in ("endpoint", "purpose_buckets")} != {
        k: row.source[k] for k in ("endpoint", "purpose_buckets")
    }:
        reject("copy_source_changed")
    maintenance = {}
    for location in sorted({inventories.LEGACY_LOCATION_ID, row.target_location_id}):
        maintenance[str(location)] = await storage_access.check(db, location, lock=lock)
    return {
        "target": revisions,
        "source": source["location_revision"],
        "maintenance": maintenance,
        "allowlist": sorted(access.allowlist),
        "target_location_id": str(row.target_location_id),
    }, access


async def current(db, row, a, *, lock=False):
    await writable(db, row)
    actor = await db.get(User, a.requested_by, populate_existing=True)
    if actor is None:
        reject("copy_authority_changed", 403)
    _, proof = await authority(db, actor, a.authority.get("api_key_id"), lock=lock)
    if proof != a.authority.get("key_proof"):
        reject("copy_authority_changed", 403)
    actual, access = await scope(db, row, lock=lock)
    if actual != a.scope:
        reject("copy_context_changed")
    return access


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


async def new_attempt(db, row, actor, proof, key_id, body, *, request=None):
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
    a = Attempt(
        id=uuid.uuid4(),
        copy_id=row.id,
        async_job_id=job.id,
        task_id=task_id,
        requested_by=actor.id,
        authority={"api_key_id": str(key_id) if key_id else None, "key_proof": proof},
        scope=attempt_scope,
        operation=body.operation,
        retry_failed=body.retry_failed,
        retry_object_ids=sorted(body.retry_object_ids)
        if body.retry_object_ids
        else None,
        status="pending",
    )
    db.add(a)
    row.last_attempt_id, row.updated_at = a.id, now()
    job.result = public_result(row, a, stage="queued")
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
    return a, job


async def start(db, actor, body, *, api_key_id=None, request=None):
    from app.schemas.storage_migration_copy import StorageMigrationCopyAttemptRequest

    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(8592650718001)"))
    raw, report_sha = await inventories.download(
        db, body.inventory_job_id, actor, api_key_id=api_key_id
    )
    inventory = await db.get(StorageMigrationInventory, body.inventory_job_id)
    source = await inventories.source_scope(db, lock=False)
    if {k: inventory.source[k] for k in ("endpoint", "purpose_buckets")} != {
        k: source[k] for k in ("endpoint", "purpose_buckets")
    }:
        reject("copy_source_changed")
    identity, _, _, _ = await targets.resolve(db, body.target_location_id, lock=False)
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
    fingerprint = inventories.digest(
        [report_sha, identity, body.handling, body.limits.model_dump()]
    )
    row = await db.scalar(
        select(Copy).where(Copy.input_fingerprint == fingerprint).with_for_update()
    )
    if row:
        a = await last(db, row, lock=True)
        if a and a.status in ACTIVE and a.requested_by != actor.id:
            reject("copy_active_conflict")
        job = await jobs.lock_job(db, a.async_job_id) if a and a.async_job_id else None
        return row, job, True
    row = Copy(
        id=uuid.uuid4(),
        input_fingerprint=fingerprint,
        source_inventory_job_id=body.inventory_job_id,
        source_report=raw,
        source_report_sha256=report_sha,
        source_complete=inventory.complete,
        source=source,
        target_location_id=body.target_location_id,
        target=identity,
        handling=body.handling,
        limits=body.limits.model_dump(),
        delta_sequence=0,
        delta_bytes=0,
    )
    db.add(row)
    await db.flush()
    a, job = await new_attempt(
        db,
        row,
        actor,
        proof,
        api_key_id,
        StorageMigrationCopyAttemptRequest(operation="prepare"),
        request=request,
    )
    return row, job, False


def evidence(values):
    """Hash validated logical evidence, independently of SQLite page layout."""
    with tempfile.TemporaryDirectory(prefix="aap-copy-parent-") as directory:
        path = materialize(directory, values)
        with Checkpoint(path, max_bytes=BASELINE_LIMIT, strict_closure=True) as state:
            return state.evidence_digest()


async def settled_parent(db, row):
    attempts = list(
        await db.scalars(
            select(Attempt)
            .where(Attempt.copy_id == row.id)
            .order_by(Attempt.id)
            .with_for_update()
        )
    )
    if any(a.status in ACTIVE for a in attempts):
        reject("copy_active_conflict")
    ids = [
        p for a in attempts for p in (a.source_operation_id, a.target_operation_id) if p
    ]
    if ids and await db.scalar(
        select(StorageOperation.id)
        .where(StorageOperation.id.in_(ids))
        .with_for_update()
        .limit(1)
    ):
        reject("copy_execution_unsettled")


async def generation(db, parent_id, actor, body, *, api_key_id=None, request=None):
    from app.schemas.storage_migration_copy import StorageMigrationCopyAttemptRequest

    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(8592650718001)"))
    raw, report_sha = await inventories.download(
        db, body.inventory_job_id, actor, api_key_id=api_key_id
    )
    inventory = await db.get(StorageMigrationInventory, body.inventory_job_id)
    initial = await owner(db, parent_id)
    await scope(db, initial, lock=True)
    parent = await owner(db, parent_id, lock=True, private=True)
    child_id = await successor(db, parent.id)
    if child_id is not None:
        child = await owner(db, child_id, lock=True)
        if child.source_report_sha256 != report_sha:
            reject("copy_successor_exists")
        a = await last(db, child, lock=True)
        if a and a.status in ACTIVE and a.requested_by != actor.id:
            reject("copy_active_conflict")
        job = await jobs.lock_job(db, a.async_job_id) if a and a.async_job_id else None
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
    values = await input_values(db, parent)
    native = asyncio.create_task(asyncio.to_thread(evidence, values))
    try:
        parent_digest = await asyncio.shield(native)
    except asyncio.CancelledError:
        # Keep the row lock until the bounded native checkpoint really closes.
        await storage_access.finish(native)
        raise
    except (CopyError, ValueError) as exc:
        reject(safe_error(exc))
    row = Copy(
        id=uuid.uuid4(),
        parent_copy_id=parent.id,
        generation=parent.generation + 1,
        parent_evidence_sha256=parent_digest,
        input_fingerprint=inventories.digest(
            ["generation", str(parent.id), parent_digest, report_sha]
        ),
        source_inventory_job_id=body.inventory_job_id,
        source_report=raw,
        source_report_sha256=report_sha,
        source_complete=inventory.complete,
        source=json.loads(json.dumps(parent.source)),
        target_location_id=parent.target_location_id,
        target=json.loads(json.dumps(parent.target)),
        handling=json.loads(json.dumps(parent.handling)),
        limits=json.loads(json.dumps(parent.limits)),
        delta_sequence=0,
        delta_bytes=0,
    )
    db.add(row)
    await db.flush()
    parent.updated_at = now()
    _, job = await new_attempt(
        db,
        row,
        actor,
        proof,
        api_key_id,
        StorageMigrationCopyAttemptRequest(operation="prepare"),
        request=request,
    )
    return row, job, False


async def settle_dead(db, a):
    ids = [value for value in (a.source_operation_id, a.target_operation_id) if value]
    existing = (
        list(
            await db.scalars(
                select(StorageOperation)
                .where(StorageOperation.id.in_(ids))
                .with_for_update()
            )
        )
        if ids
        else []
    )
    if (a.status == "running" or existing) and not proven_dead(a.process):
        reject("copy_execution_unsettled")
    for operation in existing:
        expected = (
            str(inventories.LEGACY_LOCATION_ID)
            if operation.id == a.source_operation_id
            else a.scope.get("target_location_id")
        )
        # Target ID is also saved by acquired() inside the scope.
        if (
            str(operation.location_id) != expected
            or operation.purpose != KIND
            or operation.node_id != f"migration:{a.process['pid']}"
        ):
            reject("copy_execution_unsettled")
        await db.delete(operation)
    if a.status in ACTIVE:
        job = await jobs.lock_job(db, a.async_job_id) if a.async_job_id else None
        a.status, a.completed_at, a.error_code = "failed", now(), "copy_process_ended"
        if job:
            job.status, job.completed_at, job.error_message = (
                "failed",
                a.completed_at,
                a.error_code,
            )
    await db.flush()


async def attempt(db, copy_id, actor, body, *, api_key_id=None, request=None):
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    initial = await owner(db, copy_id)
    # All authorization/connection/location locks precede owner/attempt/job locks.
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
                return row, await jobs.lock_job(db, a.async_job_id), True
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
    if body.retry_object_ids and not recovering:
        ids = set(
            await db.scalars(
                select(Delta.object_id).where(
                    Delta.copy_id == row.id,
                    Delta.object_id.in_(body.retry_object_ids),
                    Delta.data["state"].astext == "failed",
                )
            )
        )
        if ids != set(body.retry_object_ids):
            reject("selection_changed")
    a, job = await new_attempt(db, row, actor, proof, api_key_id, body, request=request)
    return row, job, False


async def redispatch(db, copy_id, actor, *, api_key_id=None):
    await authority(db, actor, api_key_id, lock=True)
    row = await owner(db, copy_id, lock=True)
    await writable(db, row)
    a = await last(db, row, lock=True)
    job = await jobs.lock_job(db, a.async_job_id) if a and a.async_job_id else None
    if not matches(row, a, job) or a.status != "pending":
        reject("copy_not_pending")
    return row, job


async def failed_objects(db, copy_id, actor, *, api_key_id=None, limit=20, offset=0):
    await authority(db, actor, api_key_id)
    await owner(db, copy_id)
    q = select(Delta).where(
        Delta.copy_id == copy_id, Delta.data["state"].astext == "failed"
    )
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = await db.scalars(q.order_by(Delta.object_id).limit(limit).offset(offset))
    return {
        "items": [
            {
                "object_id": r.object_id,
                "size_bytes": r.data["size_bytes"],
                "error_code": r.data.get("error")
                if isinstance(r.data.get("error"), str)
                and inventories.SAFE_CODE.fullmatch(r.data["error"])
                else "copy_object_failed",
            }
            for r in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def finish(db, row, a, job, status, error_code=None):
    a.status, a.completed_at, a.error_code = status, now(), error_code
    row.updated_at = a.completed_at
    if status == "cancelled":
        a.cancel_requested_at = a.cancel_requested_at or now()
    if job:
        job.status, job.completed_at, job.error_message = (
            status,
            a.completed_at,
            error_code,
        )
        job.result = public_result(row, a)
        if status == "completed":
            job.progress_pct = 100
    await db.flush()
    return True


async def cancel(db, job_id):
    initial = await by_job(db, job_id)
    if initial is None:
        raise HTTPException(404, "Copy attempt not found")
    row = await owner(db, initial.copy_id, lock=True)
    await writable(db, row)
    a = await by_job(db, job_id, lock=True)
    job = await jobs.lock_job(db, job_id)
    if not matches(row, a, job):
        reject("copy_job_unavailable")
    if a.status not in ACTIVE:
        if a.status == "cancelled":
            return "cancelled"
        reject("copy_terminal")
    a.cancel_requested_at = a.cancel_requested_at or now()
    if a.status == "pending":
        await finish(db, row, a, job, "cancelled")
        return "cancelled"
    await jobs.request_cancel(db, job_id)
    job.result = public_result(row, a)
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
    job = await jobs.lock_job(db, job_id)
    if a.status != "pending" or a.task_id != task_id or not matches(row, a, job):
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
    a.status, a.started_at, a.process = "running", now(), process_identity()
    job.status, job.started_at = "running", a.started_at
    job.result = public_result(
        row,
        a,
        stage="preparing"
        if a.operation == "prepare"
        else "copying"
        if a.operation == "run"
        else "verifying",
    )
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
    row = await owner(db, a.copy_id)
    await current(db, row, a, lock=True)
    row = await owner(db, row.id, lock=True)
    await writable(db, row)
    a = await by_job(db, job_id, lock=True)
    if (
        a.status != "running"
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
    a.source_operation_id = by_location[inventories.LEGACY_LOCATION_ID].id
    a.target_operation_id = by_location[row.target_location_id].id
    a.scope = {**a.scope, "target_location_id": str(row.target_location_id)}
    await db.commit()


async def input_values(db, row):
    deltas = list(
        await db.scalars(
            select(Delta).where(Delta.copy_id == row.id).order_by(Delta.sequence)
        )
    )
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
        )
    }
    values["deltas"] = [(r.object_id, r.data, r.digest) for r in deltas]
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


async def save_delta(db, job_id, task_id, data, checksum):
    """Persist physical facts independently of current permission/cancellation."""
    a = await by_job(db, job_id)
    if a is None:
        reject("copy_context_changed")
    row = await owner(db, a.copy_id, lock=True)
    a = await by_job(db, job_id, lock=True)
    if (
        a.task_id != task_id
        or row.last_attempt_id != a.id
        or row.baseline_sha256 is None
        or a.operation == "prepare"
    ):
        reject("copy_context_changed")
    size = len(packed(data).encode()) + 1
    if row_digest(data) != checksum or size > 262144 or data.get("state") not in STATES:
        reject("copy_checkpoint_integrity")
    old = await db.get(Delta, (row.id, data["id"]), populate_existing=True)
    updated_size = row.delta_bytes + size - (old.size_bytes if old else 0)
    if updated_size > BASELINE_LIMIT:
        reject("copy_delta_budget")
    summary = json.loads(json.dumps(row.summary))
    # Each first delta starts from its immutable plan state.
    previous = old.data["state"] if old else "pending"
    summary["counts"][previous] -= 1
    summary["counts"][data["state"]] += 1
    if any(n < 0 for n in summary["counts"].values()):
        reject("copy_checkpoint_integrity")
    summary["copy_subset_complete"] = not any(
        summary["counts"][k] for k in ("pending", "writing", "uncertain", "failed")
    )
    row.summary, row.delta_sequence, row.delta_bytes, row.updated_at = (
        summary,
        row.delta_sequence + 1,
        updated_size,
        now(),
    )
    if old is None:
        old = Delta(copy_id=row.id, object_id=data["id"])
        db.add(old)
    old.sequence, old.data, old.digest, old.size_bytes = (
        row.delta_sequence,
        data,
        checksum,
        size,
    )
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
    job = await jobs.lock_job(db, job_id)
    if a.status != "running" or a.task_id != task_id or not matches(row, a, job):
        return False
    ids = [p for p in (a.source_operation_id, a.target_operation_id) if p]
    if len(ids) != 2 or await db.scalar(
        select(StorageOperation.id).where(StorageOperation.id.in_(ids)).limit(1)
    ):
        return await finish(db, row, a, job, "failed", "storage_operation_unsettled")
    if result is not None:
        a.result = {
            k: result.get(k)
            for k in (
                "io",
                "attempt_subset_complete",
                "selected_count",
                "selected_verified_count",
            )
        }
    if cancelled or a.cancel_requested_at:
        return await finish(db, row, a, job, "cancelled")
    if failure or auth_failure:
        return await finish(db, row, a, job, "failed", auth_failure or failure)
    if prepared is not None:
        if a.operation != "prepare" or row.baseline_sha256:
            reject("copy_context_changed")
        baseline, catalog, summary = prepared
        row.baseline, row.baseline_sha256 = baseline, sha(baseline)
        row.catalog, row.catalog_sha256 = catalog, sha(catalog)
        row.summary = safe_summary(summary)
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


def materialize(directory, values):
    path = Path(directory) / "checkpoint.sqlite"
    path.write_bytes(values["baseline"])
    path.chmod(0o600)
    with Checkpoint(path, max_bytes=BASELINE_LIMIT, strict_closure=True) as state:
        if (
            state.meta.get("generation", 1) != values["generation"]
            or state.meta.get("parent_evidence_sha256")
            != values["parent_evidence_sha256"]
        ):
            raise CopyError("copy_checkpoint_integrity")
        for object_id, data, checksum in values["deltas"]:
            if object_id != data.get("id") or row_digest(data) != checksum:
                raise CopyError("copy_checkpoint_integrity")
            saved = state.db.execute(
                "SELECT data,digest FROM results WHERE id=?", (object_id,)
            ).fetchone()
            if saved is None:
                raise CopyError("copy_checkpoint_integrity")
            original = state.decode(*saved)
            # Immutable plan identity survives every mutable row delta.
            mutable = {"state", "nonce", "receipt", "error", "content"}
            if {k: v for k, v in data.items() if k not in mutable} != {
                k: v for k, v in original.items() if k not in mutable
            } or (original.get("nonce") and data.get("nonce") != original["nonce"]):
                raise CopyError("copy_checkpoint_integrity")
            state.save(data)
    return path


def snapshot(values):
    with tempfile.TemporaryDirectory(prefix="aap-copy-download-") as directory:
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
    actor = await db.get(User, actor_id, populate_existing=True)
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
    job = await jobs.lock_job(db, job_id)
    if a.status not in ACTIVE or a.task_id != task_id or not matches(row, a, job):
        return False
    return await finish(
        db,
        row,
        a,
        job,
        "cancelled" if cancelled and a.status == "pending" else "failed",
        None if cancelled and a.status == "pending" else "copy_interrupted",
    )
