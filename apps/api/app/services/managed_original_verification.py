"""Pin, authorize and preserve observations without granting physical execution."""

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocation
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.db.models.managed_original_verification import (
    ManagedOriginalVerification as Verification,
)
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.user import User
from app.schemas.managed_original_verification import (
    ManagedOriginalVerificationHistory,
    ManagedOriginalVerificationItem,
    ManagedOriginalVerificationStart,
)
from app.services import async_job as jobs, storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import (
    LEGACY_LOCATION_ID,
    MAX_DIRECT_UPLOAD_BYTES,
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

KIND = "managed_original_verification"
ACTIVE = {"pending", "running"}
MAX_BYTES = MAX_DIRECT_UPLOAD_BYTES
TIMEOUT_SECONDS = 60
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_INPUT = text("""
SELECT statement_timestamp() AS measured_at,
    to_jsonb(r) - 'policy_revision' - 'retain_until' AS anchor,
    jsonb_build_object(
      'location', (SELECT to_jsonb(l) FROM storage_locations l WHERE l.id=r.location_id),
      'bindings', (SELECT COALESCE(jsonb_agg(to_jsonb(b) ORDER BY b.purpose),'[]'::jsonb) FROM storage_bindings b)
    ) AS configuration
FROM managed_original_retirements r WHERE r.id=CAST(:retirement_id AS uuid)
""")


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
    location = await db.get(StorageLocation, location_id, populate_existing=True)
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
    return {"client_context": partial(target.client, read_timeout=2)}


async def observe(db, retirement_id):
    row = (
        (
            await db.execute(
                _INPUT,
                {
                    "platform_id": str(LEGACY_LOCATION_ID),
                    "retirement_id": str(retirement_id),
                },
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
    actor = await db.get(User, row.requested_by, populate_existing=True)
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
            row = await db.scalar(
                select(Retirement)
                .where(Retirement.id == retirement_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
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
    current = await db.scalar(
        select(Verification)
        .where(Verification.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = None
    if current is not None and current.async_job_id is not None:
        job = await db.scalar(
            job_lock_statement(
                current.async_job_id, mode="update", populate_existing=True
            )
        )
    return current, job


async def by_job(db, job_id):
    return await db.scalar(
        select(Verification)
        .where(Verification.async_job_id == job_id)
        .execution_options(populate_existing=True)
    )


async def unsettled_ids(db, retirement_id):
    return set(
        await db.scalars(
            select(Verification.operation_id)
            .join(StorageOperation, StorageOperation.id == Verification.operation_id)
            .where(Verification.retirement_id == retirement_id)
        )
    )


async def history(db, retirement_id, actor, *, api_key_id=None, limit=20, offset=0):
    with db.no_autoflush:
        actor, proof = await authority(db, actor, api_key_id)
        context = await observe(db, retirement_id)
        blockers, _ = await eligibility(db, context)
        operations = await unsettled_ids(db, retirement_id)
        active = await db.scalar(
            select(Verification)
            .where(
                Verification.retirement_id == retirement_id,
                Verification.status.in_(ACTIVE),
            )
            .execution_options(populate_existing=True)
        )
        if active is not None:
            blockers.append("verification_active")
            job = (
                await db.get(AsyncJob, active.async_job_id, populate_existing=True)
                if active.async_job_id
                else None
            )
            if not job_matches(active, job):
                blockers.append("verification_job_unavailable")
        if operations:
            blockers.append("physical_operation_unsettled")
        total = await db.scalar(
            select(func.count())
            .select_from(Verification)
            .where(Verification.retirement_id == retirement_id)
        )
        rows = list(
            await db.scalars(
                select(Verification)
                .where(Verification.retirement_id == retirement_id)
                .order_by(Verification.requested_at.desc(), Verification.id.desc())
                .limit(limit)
                .offset(offset)
                .execution_options(populate_existing=True)
            )
        )
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
    active = await db.scalar(
        select(Verification)
        .where(
            Verification.retirement_id == retirement_id, Verification.status.in_(ACTIVE)
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
        row = Verification(
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
            status="pending",
            observation={},
            requested_at=context["measured_at"],
        )
        db.add(row)
        await db.flush()
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
    row.status, row.outcome, row.error_code, row.completed_at = (
        status,
        outcome or status,
        error_code,
        now(),
    )
    if status == "cancelled" and row.cancel_requested_at is None:
        row.cancel_requested_at = row.completed_at
    if result is not None:
        row.observed_sha256 = result.observed_sha256
        row.observed_size_bytes = result.observed_size
        row.observation = dict(result.observation)
    if job is not None:
        job.status, job.completed_at = status, row.completed_at
        job.progress_pct = 100 if status == "completed" else job.progress_pct
        job.error_message = error_code
        job.result = {
            "verification_id": str(row.id),
            "outcome": row.outcome,
            "error_code": error_code,
            "execution_permission": False,
            "cancel_requested": row.cancel_requested_at is not None,
        }
    await db.flush()
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
    row.status, row.started_at = "running", now()
    job.status, job.started_at = "running", row.started_at
    job.result = {"stage": "reading", "execution_permission": False}
    await db.flush()
    return row


async def acquired(db, verification_id, permit):
    row = await db.get(Verification, verification_id, populate_existing=True)
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
    operation = await db.get(StorageOperation, permit.id, populate_existing=True)
    if (
        permit.location_id != uuid.UUID(row.anchor["location_id"])
        or permit.revision != row.maintenance_revision
        or operation is None
        or operation.location_id != permit.location_id
        or operation.revision != permit.revision
        or operation.purpose != KIND
    ):
        reject("verification_operation_mismatch")
    row.operation_id = permit.id
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
    if row.operation_id and await db.get(
        StorageOperation, row.operation_id, populate_existing=True
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
