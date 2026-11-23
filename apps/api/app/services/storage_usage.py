"""Platform-only current-object observations, with no object I/O on reads."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.db.enums import UserRole
from app.schemas.storage import BucketSummary, BucketsResponse
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)
from app.services.dataset_content import key_authority
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.storage import storage_service
from app.services.source_readers.s3 import S3Access
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
    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not actor.is_active or actor.role != UserRole.SUPER_ADMIN.value:
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
    for row in await db.execute(
        text("SELECT * FROM yoao_assets_v1.migration_inventory_binding_context()")
    ):
        if (
            row.purpose not in uncovered
            or row.location_kind != "s3"
            or row.bucket not in buckets
        ):
            continue
        if row.location_id == LEGACY_LOCATION_ID and row.connection_id is None:
            config = row.location_config
        elif (
            row.connection_kind == "s3"
            and row.connection_scope == "global"
            and row.connection_config.get("bucket") == row.bucket
        ):
            config = row.connection_config
        else:
            continue
        try:
            if S3Access(config, b"", ()).endpoint == endpoint:
                uncovered.discard(row.purpose)
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(uncovered)


async def read_usage(db):
    scope = configured_scope()
    inventory = (
        await db.execute(text("SELECT * FROM yoao_assets_v1.usage_inventory_status()"))
    ).one_or_none()
    rows = {
        row.scope_key: row
        for row in await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.usage_snapshot_facts(CAST(:keys AS text[]))"
            ),
            {"keys": [item["key"] for item in scope]},
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


async def locked(db, *, bootstrap_at=None):
    return (
        await db.execute(
            text("SELECT * FROM yoao_assets_v1.lock_usage_inventory(:at)"),
            {"at": bootstrap_at},
        )
    ).one_or_none()


async def expired(db):
    return (
        await db.execute(
            text("SELECT * FROM yoao_assets_v1.lock_expired_usage_inventory(:at)"),
            {"at": now()},
        )
    ).one_or_none()


async def _transition(db, statement, params):
    if not await db.scalar(text(statement), params):
        raise HTTPException(409, detail={"code": "inventory_changed"})


async def claim(db, inventory):
    await _transition(
        db,
        "SELECT yoao_assets_v1.claim_usage_inventory(:job, :generation, :lease)",
        {
            "job": inventory.async_job_id,
            "generation": inventory.generation,
            "lease": now() + timedelta(seconds=LEASE_SECONDS),
        },
    )


async def begin_page(db, inventory, checkpoint):
    await _transition(
        db,
        "SELECT yoao_assets_v1.begin_usage_inventory_page("
        ":job, :generation, :index, CAST(:checkpoint AS jsonb))",
        {
            "job": inventory.async_job_id,
            "generation": inventory.generation,
            "index": inventory.bucket_index,
            "checkpoint": json.dumps(checkpoint),
        },
    )


async def record_page(
    db, inventory, checkpoint, *, error, terminal, started, measured, categories
):
    outcome = "failed" if error else "completed" if terminal else "continue"
    await _transition(
        db,
        "SELECT yoao_assets_v1.record_usage_inventory_page("
        ":job, :generation, :index, CAST(:checkpoint AS jsonb), :lease,"
        ":outcome, :error, :count, :size, CAST(:categories AS jsonb), :started, :measured)",
        {
            "job": inventory.async_job_id,
            "generation": inventory.generation,
            "index": inventory.bucket_index,
            "checkpoint": json.dumps(checkpoint),
            "lease": now() + timedelta(seconds=LEASE_SECONDS),
            "outcome": outcome,
            "error": error,
            "count": checkpoint.get("count"),
            "size": checkpoint.get("bytes"),
            "categories": json.dumps(categories) if categories is not None else None,
            "started": started,
            "measured": measured,
        },
    )


async def release_chunk(db, inventory, job, *, lease=None):
    await _transition(
        db,
        "SELECT yoao_assets_v1.release_usage_inventory_chunk(:job, :generation, :lease)",
        {"job": job.id, "generation": inventory.generation, "lease": lease},
    )
    jobs.bind_execution_id(job, str(uuid.uuid4()))


async def start(db, actor, api_key_id=None):
    actor = await require_admin(db, actor.id)
    proof = await key_authority(db, actor, api_key_id, "*")
    if await validate_legacy_bindings(db):
        raise HTTPException(409, detail={"code": "configuration_changed"})
    inventory = await locked(db, bootstrap_at=now())
    generation = inventory.generation
    if inventory.status in ACTIVE:
        job = (
            await jobs.get_job(db, inventory.async_job_id, populate_existing=True)
            if inventory.async_job_id
            else None
        )
        if job and job.status in ACTIVE:
            return job, True
        await finish(db, inventory, "failed", "worker_failed")
        generation += 1
    scope = configured_scope()
    authorization = {
        "actor_id": str(actor.id),
        "api_key_id": str(api_key_id) if api_key_id else None,
        "key_proof": proof,
    }
    started = now()
    job = await jobs.create_job(
        db,
        kind="storage_usage",
        user_id=actor.id,
        celery_task_id=str(uuid.uuid4()),
        payload={"scope": "platform_current_objects", "bucket_count": len(scope)},
    )
    await _transition(
        db,
        "SELECT yoao_assets_v1.start_usage_inventory("
        ":job, :generation, CAST(:authority AS jsonb), CAST(:scope AS jsonb), :started, :lease)",
        {
            "job": job.id,
            "generation": generation,
            "authority": json.dumps(authorization),
            "scope": json.dumps(scope),
            "started": started,
            "lease": now() + timedelta(seconds=LEASE_SECONDS),
        },
    )
    return job, False


async def fenced(db, job_id, task_id, generation):
    inventory = await locked(db)
    job = await jobs.get_job(db, job_id, populate_existing=True)
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


def result(inventory, *, success=0, failure=0):
    return {
        "success_count": inventory.success_count + success,
        "failed_count": inventory.failed_count + failure,
        "bucket_count": len(inventory.scope),
        "stage": "enumerating",
    }


async def finish(db, inventory, status, error=None):
    if inventory.async_job_id:
        await accounting.discard_staging(db, inventory.async_job_id)
    await _transition(
        db,
        "SELECT yoao_assets_v1.finish_usage_inventory(:job, :generation, :status, :error)",
        {
            "job": inventory.async_job_id,
            "generation": inventory.generation,
            "status": status,
            "error": error,
        },
    )
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
    job = await jobs.get_job(db, job_id, populate_existing=True)
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
    job = await jobs.get_job(db, job_id, populate_existing=True)
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
    await release_chunk(db, inventory, job, lease=now())


async def configure_schedule(db, actor, enabled, interval_hours, api_key_id=None):
    actor = await require_admin(db, actor.id)
    proof = await key_authority(db, actor, api_key_id, "*")
    if enabled and await validate_legacy_bindings(db):
        raise HTTPException(409, detail={"code": "configuration_changed"})
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.configure_usage_schedule("
                ":enabled, :hours, CAST(:authority AS jsonb), CAST(:scope AS jsonb), :now)"
            ),
            {
                "enabled": enabled,
                "hours": interval_hours,
                "authority": json.dumps(
                    {
                        "actor_id": str(actor.id),
                        "api_key_id": str(api_key_id) if api_key_id else None,
                        "key_proof": proof,
                    }
                ),
                "scope": json.dumps(configured_scope()),
                "now": now(),
            },
        )
    ).one()
    return accounting.schedule_summary(row)


async def start_due(db):
    row = (
        await db.execute(
            text("SELECT * FROM yoao_assets_v1.lock_active_usage_schedule()")
        )
    ).one_or_none()
    if row is None:
        await db.commit()
        return None
    try:
        await authority(db, row)
    except HTTPException as exc:
        await db.execute(
            text("SELECT yoao_assets_v1.pause_usage_schedule(:reason)"),
            {
                "reason": "access_revoked"
                if exc.status_code in {401, 403, 404}
                else "configuration_changed",
            },
        )
        await db.commit()
        return None
    if row.next_due_at is None or row.next_due_at > now():
        await db.commit()
        return None
    actor = await require_admin(db, uuid.UUID(row.authority["actor_id"]))
    key_id = row.authority.get("api_key_id")
    job, reused = await start(db, actor, uuid.UUID(key_id) if key_id else None)
    if reused:
        await db.commit()
        return None
    await db.execute(
        text("SELECT yoao_assets_v1.advance_usage_schedule(:started)"),
        {"started": now()},
    )
    await db.commit()
    return job
