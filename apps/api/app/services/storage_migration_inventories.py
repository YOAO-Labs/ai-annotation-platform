"""Claim-once fixed-source inventory lifecycle and private report publication."""

from datetime import datetime, timezone
import hashlib
import io
import json
import re
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import undefer

from app.config import settings
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_migration_inventory import (
    REPORT_LIMIT,
    StorageMigrationInventory as Inventory,
)
from app.db.models.user import User
from app.schemas.storage_migration_inventory import (
    StorageMigrationInventoryHistory,
    StorageMigrationInventoryItem,
    StorageMigrationInventoryRequest,
)
from app.services import async_job as jobs, storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import LEGACY_LOCATION_ID, PURPOSE_BUCKET_ATTRIBUTES
from app.services.storage import storage_service
from app.services.storage_migration_inventory import PURPOSES, verify_inventory
from app.services.storage_migration_observations import endpoint_identity

KIND = "storage_migration_inventory"
ACTIVE = {"pending", "running"}
SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def now():
    return datetime.now(timezone.utc)


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def authority(db, actor, api_key_id=None, *, lock=False):
    return await storage_maintenance.authority(db, actor, api_key_id, "*", lock=lock)


async def attempt_authority(db, row, *, lock=False):
    actor = await db.get(User, row.requested_by, populate_existing=True)
    if actor is None:
        reject("inventory_authority_changed", 403)
    actor, proof = await authority(
        db, actor, row.authority.get("api_key_id"), lock=lock
    )
    if proof != row.authority.get("key_proof"):
        reject("inventory_authority_changed", 403)
    return actor


async def source_scope(db, *, lock=False):
    statement = (
        select(StorageLocation)
        .where(StorageLocation.id == LEGACY_LOCATION_ID)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(read=True)
    location = await db.scalar(statement)
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    if (
        not location
        or not location.enabled
        or location.kind != "s3"
        or location.connection_id is not None
    ):
        reject("inventory_source_unavailable")
    configured = location.config.get("endpoint")
    if isinstance(configured, str) and "://" not in configured:
        configured = (
            f"{'https' if location.config.get('use_ssl') else 'http'}://{configured}"
        )
    if endpoint_identity(configured) != endpoint_identity(
        endpoint
    ) or endpoint_identity(
        storage_service.client.meta.endpoint_url
    ) != endpoint_identity(endpoint):
        reject("inventory_source_changed")
    return {
        "endpoint": endpoint,
        "location_revision": location.config_revision,
        "purpose_buckets": {
            purpose: getattr(storage_service, attribute)
            for purpose, attribute in PURPOSE_BUCKET_ATTRIBUTES.items()
        },
    }


async def by_job(db, job_id, *, lock=False):
    statement = (
        select(Inventory)
        .where(Inventory.async_job_id == job_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


def matches(row, job):
    return bool(
        job
        and job.id == row.async_job_id
        and job.kind == KIND
        and job.status == row.status
        and job.celery_task_id == row.task_id
        and job.user_id == row.requested_by
        and {
            key: value
            for key, value in job.payload.items()
            if key not in {"cancel_requested", "cancel_requested_at"}
        }
        == payload(row)
    )


def payload(row):
    result = {"scope": "selected-configured-source", "migration_ready": False}
    if row.window_id is not None:
        result.update(window_id=str(row.window_id), window_revision=row.window_revision)
    return result


async def source_revision(db, source, window_id, window_revision, *, lock=False):
    if window_id is None:
        if window_revision is not None:
            reject("inventory_context_changed")
        return await gate_revision(db, LEGACY_LOCATION_ID, lock=lock)
    from app.services import storage_migration_windows as windows

    return await windows.check_source(db, window_id, window_revision, source, lock=lock)


async def gate_revision(db, location_id, *, lock=False):
    from app.services.content_snapshot import ContentError

    try:
        return await storage_access.check(db, location_id, lock=lock)
    except ContentError as exc:
        reject(exc.code, exc.status)


def item(row):
    return StorageMigrationInventoryItem(
        job_id=row.async_job_id,
        window_id=row.window_id,
        window_revision=row.window_revision,
        status=row.status,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        source_location_id=row.source_location_id,
        limits=row.limits,
        handling={
            purpose: row.handling.get(purpose, "unreviewed")
            for purpose in sorted(PURPOSES)
        },
        cancel_requested=row.cancel_requested_at is not None,
        complete=row.complete,
        counts=row.counts,
        error_code=row.error_code,
        report_available=row.report_sha256 is not None,
        report_sha256=row.report_sha256,
        report_size_bytes=row.report_size_bytes,
    )


async def history(db, actor, *, api_key_id=None, limit=20, offset=0):
    actor, proof = await authority(db, actor, api_key_id)
    total = await db.scalar(select(func.count()).select_from(Inventory))
    rows = list(
        await db.scalars(
            select(Inventory)
            .order_by(Inventory.created_at.desc(), Inventory.async_job_id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(populate_existing=True)
        )
    )
    _, current = await authority(db, actor, api_key_id)
    if current != proof:
        reject("inventory_authority_changed", 403)
    return StorageMigrationInventoryHistory(
        items=[item(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def start(db, actor, request_body=None, *, api_key_id=None, request=None):
    body = request_body or StorageMigrationInventoryRequest()
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    # A transaction advisory lock serializes starts without retaining a source
    # lock across native reads. The partial unique index independently fences it.
    await db.execute(text("SELECT pg_advisory_xact_lock(8592650717001)"))
    source = await source_scope(db, lock=True)
    window_revision = None
    if body.window_id is not None:
        from app.db.models.storage_migration_window import StorageMigrationWindow
        from app.services import storage_migration_windows as windows

        revision = await windows.source_revision(db, body.window_id, source, lock=True)
        window = await db.get(StorageMigrationWindow, body.window_id)
        window_revision = window.revision
    else:
        revision = await gate_revision(db, LEGACY_LOCATION_ID, lock=True)
    limits, handling = body.limits.model_dump(), dict(body.handling)
    inputs = [source, limits, handling]
    if body.window_id is not None:
        inputs.extend([str(body.window_id), window_revision])
    fingerprint = digest(inputs)
    row = await db.scalar(
        select(Inventory)
        .where(
            Inventory.source_location_id == LEGACY_LOCATION_ID,
            Inventory.status.in_(ACTIVE),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is not None:
        job = await jobs.lock_job(db, row.async_job_id)
        if not matches(row, job):
            reject("inventory_job_unavailable")
        if row.requested_by != actor.id or row.input_fingerprint != fingerprint:
            reject("inventory_active_conflict")
        return row, job, True
    async with db.begin_nested():
        task_id = str(uuid.uuid4())
        job = await jobs.create_job(
            db,
            kind=KIND,
            user_id=actor.id,
            celery_task_id=task_id,
            payload=payload(
                SimpleNamespace(
                    window_id=body.window_id, window_revision=window_revision
                )
            ),
        )
        job.result = {"stage": "queued", "migration_ready": False}
        row = Inventory(
            async_job_id=job.id,
            requested_by=actor.id,
            task_id=task_id,
            source_location_id=LEGACY_LOCATION_ID,
            source=source,
            window_id=body.window_id,
            window_revision=window_revision,
            limits=limits,
            handling=handling,
            authority={
                "api_key_id": str(api_key_id) if api_key_id else None,
                "key_proof": proof,
            },
            input_fingerprint=fingerprint,
            maintenance_revision=revision,
            status="pending",
        )
        db.add(row)
        await db.flush()
        await AuditService.log(
            db,
            actor=actor,
            action="storage.migration_inventory_start",
            target_type="async_job",
            target_id=str(job.id),
            request=request,
            status_code=202,
            detail={"migration_ready": False},
        )
    return row, job, False


async def redispatch(db, job_id, actor, *, api_key_id=None):
    await authority(db, actor, api_key_id, lock=True)
    row = await by_job(db, job_id, lock=True)
    if row is None:
        raise HTTPException(404, "Inventory not found")
    job = await jobs.lock_job(db, job_id)
    if not matches(row, job):
        reject("inventory_job_unavailable")
    if row.window_id is not None:
        await current(db, row, lock=True)
    return row, job


async def finish(db, row, job, status, *, error_code=None, report=None):
    if row.status not in ACTIVE:
        return False
    row.status, row.completed_at, row.error_code = status, now(), error_code
    if status == "cancelled" and row.cancel_requested_at is None:
        row.cancel_requested_at = row.completed_at
    if report is not None:
        raw, tail = report
        row.report, row.report_sha256, row.report_size_bytes = (
            raw,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        )
        row.complete, row.counts = tail["complete"], tail["counts"]
    job.status, job.completed_at, job.error_message = (
        status,
        row.completed_at,
        error_code,
    )
    job.progress_pct = 100 if status == "completed" else job.progress_pct
    job.result = {
        "stage": "sealed" if report is not None else "ended",
        "migration_ready": False,
        "complete": row.complete,
        "counts": row.counts,
        "error_code": error_code,
        "cancel_requested": row.cancel_requested_at is not None,
        "report_available": report is not None,
        "report_sha256": row.report_sha256,
        "report_size_bytes": row.report_size_bytes,
    }
    await db.flush()
    return True


async def cancel(db, job_id):
    row = await by_job(db, job_id, lock=True)
    if row is None:
        raise HTTPException(404, "Inventory not found")
    job = await jobs.lock_job(db, job_id)
    if row.status not in ACTIVE:
        if row.status == "cancelled":
            return "cancelled"
        reject("inventory_terminal")
    if not matches(row, job):
        reject("inventory_job_unavailable")
    row.cancel_requested_at = row.cancel_requested_at or now()
    if row.status == "pending":
        await finish(db, row, job, "cancelled")
        return "cancelled"
    job.result = {**job.result, "cancel_requested": True}
    await jobs.request_cancel(db, job_id)
    await db.flush()
    return "cancel_requested"


async def current(db, row, *, lock=False):
    await attempt_authority(db, row, lock=lock)
    if (
        row.source_location_id != LEGACY_LOCATION_ID
        or await source_scope(db, lock=lock) != row.source
    ):
        reject("inventory_source_changed")
    revision = await source_revision(
        db, row.source, row.window_id, row.window_revision, lock=lock
    )
    if revision != row.maintenance_revision:
        reject("storage_maintenance_changed")


async def claim(db, job_id, task_id):
    row = await by_job(db, job_id)
    if row is None:
        return None
    failure = None
    try:
        await current(db, row, lock=True)
    except (HTTPException, ValueError) as exc:
        failure = safe_error(exc)
    row = await by_job(db, job_id, lock=True)
    job = await jobs.lock_job(db, job_id)
    if row.status != "pending" or row.task_id != task_id or not matches(row, job):
        return None
    if row.cancel_requested_at is not None:
        await finish(db, row, job, "cancelled")
        return None
    if failure:
        await finish(db, row, job, "failed", error_code=failure)
        return None
    row.status, row.started_at = "running", now()
    job.status, job.started_at = "running", row.started_at
    job.result = {"stage": "reading", "migration_ready": False}
    await db.flush()
    return row


async def acquired(db, job_id, task_id, permit):
    row = await by_job(db, job_id)
    await current(db, row, lock=True)
    row = await by_job(db, job_id, lock=True)
    job = await jobs.lock_job(db, job_id)
    operation = await db.get(StorageOperation, permit.id, populate_existing=True)
    if (
        row.status != "running"
        or row.task_id != task_id
        or row.operation_id is not None
        or not matches(row, job)
        or row.cancel_requested_at is not None
        or operation is None
        or permit.location_id != LEGACY_LOCATION_ID
        or permit.revision != row.maintenance_revision
        or operation.purpose != KIND
    ):
        reject("inventory_context_changed")
    row.operation_id = permit.id
    await db.commit()


def safe_error(exc):
    code = (
        exc.detail.get("code")
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict)
        else getattr(exc, "code", None)
    )
    return (
        code
        if isinstance(code, str) and SAFE_CODE.fullmatch(code)
        else "inventory_read_failed"
    )


async def checkpoint(db, job_id, task_id):
    row = await by_job(db, job_id)
    if row is None or row.status != "running" or row.task_id != task_id:
        reject("inventory_context_changed")
    if row.cancel_requested_at is not None:
        reject("inventory_cancelled")
    await current(db, row)


async def publish(
    db, job_id, task_id, *, report=None, error_code=None, cancelled=False
):
    row = await by_job(db, job_id)
    if row is None:
        return False
    failure = None
    try:
        await current(db, row, lock=True)
    except (HTTPException, ValueError) as exc:
        failure = safe_error(exc)
    row = await by_job(db, job_id, lock=True)
    job = await jobs.lock_job(db, job_id)
    if row.status != "running" or row.task_id != task_id or not matches(row, job):
        return False
    if row.operation_id and await db.get(
        StorageOperation, row.operation_id, populate_existing=True
    ):
        return await finish(
            db, row, job, "failed", error_code="storage_operation_unsettled"
        )
    if cancelled or row.cancel_requested_at is not None:
        return await finish(db, row, job, "cancelled")
    if failure or error_code:
        return await finish(db, row, job, "failed", error_code=failure or error_code)
    if row.operation_id is None or report is None:
        return await finish(
            db, row, job, "failed", error_code="inventory_invalid_result"
        )
    raw = report
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= REPORT_LIMIT:
            raise ValueError()
        tail = verify_inventory(stream=io.BytesIO(raw))
        header = json.loads(raw[: raw.index(b"\n")])
        expected_limits = {
            key: value for key, value in row.limits.items() if key != "max_seconds"
        }
        expected_limits["chunk_bytes"] = 1024**2
        if (
            header.get("purpose_buckets") != row.source["purpose_buckets"]
            or header.get("source", {}).get("endpoint") != row.source["endpoint"]
            or header.get("handling")
            != {
                purpose: row.handling.get(purpose, "unreviewed") for purpose in PURPOSES
            }
            or header.get("limits") != expected_limits
            or header.get("observations", {}).get("migration_window")
            != (
                {
                    "window_id": str(row.window_id),
                    "window_revision": row.window_revision,
                    "source_quiescence_proven": False,
                }
                if row.window_id is not None
                else None
            )
        ):
            raise ValueError()

        from app.schemas.storage_migration_inventory import (
            StorageMigrationInventoryCounts,
        )

        StorageMigrationInventoryCounts.model_validate(tail["counts"])
    except (ValueError, TypeError, KeyError):
        return await finish(
            db, row, job, "failed", error_code="inventory_invalid_result"
        )
    return await finish(
        db,
        row,
        job,
        "completed" if tail["complete"] else "failed",
        error_code=None if tail["complete"] else "inventory_incomplete",
        report=(raw, tail),
    )


async def download(db, job_id, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    row = await db.scalar(
        select(Inventory)
        .where(Inventory.async_job_id == job_id)
        .options(undefer(Inventory.report))
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "Inventory not found")
    if row.report is None:
        reject("inventory_report_unavailable")
    raw = row.report
    if (
        len(raw) != row.report_size_bytes
        or hashlib.sha256(raw).hexdigest() != row.report_sha256
    ):
        reject("inventory_report_integrity")
    _, current_proof = await authority(db, actor, api_key_id)
    if current_proof != proof:
        reject("inventory_authority_changed", 403)
    return raw, row.report_sha256


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    row = await by_job(db, job_id, lock=True)
    if row is None or row.status not in ACTIVE or row.task_id != task_id:
        return False
    job = await jobs.lock_job(db, job_id)
    if not matches(row, job):
        return False
    # Signals are not evidence of a native handle closing. Keep the independent
    # operation receipt; never publish a downloadable report from this fallback.
    return await finish(
        db,
        row,
        job,
        "cancelled" if cancelled and row.status == "pending" else "failed",
        error_code=None
        if cancelled and row.status == "pending"
        else "inventory_interrupted",
    )
