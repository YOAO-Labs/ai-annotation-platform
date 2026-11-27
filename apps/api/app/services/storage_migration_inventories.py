"""Claim-once fixed-source inventory lifecycle and private report publication."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import re
from types import SimpleNamespace
from typing import Literal, overload
import uuid

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from yoao_jobs.lifecycle import JobsLifecycleManager

from app.db.models.storage_migration_inventory import REPORT_LIMIT
from app.schemas.storage_migration_inventory import (
    StorageMigrationInventoryHistory,
    StorageMigrationInventoryItem,
    StorageMigrationInventoryRequest,
)
from app.services import jobs_lifecycle_data as jobs
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.storage_identity import LEGACY_LOCATION_ID
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.jobs_lifecycle_data import (
    JobsLifecyclePostgresData,
    job_lock_statement,
)
from app.services.storage_migration_guards import reject
from app.services.storage_migration_inventory import PURPOSES, verify_inventory
from app.services.storage_migration_window_access import (
    check_source,
    owner as window_owner,
    source_revision as window_source_revision,
    source_scope as source_scope,
)

KIND = "storage_migration_inventory"
ACTIVE = {"pending", "running"}
SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")

_BY_JOB = text("SELECT * FROM yoao_assets_v1.read_migration_inventory(:job_id, :lock)")
_PRIVATE_BY_JOB = text(
    "SELECT * FROM yoao_assets_v1.read_migration_inventory_report(:job_id, :lock)"
)
_ACTIVE = text(
    "SELECT facts.* FROM "
    "yoao_assets_v1.lock_active_migration_inventory() AS active(job_id) "
    "CROSS JOIN LATERAL "
    "yoao_assets_v1.read_migration_inventory(active.job_id, false) AS facts"
)
_QUEUE = text(
    "SELECT yoao_assets_v1.queue_migration_inventory("
    ":job_id, :actor_id, :task_id, :window_id, :window_revision, :revision, "
    ":source, :limits, :handling, :authority, :fingerprint)"
).bindparams(
    *(
        bindparam(key, type_=JSONB)
        for key in ("source", "limits", "handling", "authority")
    )
)
_CLAIM = text(
    "SELECT yoao_assets_v1.claim_migration_inventory(:job_id, :task_id, :started_at)"
)
_CANCEL = text(
    "SELECT yoao_assets_v1.request_migration_inventory_cancel("
    ":job_id, :task_id, :requested_at)"
)
_ATTACH = text(
    "SELECT yoao_assets_v1.attach_migration_inventory_operation("
    ":job_id, :task_id, :operation_id)"
)
_FINISH = text(
    "SELECT yoao_assets_v1.end_migration_inventory("
    ":job_id, :task_id, :expected_status, :status, :completed_at, :error_code)"
)
_SEAL = text(
    "SELECT yoao_assets_v1.seal_migration_inventory("
    ":job_id, :task_id, :completed_at, :report, :sha256, :complete, :counts, :error_code)"
).bindparams(bindparam("counts", type_=JSONB))
_HISTORY = text(
    "SELECT * FROM yoao_assets_v1.migration_inventory_history(:limit, :offset)"
)
_HISTORY_COUNT = text("SELECT yoao_assets_v1.migration_inventory_history_count()")
_ACTIVE_WINDOW = text(
    "SELECT yoao_assets_v1.migration_window_has_active_inventory(:window_id)"
)


@dataclass(frozen=True, slots=True)
class InventorySummary:
    async_job_id: uuid.UUID
    window_id: uuid.UUID | None
    window_revision: int | None
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    source_location_id: uuid.UUID
    limits: dict[str, int]
    handling: dict[str, str]
    cancel_requested_at: datetime | None
    complete: bool | None
    counts: dict[str, int] | None
    error_code: str | None
    report_sha256: str | None
    report_size_bytes: int | None


@dataclass(frozen=True, slots=True)
class InventoryFacts(InventorySummary):
    requested_by: uuid.UUID
    task_id: str
    source: dict
    authority: dict[str, str | None]
    input_fingerprint: str
    maintenance_revision: int
    operation_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class InventoryReport(InventoryFacts):
    report: bytes | None


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def authority(db, actor, api_key_id=None, *, lock=False):
    return await storage_maintenance.authority(db, actor, api_key_id, "*", lock=lock)


async def attempt_authority(db, row, *, lock=False):
    actor = await IdentityAccountLockData(db).read_actor_status(row.requested_by)
    if actor is None:
        reject("inventory_authority_changed", 403)
    actor, proof = await authority(
        db, actor, row.authority.get("api_key_id"), lock=lock
    )
    if proof != row.authority.get("key_proof"):
        reject("inventory_authority_changed", 403)
    return actor


@overload
async def by_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    lock: bool = False,
    private: Literal[False] = False,
) -> InventoryFacts | None: ...


@overload
async def by_job(
    db: AsyncSession, job_id: uuid.UUID, *, lock: bool = False, private: Literal[True]
) -> InventoryReport | None: ...


async def by_job(
    db: AsyncSession, job_id: uuid.UUID, *, lock: bool = False, private: bool = False
) -> InventoryFacts | None:
    if db.autoflush:
        await db.flush()
    query, facts = (
        (_PRIVATE_BY_JOB, InventoryReport) if private else (_BY_JOB, InventoryFacts)
    )
    row = (
        (await db.execute(query, {"job_id": job_id, "lock": lock}))
        .mappings()
        .one_or_none()
    )
    return facts(**row) if row is not None else None


async def active_for_window(db, window_id) -> bool:
    return await db.scalar(_ACTIVE_WINDOW, {"window_id": window_id})


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
    return await check_source(db, window_id, window_revision, source, lock=lock)


async def gate_revision(db, location_id, *, lock=False):
    from app.services.content_snapshot import ContentError

    try:
        return await storage_access.check(db, location_id, lock=lock)
    except ContentError as exc:
        reject(exc.code, exc.status)


def item(row: InventorySummary) -> StorageMigrationInventoryItem:
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
    total = await db.scalar(_HISTORY_COUNT)
    rows = (
        (await db.execute(_HISTORY, {"limit": limit, "offset": offset}))
        .mappings()
        .all()
    )
    _, current = await authority(db, actor, api_key_id)
    if current != proof:
        reject("inventory_authority_changed", 403)
    return StorageMigrationInventoryHistory(
        items=[item(InventorySummary(**row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def start(db, actor, request_body=None, *, api_key_id=None, request=None):
    body = request_body or StorageMigrationInventoryRequest()
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await db.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(8592650717001)"))
    source = await source_scope(db, lock=True)
    window_revision = None
    if body.window_id is not None:
        revision = await window_source_revision(db, body.window_id, source, lock=True)
        window = await window_owner(db, body.window_id)
        window_revision = window.revision
    else:
        revision = await gate_revision(db, LEGACY_LOCATION_ID, lock=True)
    limits, handling = body.limits.model_dump(), dict(body.handling)
    inputs = [source, limits, handling]
    if body.window_id is not None:
        inputs.extend([str(body.window_id), window_revision])
    fingerprint = digest(inputs)
    active = (await db.execute(_ACTIVE)).mappings().one_or_none()
    row = InventoryFacts(**active) if active is not None else None
    if row is not None:
        job = await db.scalar(
            job_lock_statement(row.async_job_id, mode="update", populate_existing=True)
        )
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
        await jobs.replace_result(
            db, job.id, result={"stage": "queued", "migration_ready": False}
        )
        reserved = await db.scalar(
            _QUEUE,
            {
                "job_id": job.id,
                "actor_id": actor.id,
                "task_id": task_id,
                "window_id": body.window_id,
                "window_revision": window_revision,
                "revision": revision,
                "source": source,
                "limits": limits,
                "handling": handling,
                "authority": {
                    "api_key_id": str(api_key_id) if api_key_id else None,
                    "key_proof": proof,
                },
                "fingerprint": fingerprint,
            },
        )
        if not reserved:
            reject("inventory_context_changed")
        row = await by_job(db, job.id)
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
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, job):
        reject("inventory_job_unavailable")
    if row.window_id is not None:
        await current(db, row, lock=True)
    return row, job


async def finish(db, row: InventoryFacts, job, status, *, error_code=None, report=None):
    if row.status not in ACTIVE:
        return False
    completed_at = now()
    values = {
        "job_id": row.async_job_id,
        "task_id": row.task_id,
        "completed_at": completed_at,
        "error_code": error_code,
    }
    if report is None:
        changed = await db.scalar(
            _FINISH, {**values, "expected_status": row.status, "status": status}
        )
        complete, counts = row.complete, row.counts
        sha256, size = row.report_sha256, row.report_size_bytes
    else:
        raw, tail = report
        sha256, size = hashlib.sha256(raw).hexdigest(), len(raw)
        complete, counts = tail["complete"], tail["counts"]
        changed = await db.scalar(
            _SEAL,
            {
                **values,
                "report": raw,
                "sha256": sha256,
                "complete": complete,
                "counts": counts,
            },
        )
    if not changed:
        return False
    result = {
        "stage": "sealed" if report is not None else "ended",
        "migration_ready": False,
        "complete": complete,
        "counts": counts,
        "error_code": error_code,
        "cancel_requested": row.cancel_requested_at is not None
        or status == "cancelled",
        "report_available": report is not None,
        "report_sha256": sha256,
        "report_size_bytes": size,
    }
    lifecycle = JobsLifecycleManager(
        JobsLifecyclePostgresData(db), clock=lambda: completed_at
    )
    if status == "completed":
        lifecycle.mark_complete(job, result=result)
    elif status == "cancelled":
        lifecycle.mark_cancelled(job, result=result)
    else:
        lifecycle.mark_failed(job, error=error_code, result=result)
    await db.flush()
    return True


async def cancel(db, job_id):
    row = await by_job(db, job_id, lock=True)
    if row is None:
        raise HTTPException(404, "Inventory not found")
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if row.status not in ACTIVE:
        if row.status == "cancelled":
            return "cancelled"
        reject("inventory_terminal")
    if not matches(row, job):
        reject("inventory_job_unavailable")
    if row.status == "pending":
        if not await finish(db, row, job, "cancelled"):
            reject("inventory_context_changed")
        return "cancelled"
    if row.cancel_requested_at is None and not await db.scalar(
        _CANCEL, {"job_id": job_id, "task_id": row.task_id, "requested_at": now()}
    ):
        reject("inventory_context_changed")
    await jobs.replace_result(
        db, job_id, result={**job.result, "cancel_requested": True}
    )
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
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if (
        row is None
        or row.status != "pending"
        or row.task_id != task_id
        or not matches(row, job)
    ):
        return None
    if row.cancel_requested_at is not None:
        await finish(db, row, job, "cancelled")
        return None
    if failure:
        await finish(db, row, job, "failed", error_code=failure)
        return None
    started_at = now()
    changed = await db.scalar(
        _CLAIM, {"job_id": job_id, "task_id": task_id, "started_at": started_at}
    )
    if not changed:
        return None
    lifecycle = JobsLifecycleManager(
        JobsLifecyclePostgresData(db), clock=lambda: started_at
    )
    lifecycle.mark_running(job)
    lifecycle.replace_result(job, result={"stage": "reading", "migration_ready": False})
    await db.flush()
    return replace(row, status="running", started_at=started_at)


async def acquired(db, job_id, task_id, permit):
    row = await by_job(db, job_id)
    if row is None:
        reject("inventory_context_changed")
    await current(db, row, lock=True)
    row = await by_job(db, job_id, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if (
        row is None
        or row.status != "running"
        or row.task_id != task_id
        or row.operation_id is not None
        or not matches(row, job)
        or row.cancel_requested_at is not None
        or permit.location_id != LEGACY_LOCATION_ID
        or permit.revision != row.maintenance_revision
    ):
        reject("inventory_context_changed")
    if not await db.scalar(
        _ATTACH, {"job_id": job_id, "task_id": task_id, "operation_id": permit.id}
    ):
        reject("inventory_context_changed")
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
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if (
        row is None
        or row.status != "running"
        or row.task_id != task_id
        or not matches(row, job)
    ):
        return False
    if row.operation_id and await storage_access.operation_receipt(
        db, row.operation_id
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
    row = await by_job(db, job_id, private=True)
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
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if not matches(row, job):
        return False
    return await finish(
        db,
        row,
        job,
        "cancelled" if cancelled and row.status == "pending" else "failed",
        error_code=None
        if cancelled and row.status == "pending"
        else "inventory_interrupted",
    )
