"""Platform-only current-object observations, with no object I/O on reads."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.enums import UserRole
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_usage import (
    StorageUsageInventory as Inventory,
    StorageUsageSnapshot as Snapshot,
    StorageUsageSchedule as Schedule,
)
from app.db.models.user import User
from app.schemas.storage import BucketSummary, BucketsResponse
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)
from app.services.dataset_content import key_authority
from app.services.storage import storage_service
from app.services.sources.s3_reference import S3Access
from app.services.storage_usage_categories import summarize
from app.services import storage_usage_accounting as accounting

ACTIVE = {"pending", "running"}
LEASE_SECONDS = 120
MAX_PAGES = 10000


def now():
    return datetime.now(timezone.utc)


def configured_scope():
    buckets = {}
    for role, attr in PURPOSE_BUCKET_ATTRIBUTES.items():
        name = getattr(storage_service, attr)
        if name not in buckets:
            key = hashlib.sha256(
                json.dumps(
                    [settings.minio_endpoint, settings.minio_use_ssl, name]
                ).encode()
            ).hexdigest()
            buckets[name] = {"key": key, "name": name, "roles": []}
        buckets[name]["roles"].append(role)
    return list(buckets.values())


async def require_admin(db, actor_id):
    actor = await db.get(User, actor_id, populate_existing=True)
    if actor is None or not actor.is_active or actor.role != UserRole.SUPER_ADMIN:
        raise HTTPException(403, detail={"code": "access_revoked"})
    return actor


async def authority(db, inventory):
    proof = inventory.authority
    actor = await require_admin(db, uuid.UUID(proof["actor_id"]))
    if await key_authority(db, actor, proof.get("api_key_id"), "*") != proof.get(
        "key_proof"
    ):
        raise HTTPException(403, detail={"code": "access_revoked"})
    if inventory.scope != configured_scope() or await validate_legacy_bindings(db):
        raise HTTPException(409, detail={"code": "configuration_changed"})


async def uncovered_bindings(db, scope):
    """Compare saved configuration only; this read never probes an endpoint."""
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    ).rstrip("/")
    buckets = {target["name"] for target in scope}
    uncovered = set(PURPOSE_BUCKET_ATTRIBUTES)
    for binding, location, connection in await db.execute(
        select(StorageBinding, StorageLocation, StorageConnection)
        .outerjoin(StorageLocation, StorageLocation.id == StorageBinding.location_id)
        .outerjoin(
            StorageConnection, StorageConnection.id == StorageLocation.connection_id
        )
        .where(StorageBinding.purpose.in_(PURPOSE_BUCKET_ATTRIBUTES))
    ):
        if location is None or location.kind != "s3" or binding.bucket not in buckets:
            continue
        if location.id == LEGACY_LOCATION_ID and location.connection_id is None:
            config = location.config
        elif (
            connection is not None
            and connection.kind == "s3"
            and connection.scope == "global"
            and connection.config.get("bucket") == binding.bucket
        ):
            config = connection.config
        else:
            continue
        try:
            if S3Access(config, b"", ()).endpoint == endpoint:
                uncovered.discard(binding.purpose)
        except (ValueError, KeyError, TypeError):
            # Invalid configuration is unknown coverage, never evidence of zero.
            continue
    return sorted(uncovered)


async def read_usage(db):
    scope = configured_scope()
    # Read the task state before its observations: a terminal state must never
    # be paired with pre-completion rows and stop the client polling too early.
    inventory = await db.get(Inventory, "platform", populate_existing=True)
    rows = {
        r.scope_key: r
        for r in await db.scalars(
            select(Snapshot).where(Snapshot.scope_key.in_([s["key"] for s in scope]))
        )
    }
    items = []
    for target in scope:
        row = rows.get(target["key"])
        measured = row is not None and row.measured_at is not None
        stale = measured and (
            row.measured_at <= now() - timedelta(hours=24)
            or row.attempt_status in {"failed", "cancelled"}
        )
        items.append(
            BucketSummary(
                name=target["name"],
                role=target["roles"][0],
                roles=target["roles"],
                status="stale" if stale else "ok" if measured else "unknown",
                object_count=row.object_count if measured else None,
                total_size_bytes=row.total_size_bytes if measured else None,
                measured_at=row.measured_at if measured else None,
                measurement_started_at=row.measurement_started_at if measured else None,
                attempt_status=row.attempt_status if row else "never",
                attempted_at=row.attempted_at if row else None,
                error=row.error_code if row else None,
                coverage="complete" if measured else "none",
            )
        )
    count = sum(i.measured_at is not None for i in items)
    ledger, reconciliation, schedule = await accounting.summaries(db, scope)
    return BucketsResponse(
        accounting=ledger,
        reconciliation=reconciliation,
        usage_schedule=schedule,
        items=items,
        total_object_count=sum(i.object_count for i in items)
        if count == len(items)
        else None,
        total_size_bytes=sum(i.total_size_bytes for i in items)
        if count == len(items)
        else None,
        measured_bucket_count=count,
        bucket_count=len(items),
        location_id=LEGACY_LOCATION_ID,
        inventory_job_id=inventory.async_job_id if inventory else None,
        inventory_status=inventory.status if inventory else None,
        uncovered_binding_purposes=await uncovered_bindings(db, scope),
        breakdown=summarize(
            [
                (rows[target["key"]], item.status)
                for target, item in zip(scope, items)
                if target["key"] in rows
            ],
            len(scope),
        ),
    )


async def locked(db):
    return await db.scalar(
        select(Inventory)
        .where(Inventory.id == "platform")
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def start(db, actor, api_key_id=None):
    actor = await require_admin(db, actor.id)
    proof = await key_authority(db, actor, api_key_id, "*")
    if await validate_legacy_bindings(db):
        raise HTTPException(409, detail={"code": "configuration_changed"})
    await db.execute(
        insert(Inventory)
        .values(
            id="platform",
            status="never",
            authority={},
            scope=[],
            bucket_index=0,
            checkpoint={},
            success_count=0,
            failed_count=0,
            generation=0,
            started_at=now(),
        )
        .on_conflict_do_nothing()
    )
    inventory = await locked(db)
    if inventory.status in ACTIVE:
        job = await db.get(AsyncJob, inventory.async_job_id, populate_existing=True)
        if job and job.status in ACTIVE:
            return job, True
        await finish(db, inventory, "failed", "worker_failed")
    scope = configured_scope()
    for target in scope:
        await db.execute(
            insert(Snapshot)
            .values(scope_key=target["key"], attempt_status="never")
            .on_conflict_do_nothing()
        )
        row = await db.get(Snapshot, target["key"], populate_existing=True)
        row.attempt_status, row.attempted_at, row.error_code = "pending", now(), None
    inventory.scope, inventory.status = scope, "pending"
    inventory.authority = {
        "actor_id": str(actor.id),
        "api_key_id": str(api_key_id) if api_key_id else None,
        "key_proof": proof,
    }
    inventory.bucket_index, inventory.checkpoint = 0, {}
    inventory.success_count = inventory.failed_count = 0
    inventory.generation += 1
    inventory.started_at = now()
    inventory.lease_until = now() + timedelta(seconds=LEASE_SECONDS)
    job = await jobs.create_job(
        db,
        kind="storage_usage",
        user_id=actor.id,
        celery_task_id=str(uuid.uuid4()),
        payload={"scope": "platform_current_objects", "bucket_count": len(scope)},
    )
    inventory.async_job_id = job.id
    return job, False


async def fenced(db, job_id, task_id, generation):
    inventory = await locked(db)
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        not inventory
        or not job
        or inventory.async_job_id != job_id
        or inventory.generation != generation
        or inventory.status not in ACTIVE
        or job.status not in ACTIVE
        or job.celery_task_id != task_id
        or not inventory.lease_until
        or inventory.lease_until <= now()
    ):
        return None
    return inventory


def result(inventory):
    return {
        "success_count": inventory.success_count,
        "failed_count": inventory.failed_count,
        "bucket_count": len(inventory.scope),
        "stage": "enumerating",
    }


async def finish(db, inventory, status, error=None):
    if inventory.async_job_id:
        await accounting.discard_staging(db, inventory.async_job_id)
    for target in inventory.scope[inventory.bucket_index :]:
        row = await db.get(Snapshot, target["key"], populate_existing=True)
        if row:
            row.attempt_status, row.error_code = status, error
    inventory.status, inventory.lease_until = status, None
    inventory.generation += 1
    if inventory.async_job_id:
        summary = result(inventory)
        if status == "completed":
            await jobs.mark_complete(db, inventory.async_job_id, result=summary)
        elif status == "cancelled":
            await jobs.mark_cancelled(db, inventory.async_job_id, result=summary)
        else:
            await jobs.mark_failed(
                db,
                inventory.async_job_id,
                error=error or "inventory_failed",
                result=summary,
            )
        await notify_job_terminal(db, job_id=inventory.async_job_id)


async def cancel(db, job_id):
    inventory = await locked(db)
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        not inventory
        or inventory.async_job_id != job_id
        or inventory.status not in ACTIVE
        or not job
        or job.status not in ACTIVE
    ):
        raise HTTPException(409, "盘点任务已结束")
    await finish(db, inventory, "cancelled", "cancelled")


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    inventory = await locked(db)
    job = await db.get(AsyncJob, job_id, populate_existing=True)
    if (
        not inventory
        or inventory.async_job_id != job_id
        or not job
        or job.celery_task_id != task_id
        or inventory.status not in ACTIVE
        or job.status not in ACTIVE
    ):
        return
    if cancelled:
        await finish(db, inventory, "cancelled", "cancelled")
        return
    # A process-loss signal fences that delivery, but keeps its durable page checkpoint.
    # Beat republishes it after this commit; late signals for the old task ID are ignored.
    inventory.status = "pending"
    inventory.generation += 1
    inventory.lease_until = now()
    job.celery_task_id = str(uuid.uuid4())


async def configure_schedule(db, actor, enabled, interval_hours, api_key_id=None):
    actor = await require_admin(db, actor.id)
    proof = await key_authority(db, actor, api_key_id, "*")
    if enabled and await validate_legacy_bindings(db):
        raise HTTPException(409, detail={"code": "configuration_changed"})
    await db.execute(
        insert(Schedule)
        .values(id="platform", authority={}, scope=[])
        .on_conflict_do_nothing()
    )
    row = await db.scalar(
        select(Schedule).where(Schedule.id == "platform").with_for_update()
    )
    row.enabled, row.interval_hours = enabled, interval_hours
    row.status, row.pause_reason = ("active" if enabled else "off"), None
    row.next_due_at = now() if enabled else None
    row.authority = {
        "actor_id": str(actor.id),
        "api_key_id": str(api_key_id) if api_key_id else None,
        "key_proof": proof,
    }
    row.scope = configured_scope()
    await db.flush()
    return accounting.schedule_summary(row)


async def start_due(db):
    row = await db.scalar(
        select(Schedule)
        .where(
            Schedule.id == "platform",
            Schedule.enabled.is_(True),
            Schedule.status == "active",
        )
        .with_for_update(skip_locked=True)
    )
    if row is None:
        await db.commit()
        return None
    try:
        await authority(db, row)
    except HTTPException as exc:
        row.status = "paused"
        row.pause_reason = (
            "access_revoked"
            if exc.status_code in {401, 403, 404}
            else "configuration_changed"
        )
        row.next_due_at = None
        await db.commit()
        return None
    if row.next_due_at is None or row.next_due_at > now():
        await db.commit()
        return None
    # start() takes the same inventory lock as manual starts. Reuse is not a
    # new scheduled delivery and must not move the next due time.
    actor = await require_admin(db, uuid.UUID(row.authority["actor_id"]))
    key_id = row.authority.get("api_key_id")
    job, reused = await start(db, actor, uuid.UUID(key_id) if key_id else None)
    if reused:
        await db.commit()
        return None
    row.last_started_at = now()
    row.next_due_at = row.last_started_at + timedelta(hours=row.interval_hours)
    await db.commit()
    return job
