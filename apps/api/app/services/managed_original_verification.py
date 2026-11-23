"""Pin, authorize and preserve observations without granting physical execution."""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.managed_original_verification import (
    ManagedOriginalVerificationHistory,
    ManagedOriginalVerificationItem,
    ManagedOriginalVerificationStart,
)
from app.services import (
    async_job as jobs,
    storage_access,
    storage_maintenance,
    storage_registry_data,
)
from app.services.audit import AuditService
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    MAX_DIRECT_UPLOAD_BYTES,
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.managed_original_lifecycle_data import (
    active_verification,
    committed_job,
    original_json,
    retirement_anchor,
    verification_facts,
    verification_for_job,
)
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

KIND = "managed_original_verification"
ACTIVE = {"pending", "running"}
MAX_BYTES = MAX_DIRECT_UPLOAD_BYTES
TIMEOUT_SECONDS = 60
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_INPUT = text(
    "SELECT * FROM yoao_assets_v1.observe_original_verification(:retirement_id)"
)


def now():
    return datetime.now(timezone.utc)


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def local_configuration():
    return {
        "endpoint": storage_service.client.meta.endpoint_url,
        "setting_endpoint": settings.minio_endpoint,
        "use_ssl": settings.minio_use_ssl,
        "buckets": {
            purpose: getattr(storage_service, attr)
            for purpose, attr in PURPOSE_BUCKET_ATTRIBUTES.items()
        },
    }


async def _registered_ref(db, anchor):
    """Prove a registered candidate against the assertion's saved physical hash."""
    from app.services import dataset_storage, dataset_storage_target

    location_id = uuid.UUID(anchor["location_id"])
    location = await storage_registry_data.get_location(db, location_id)
    target = await dataset_storage_target.target_for(db, location)
    ref = dataset_storage.make_ref(
        target, anchor["binding_generation"], anchor["object_key"]
    )
    ref["config_revision"] = anchor["location_config_revision"]
    saved = dataset_storage.resolve_ref(ref)
    if (
        saved.location_id != location_id
        or saved.bucket != anchor["bucket"]
        or saved.storage_identity != anchor["storage_identity"]
    ):
        raise ValueError("retirement saved address changed")
    return ref


async def probe_options(db, row):
    """Capture live credentials separately from the durable verification input."""
    if row.anchor["location_id"] == str(LEGACY_LOCATION_ID):
        return {}
    from functools import partial
    from app.services import dataset_storage

    ref = row.configuration.get("saved_ref")
    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as owned:
        if await _registered_ref(owned, row.anchor) != ref:
            reject("verification_context_changed")
        target = await dataset_storage.target_for_ref(owned, ref)
    from app.services import saved_media_io

    if saved_media_io.registered(target):
        return {"registered": True}
    return {"client_context": partial(target.client, read_timeout=2)}


async def observe(db, retirement_id):
    row = (
        (
            await db.execute(
                _INPUT,
                {"retirement_id": retirement_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Retirement assertion not found")
    if row["anchor"]["location_id"] == str(LEGACY_LOCATION_ID):
        configuration = {
            "local": local_configuration(),
            "persistent": row["configuration"],
        }
    else:
        location = row["configuration"]["location"] or {}
        configuration = {
            "saved_ref": None,
            "location": {
                key: location.get(key)
                for key in ("id", "kind", "enabled", "connection_id")
            },
        }
        try:
            configuration["saved_ref"] = await _registered_ref(db, row["anchor"])
        except (ValueError, TypeError, KeyError):
            pass
    return {
        "anchor": row["anchor"],
        "configuration": configuration,
        "fingerprint": digest([row["anchor"], configuration]),
        "measured_at": row["measured_at"],
    }


async def authority(db, actor, api_key_id=None, *, lock=False):
    return await storage_maintenance.authority(db, actor, api_key_id, "*", lock=lock)


async def attempt_authority(db, row, *, lock=False):
    actor = await IdentityAccountLockData(db).read_actor_status(row.requested_by)
    if actor is None:
        reject("verification_authority_changed", 403)
    actor, proof = await authority(
        db, actor, row.authority.get("api_key_id"), lock=lock
    )
    if proof != row.authority.get("key_proof"):
        reject("verification_authority_changed", 403)
    return actor


def context_matches(row, context):
    return row.input_fingerprint == context["fingerprint"]


async def eligibility(db, context):
    anchor = context["anchor"]
    blockers = []
    sha, size = anchor.get("sha256"), anchor.get("size_bytes")
    if (
        anchor.get("asset_verification_state") != "verified"
        or not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha)
        or type(size) is not int
        or size < 0
    ):
        blockers.append("historical_content_unverified")
    if type(size) is int and size > MAX_BYTES:
        blockers.append("verification_size_budget")
    if anchor.get("identity_state") != "verified_binding" or not anchor.get(
        "storage_identity"
    ):
        blockers.append("platform_identity_unproven")
    elif anchor.get("location_id") != str(LEGACY_LOCATION_ID):
        if context["configuration"].get("saved_ref") is None:
            blockers.append("platform_configuration_changed")
    else:
        configured = context["configuration"]["local"]
        expected = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        location = context["configuration"]["persistent"]["location"]
        if (
            configured["endpoint"].rstrip("/") != expected.rstrip("/")
            or anchor.get("bucket") != storage_service.datasets_bucket
            or anchor.get("storage_identity")
            != managed_storage_identity(
                configured["endpoint"], storage_service.datasets_bucket
            )
            or anchor.get("location_config_revision") != 1
            or anchor.get("binding_generation") != 1
            or not location
            or not location.get("enabled")
            or await validate_legacy_bindings(db)
        ):
            blockers.append("platform_configuration_changed")
    current = await storage_access.state(db, uuid.UUID(anchor["location_id"]))
    if current["state"] != "active":
        blockers.append("storage_maintenance")
    return blockers, current["revision"]


async def lock_anchor(db, retirement_id):
    try:
        async with db.begin_nested():
            row = await retirement_anchor(db, retirement_id, lock=True)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("verification_busy")
        raise
    if row is None:
        raise HTTPException(404, "Retirement assertion not found")
    return row


def job_matches(row, job):
    return bool(
        job
        and job.id == row.async_job_id
        and job.kind == KIND
        and job.status == row.status
        and job.celery_task_id == row.task_id
        and job.user_id == row.requested_by
        and job.payload.get("verification_id") == str(row.id)
        and job.payload.get("retirement_id") == str(row.retirement_id)
    )


async def locked(db, row):
    """The shared suffix is Retirement -> Verification -> AsyncJob on every path."""
    await lock_anchor(db, row.retirement_id)
    current = await verification_facts(db, row.id, lock=True)
    job = None
    if current is not None and current.async_job_id is not None:
        job = await db.scalar(
            job_lock_statement(
                current.async_job_id, mode="update", populate_existing=True
            )
        )
    return current, job


async def by_job(db, job_id):
    return await verification_for_job(db, job_id)


async def unsettled_ids(db, retirement_id):
    return set(
        await db.scalars(
            text(
                "SELECT * FROM yoao_assets_v1.original_verification_unsettled(:retirement)"
            ),
            {"retirement": retirement_id},
        )
    )


async def history(db, retirement_id, actor, *, api_key_id=None, limit=20, offset=0):
    with db.no_autoflush:
        actor, proof = await authority(db, actor, api_key_id)
        context = await observe(db, retirement_id)
        blockers, _ = await eligibility(db, context)
        operations = await unsettled_ids(db, retirement_id)
        active = await active_verification(db, retirement_id)
        if active is not None:
            blockers.append("verification_active")
            job = (
                await jobs.get_job(db, active.async_job_id, populate_existing=True)
                if active.async_job_id
                else None
            )
            if not job_matches(active, job):
                blockers.append("verification_job_unavailable")
        if operations:
            blockers.append("physical_operation_unsettled")
        total = await db.scalar(
            text("SELECT yoao_assets_v1.original_verification_count(:retirement)"),
            {"retirement": retirement_id},
        )
        rows = (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.original_verification_history(:retirement, :limit, :offset)"
                ),
                {"retirement": retirement_id, "limit": limit, "offset": offset},
            )
        ).all()
        _, current_proof = await authority(db, actor, api_key_id)
        if proof != current_proof:
            reject("verification_authority_changed", 403)
        if (await observe(db, retirement_id))["fingerprint"] != context["fingerprint"]:
            reject("verification_context_changed")
    return ManagedOriginalVerificationHistory(
        retirement_id=retirement_id,
        measured_at=context["measured_at"],
        can_start=not blockers,
        blockers=blockers,
        max_bytes=MAX_BYTES,
        timeout_seconds=TIMEOUT_SECONDS,
        items=[
            ManagedOriginalVerificationItem(
                **{
                    field: getattr(row, field)
                    for field in [
                        "id",
                        "async_job_id",
                        "status",
                        "outcome",
                        "error_code",
                        "requested_at",
                        "started_at",
                        "completed_at",
                        "cancel_requested_at",
                        "expected_sha256",
                        "expected_size_bytes",
                        "observed_sha256",
                        "observed_size_bytes",
                    ]
                },
                context_matches=context_matches(row, context),
                unsettled_operation=row.operation_id in operations,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


async def start(db, retirement_id, actor, *, api_key_id=None, request=None):
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    await lock_anchor(db, retirement_id)
    context = await observe(db, retirement_id)
    active = await active_verification(db, retirement_id, lock=True)
    if active is not None:
        job = (
            await db.scalar(
                job_lock_statement(
                    active.async_job_id, mode="update", populate_existing=True
                )
            )
            if active.async_job_id
            else None
        )
        if not job_matches(active, job):
            reject("verification_job_unavailable")
        return active, job, True
    if await unsettled_ids(db, retirement_id):
        reject("physical_operation_unsettled")
    blockers, revision = await eligibility(db, context)
    if blockers:
        reject(blockers[0])
    verification_id, task_id = uuid.uuid4(), str(uuid.uuid4())
    async with db.begin_nested():
        job = await jobs.create_job(
            db,
            kind=KIND,
            user_id=actor.id,
            celery_task_id=task_id,
            payload={
                "retirement_id": str(retirement_id),
                "verification_id": str(verification_id),
            },
        )
        facts = dict(
            id=verification_id,
            retirement_id=retirement_id,
            async_job_id=job.id,
            requested_by=actor.id,
            task_id=task_id,
            authority={
                "actor_id": str(actor.id),
                "api_key_id": str(api_key_id) if api_key_id else None,
                "key_proof": proof,
            },
            anchor=context["anchor"],
            configuration=context["configuration"],
            input_fingerprint=context["fingerprint"],
            expected_sha256=context["anchor"]["sha256"],
            expected_size_bytes=context["anchor"]["size_bytes"],
            maintenance_revision=revision,
            requested_at=context["measured_at"],
        )
        row = (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.admit_original_verification(CAST(:facts AS jsonb))"
                ),
                {"facts": original_json(facts)},
            )
        ).one()
        await AuditService.log(
            db,
            actor=actor,
            action="managed_original.verification_start",
            target_type="managed_original_retirement",
            target_id=str(retirement_id),
            request=request,
            status_code=202,
            detail={
                "verification_id": str(row.id),
                "async_job_id": str(job.id),
                "execution_permission": False,
            },
        )
    return row, job, False


def start_response(row, job, existing):
    return ManagedOriginalVerificationStart(
        retirement_id=row.retirement_id,
        verification_id=row.id,
        async_job_id=job.id,
        existing=existing,
    )


async def finish(db, row, job, status, *, outcome=None, error_code=None, result=None):
    """Only a current short locked transaction may publish a terminal observation."""
    if row.status not in ACTIVE:
        return False
    completed_at = now()
    cancel_at = row.cancel_requested_at or (
        completed_at if status == "cancelled" else None
    )
    evidence = (
        None
        if result is None
        else {
            "sha256": result.observed_sha256,
            "size_bytes": result.observed_size,
            "observation": dict(result.observation),
        }
    )
    if not await db.scalar(
        text("""SELECT yoao_assets_v1.finish_original_verification(
            :id, :task, :previous, :status, :outcome, :error, :at, :cancel, CAST(:evidence AS jsonb)
        )"""),
        {
            "id": row.id,
            "task": row.task_id,
            "previous": row.status,
            "status": status,
            "outcome": outcome or status,
            "error": error_code,
            "at": completed_at,
            "cancel": cancel_at,
            "evidence": original_json(evidence),
        },
    ):
        return False
    if job is not None:
        committed_job(
            job,
            status=status,
            completed_at=completed_at,
            error_message=error_code,
            progress_pct=100 if status == "completed" else job.progress_pct,
            result={
                "verification_id": str(row.id),
                "outcome": outcome or status,
                "error_code": error_code,
                "execution_permission": False,
                "cancel_requested": cancel_at is not None,
            },
        )
    return True


async def cancel(db, job_id):
    row = await by_job(db, job_id)
    if row is None:
        raise HTTPException(404, "Verification not found")
    row, job = await locked(db, row)
    if row.status not in ACTIVE:
        if row.status == "cancelled":
            return "cancelled"
        reject("verification_terminal")
    if not job_matches(row, job):
        reject("verification_job_unavailable")
    if row.cancel_requested_at is None:
        row.cancel_requested_at = now()
    if row.status == "pending":
        await finish(db, row, job, "cancelled")
        return "cancelled"
    await db.execute(
        text(
            "SELECT yoao_assets_v1.request_original_verification_cancel(:id, :task, :at)"
        ),
        {"id": row.id, "task": row.task_id, "at": row.cancel_requested_at},
    )
    if job is not None:
        job.result = {
            **(job.result or {}),
            "cancel_requested": True,
            "execution_permission": False,
        }
        await jobs.request_cancel(db, job.id)
    await db.flush()
    return "cancel_requested"


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    row = await by_job(db, job_id)
    if row is None:
        return False
    row, job = await locked(db, row)
    if (
        row.status not in ACTIVE
        or row.task_id != task_id
        or not job
        or job.celery_task_id != task_id
        or job.kind != KIND
    ):
        return False
    if cancelled and row.cancel_requested_at is None:
        row.cancel_requested_at = now()
    return await finish(
        db,
        row,
        job,
        "cancelled" if cancelled and row.status == "pending" else "failed",
        error_code=None
        if cancelled and row.status == "pending"
        else "verification_interrupted",
    )


async def claim(db, job_id, task_id):
    row = await by_job(db, job_id)
    if row is None:
        return None
    failure = None
    try:
        await attempt_authority(db, row, lock=True)
    except HTTPException:
        failure = "verification_authority_changed"
    row, job = await locked(db, row)
    if (
        row.status != "pending"
        or row.task_id != task_id
        or not job
        or job.kind != KIND
        or job.celery_task_id != task_id
        or job.status != "pending"
    ):
        return None
    if row.cancel_requested_at is not None:
        await finish(db, row, job, "cancelled")
        return None
    context = await observe(db, row.retirement_id)
    if failure is None and (
        not context_matches(row, context) or not job_matches(row, job)
    ):
        failure = "verification_context_changed"
    if failure is None:
        blockers, revision = await eligibility(db, context)
        if blockers or revision != row.maintenance_revision:
            failure = blockers[0] if blockers else "storage_maintenance_changed"
    if failure:
        await finish(db, row, job, "failed", error_code=failure)
        return None
    started_at = now()
    if not await db.scalar(
        text("SELECT yoao_assets_v1.claim_original_verification(:id, :task, :at)"),
        {"id": row.id, "task": task_id, "at": started_at},
    ):
        return None
    row.status, row.started_at = "running", started_at
    committed_job(
        job,
        status="running",
        started_at=started_at,
        result={"stage": "reading", "execution_permission": False},
    )
    return row


async def acquired(db, verification_id, permit):
    row = await verification_facts(db, verification_id)
    if row is None:
        reject("verification_context_changed")
    await attempt_authority(db, row, lock=True)
    row, job = await locked(db, row)
    if (
        row.status != "running"
        or row.operation_id is not None
        or not job_matches(row, job)
        or job.status != "running"
    ):
        reject("verification_context_changed")
    if row.cancel_requested_at is not None:
        reject("verification_cancelled")
    if not context_matches(row, await observe(db, row.retirement_id)):
        reject("verification_context_changed")
    operation = await storage_access.operation_receipt(db, permit.id)
    if (
        permit.location_id != uuid.UUID(row.anchor["location_id"])
        or permit.revision != row.maintenance_revision
        or operation is None
        or operation["location_id"] != permit.location_id
        or operation["revision"] != permit.revision
        or operation["purpose"] != KIND
    ):
        reject("verification_operation_mismatch")
    from app.services.managed_original_reclaim_fence import shared

    await shared(db, row.anchor["bucket"], row.anchor["object_key"])
    if not await db.scalar(
        text(
            "SELECT yoao_assets_v1.bind_original_verification_permit(:id, :task, :operation)"
        ),
        {"id": row.id, "task": row.task_id, "operation": permit.id},
    ):
        reject("verification_operation_mismatch")
    await db.commit()


async def publish(
    db, job_id, task_id, *, result=None, error_code=None, cancelled=False
):
    row = await by_job(db, job_id)
    if row is None:
        return False
    authority_failure = False
    try:
        await attempt_authority(db, row, lock=True)
    except HTTPException:
        authority_failure = True
    row, job = await locked(db, row)
    if (
        row.status != "running"
        or row.task_id != task_id
        or not job
        or job.celery_task_id != task_id
        or job.kind != KIND
        or job.status != "running"
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
    if authority_failure:
        return await finish(
            db, row, job, "failed", error_code="verification_authority_changed"
        )
    context = await observe(db, row.retirement_id)
    if not context_matches(row, context) or not job_matches(row, job):
        return await finish(
            db, row, job, "failed", error_code="verification_context_changed"
        )
    try:
        await storage_access.check(
            db,
            uuid.UUID(row.anchor["location_id"]),
            expected_revision=row.maintenance_revision,
            lock=True,
        )
    except ValueError:
        return await finish(
            db, row, job, "failed", error_code="storage_maintenance_changed"
        )
    if error_code is not None:
        safe = (
            error_code
            if _SAFE_CODE.fullmatch(error_code)
            else "verification_read_failed"
        )
        return await finish(db, row, job, "failed", error_code=safe)
    if row.operation_id is None:
        return await finish(
            db, row, job, "failed", error_code="verification_operation_missing"
        )
    if result is None or result.outcome not in {
        "matched",
        "missing",
        "changed",
        "unverifiable",
        "failed",
    }:
        return await finish(
            db, row, job, "failed", error_code="verification_invalid_result"
        )
    if (
        (
            result.observed_sha256 is not None
            and (
                not isinstance(result.observed_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", result.observed_sha256)
            )
        )
        or (
            result.observed_size is not None
            and (type(result.observed_size) is not int or result.observed_size < 0)
        )
        or not isinstance(result.observation, dict)
    ):
        return await finish(
            db, row, job, "failed", error_code="verification_invalid_result"
        )
    if result.outcome == "matched" and (
        result.error_code is not None
        or result.observed_sha256 != row.expected_sha256
        or result.observed_size != row.expected_size_bytes
    ):
        return await finish(
            db, row, job, "failed", error_code="verification_invalid_result"
        )
    safe = (
        result.error_code
        if isinstance(result.error_code, str)
        and _SAFE_CODE.fullmatch(result.error_code)
        else None
    )
    return await finish(
        db,
        row,
        job,
        "failed" if result.outcome == "failed" else "completed",
        outcome=result.outcome,
        error_code=safe
        or ("verification_read_failed" if result.outcome == "failed" else None),
        result=result,
    )
