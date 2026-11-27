"""One reviewed immutable version, a durable publication fence and drained deletion."""

from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import json
import os
import re
import socket
import uuid

from fastapi import HTTPException
from jose import jwt, JWTError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.schemas.managed_original_reclaim import (
    ManagedOriginalReclaimHistory,
    ManagedOriginalReclaimItem,
    ManagedOriginalReclaimPreview,
    ManagedOriginalReclaimStart,
    ReferenceReconciliationOut,
)
from app.services import (
    jobs_lifecycle_data as jobs,
    storage_access,
    storage_maintenance,
)
from app.services.audit import AuditService
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.jobs_lifecycle_data import job_lock_statement
from app.services.managed_original_lifecycle_data import (
    advance_reconciliation,
    committed_job,
    original_json,
    reclaim_attempt,
    reclaim_attempt_for_job,
    reclaim_claim,
    reclaim_reconciliation,
)

KIND = "managed_original_reclaim"
REFERENCE_KIND = "managed_reference_reconciliation"
ACTIVE = {"pending", "running"}
MAX_BYTES = 5 * 1024**3
TIMEOUT_SECONDS = 60
AUDIENCE = "managed-original-reclaim"
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def now():
    return datetime.now(timezone.utc)


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def authority(db, actor, api_key_id=None, *, lock=False):
    return await storage_maintenance.authority(db, actor, api_key_id, "*", lock=lock)


async def attempt_authority(db, row, *, lock=False):
    actor = await IdentityAccountLockData(db).read_actor_status(row.requested_by)
    if actor is None:
        reject("reclaim_authority_changed", 403)
    actor, proof = await authority(
        db, actor, row.authority.get("api_key_id"), lock=lock
    )
    if proof != row.authority.get("key_proof"):
        reject("reclaim_authority_changed", 403)
    return actor


async def _observe(db, retirement_id, *, reclaim_attempt_id=None):
    from app.services import managed_original_reclamation as observations

    return await observations.observe(
        db, retirement_id, reclaim_attempt_id=reclaim_attempt_id
    )


async def _exclusive(db, bucket, key):
    from app.services.managed_original_reclaim_fence import exclusive

    await exclusive(db, bucket, key)


async def _target(db, context):
    """Resolve only the Creation's saved physical address, never today's binding."""
    from app.services import (
        managed_original_creation as creation_service,
        dataset_storage,
    )
    from app.services.content_snapshot import ContentError

    creation = context.get("creation") or {}
    anchor = context["anchor"]
    blockers = list(context.get("blockers", []))
    version, size, sha = (
        creation.get("version_id"),
        creation.get("size_bytes"),
        creation.get("sha256"),
    )
    if not creation:
        return None, blockers + ["reclaim_creation_unproven"]
    if (
        creation.get("owner_kind") not in {"dataset", "dataset_auxiliary"}
        or creation.get("status") != "verified"
        or not creation.get("settled_at")
        or not creation.get("write_started_at")
        or not creation.get("receipt")
        or not creation.get("verification")
        or not creation.get("readback_at")
    ):
        blockers.append("reclaim_creation_unproven")
    if (
        not isinstance(version, str)
        or not version
        or version == "null"
        or len(version) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in version)
    ):
        blockers.append("reclaim_immutable_version_required")
    receipt = (
        creation.get("receipt") if isinstance(creation.get("receipt"), dict) else {}
    )
    verification = (
        creation.get("verification")
        if isinstance(creation.get("verification"), dict)
        else {}
    )
    response = (
        receipt.get("response") if isinstance(receipt.get("response"), dict) else {}
    )
    metadata = (
        response.get("ResponseMetadata")
        if isinstance(response.get("ResponseMetadata"), dict)
        else {}
    )
    if (
        receipt.get("version_id") != version
        or response.get("VersionId") != version
        or metadata.get("HTTPStatusCode") != 200
        or verification.get("sha256") != sha
        or verification.get("size_bytes") != size
    ):
        blockers.append("reclaim_creation_unproven")
    if type(size) is not int or size < 0 or size > MAX_BYTES:
        blockers.append("reclaim_size_budget")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        blockers.append("reclaim_content_unverified")
    if any(
        creation.get(field) != anchor.get(field)
        for field in (
            "bucket",
            "object_key",
            "storage_identity",
            "sha256",
            "size_bytes",
        )
    ):
        blockers.append("reclaim_creation_changed")
    if str(creation.get("location_id")) != str(anchor.get("location_id")):
        blockers.append("reclaim_creation_changed")
    if creation.get("operation_id") and await storage_access.operation_receipt(
        db, uuid.UUID(str(creation["operation_id"]))
    ):
        blockers.append("physical_operation_unsettled")
    try:
        saved = creation_service.original_locator(creation)
        target = await dataset_storage.target_for_ref(db, saved.ref)
        if (saved.bucket, saved.object_key, saved.storage_identity) != (
            anchor.get("bucket"),
            anchor.get("object_key"),
            anchor.get("storage_identity"),
        ):
            raise ValueError()
        if (target.endpoint_url, target.bucket, target.key_prefix) != (
            saved.endpoint_url,
            saved.bucket,
            saved.key_prefix,
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError, ContentError, HTTPException):
        return None, list(dict.fromkeys(blockers + ["reclaim_configuration_changed"]))
    fixture_scope = await db.scalar(
        text(
            "SELECT yoao_assets_v1.classify_fixture_original_reclaim("
            "CAST(:creation AS uuid), CAST(:identity AS text))"
        ),
        {"creation": creation["id"], "identity": saved.storage_identity},
    )
    if fixture_scope not in {"ordinary", "qualified", "blocked"}:
        raise RuntimeError("invalid fixture original reclaim classification")
    if creation.get("owner_kind") == "dataset_auxiliary":
        if (
            fixture_scope != "qualified"
            or anchor.get("owner_type") != "dataset_auxiliary"
            or str(anchor.get("auxiliary_original_id")) != str(creation["id"])
        ):
            blockers.append("reclaim_creation_unproven")
    elif anchor.get("owner_type") != "dataset_asset":
        blockers.append("reclaim_creation_unproven")
    if fixture_scope == "blocked":
        blockers.append("fixture_original_store_unqualified")
    elif fixture_scope == "qualified" and not await db.scalar(
        text(
            "SELECT yoao_assets_v1.fixture_original_target_matches(:creation_id, false)"
        ),
        {"creation_id": uuid.UUID(creation["id"])},
    ):
        blockers.append("fixture_original_storage_changed")
    return {
        "location_id": str(saved.location_id),
        "creation_id": str(creation["id"]),
        "bucket": saved.bucket,
        "object_key": saved.object_key,
        "storage_identity": saved.storage_identity,
        "endpoint_url": saved.endpoint_url,
        "saved_ref": saved.ref,
        "version_id": version,
        "sha256": sha,
        "size_bytes": size,
        "configuration_fingerprint": digest(context["configuration"]),
    }, list(dict.fromkeys(blockers))


async def _gate(db, location_id, *, lock=False):
    try:
        async with db.begin_nested():
            return await storage_access.gate_facts(
                db, uuid.UUID(str(location_id)), lock="share_nowait" if lock else None
            )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("reclaim_busy")
        raise


async def context(
    db, retirement_id, *, lock=False, retry_of_id=None, current_attempt=None
):
    raw = await _observe(
        db,
        retirement_id,
        reclaim_attempt_id=current_attempt.id if current_attempt else None,
    )
    target, blockers = await _target(db, raw)
    gate = None
    if target:
        gate = await _gate(db, target["location_id"], lock=lock)
        if gate is None or gate["state"] != "stopped":
            blockers.append("reclaim_location_not_stopped")
        if gate:
            target["maintenance_revision"] = gate["revision"]
        claim = await reclaim_claim(db, target["bucket"], target["object_key"])
        if claim and (
            claim.creation_id != uuid.UUID(target["creation_id"])
            or claim.version_id != target["version_id"]
            or claim.storage_identity != target["storage_identity"]
        ):
            blockers.append("reclaim_claim_changed")
        allowed_attempt = current_attempt.id if current_attempt else retry_of_id
        if claim and claim.state != "released":
            if claim.state == "deleted":
                blockers.append("reclaim_already_deleted")
            elif allowed_attempt != claim.current_attempt_id:
                blockers.append("reclaim_claimed")
        if retry_of_id is not None:
            previous = await reclaim_attempt(db, retry_of_id)
            if (
                previous is None
                or previous.retirement_id != retirement_id
                or previous.status not in {"failed", "cancelled", "unknown"}
                or not previous.physical_settled
                or (claim and claim.current_attempt_id != previous.id)
                or previous.creation_id != uuid.UUID(target["creation_id"])
                or previous.version_id != target["version_id"]
                or any(
                    previous.target.get(key) != target.get(key)
                    for key in (
                        "bucket",
                        "object_key",
                        "storage_identity",
                        "location_id",
                    )
                )
            ):
                blockers.append("reclaim_retry_unavailable")
            elif previous.operation_id and await storage_access.operation_receipt(
                db, previous.operation_id
            ):
                blockers.append("physical_operation_unsettled")
    else:
        claim = None
    immutable_fingerprint = digest([raw["fingerprint"], target])
    review_fingerprint = digest(
        [
            immutable_fingerprint,
            None
            if claim is None
            else {
                "id": str(claim.id),
                "state": claim.state,
                "revision": claim.revision,
                "current_attempt_id": str(claim.current_attempt_id),
            },
        ]
    )
    return {
        **raw,
        "target": target,
        "blockers": list(dict.fromkeys(blockers)),
        "fingerprint": immutable_fingerprint,
        "review_fingerprint": review_fingerprint,
        "claim": claim,
    }


def binding(actor, proof, retirement_id, payload):
    return digest(
        {
            "actor": str(actor.id),
            "proof": proof,
            "retirement": str(retirement_id),
            "candidate": payload.model_dump(mode="json", exclude={"impact_token"}),
        }
    )


def decode_token(token):
    try:
        data = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if data["exp"] <= now().timestamp() or not all(
            key in data for key in ("binding", "fingerprint")
        ):
            raise ValueError()
        return data
    except (JWTError, ValueError, KeyError, TypeError):
        reject("reclaim_preview_invalid")


async def preview(db, retirement_id, payload, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id)
    value = await context(db, retirement_id, retry_of_id=payload.retry_of_id)
    _, current_proof = await authority(db, actor, api_key_id)
    if current_proof != proof:
        reject("reclaim_authority_changed", 403)
    expires = value["measured_at"] + timedelta(minutes=5)
    target = value["target"] or {}
    token = (
        None
        if value["blockers"]
        else jwt.encode(
            {
                "aud": AUDIENCE,
                "exp": expires,
                "binding": binding(actor, proof, retirement_id, payload),
                "fingerprint": value["review_fingerprint"],
            },
            settings.secret_key,
            algorithm="HS256",
        )
    )
    return ManagedOriginalReclaimPreview(
        retirement_id=retirement_id,
        measured_at=value["measured_at"],
        expires_at=expires,
        eligible=not value["blockers"],
        blockers=value["blockers"],
        impact_token=token,
        expected_size_bytes=target.get("size_bytes"),
        version_id=target.get("version_id"),
        creation_id=target.get("creation_id"),
        location_id=target.get("location_id"),
    )


async def history(db, retirement_id, actor, *, api_key_id=None, limit=20, offset=0):
    await authority(db, actor, api_key_id)
    value = await context(db, retirement_id)
    total = await db.scalar(
        text("SELECT yoao_assets_v1.reclaim_history_count(:id)"),
        {"id": retirement_id},
    )
    rows = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.reclaim_history_page(:id, :limit, :offset)"
            ),
            {"id": retirement_id, "limit": limit, "offset": offset},
        )
    ).all()
    result = []
    for row in rows:
        unsettled = not row.physical_settled or bool(
            row.operation_id
            and await storage_access.operation_receipt(db, row.operation_id)
        )
        fields = {
            field: getattr(row, field)
            for field in (
                "id",
                "async_job_id",
                "retry_of_id",
                "status",
                "phase",
                "outcome",
                "error_code",
                "requested_at",
                "started_at",
                "completed_at",
                "cancel_requested_at",
                "expected_size_bytes",
                "version_id",
                "creation_id",
            )
        }
        result.append(
            ManagedOriginalReclaimItem(
                **fields,
                location_id=row.target["location_id"],
                delete_started=row.delete_intent_at is not None,
                absence_proven=row.absence_verified_at is not None,
                unsettled_operation=unsettled,
            )
        )
    await authority(db, actor, api_key_id)
    return ManagedOriginalReclaimHistory(
        retirement_id=retirement_id,
        measured_at=value["measured_at"],
        can_preview=not value["blockers"],
        blockers=value["blockers"],
        max_bytes=MAX_BYTES,
        timeout_seconds=TIMEOUT_SECONDS,
        items=result,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


def job_matches(row, job):
    return bool(
        job
        and job.id == row.async_job_id
        and job.kind == KIND
        and job.status == ("failed" if row.status == "unknown" else row.status)
        and job.celery_task_id == row.task_id
        and job.user_id == row.requested_by
        and job.payload.get("reclaim_id") == str(row.id)
        and job.payload.get("retirement_id") == str(row.retirement_id)
    )


async def _start(db, retirement_id, payload, actor, *, api_key_id=None, request=None):
    token = decode_token(payload.impact_token)
    actor, proof = await authority(db, actor, api_key_id, lock=True)
    first = await _observe(db, retirement_id)
    anchor = first["anchor"]
    await _exclusive(db, anchor["bucket"], anchor["object_key"])
    value = await context(db, retirement_id, lock=True, retry_of_id=payload.retry_of_id)
    if (
        token["binding"] != binding(actor, proof, retirement_id, payload)
        or token["fingerprint"] != value["review_fingerprint"]
    ):
        reject("reclaim_preview_changed")
    if value["blockers"]:
        reject(value["blockers"][0])
    target = value["target"]
    existing = value["claim"]
    active = await db.scalar(
        text("SELECT yoao_assets_v1.reclaim_creation_active(:id)"),
        {"id": uuid.UUID(target["creation_id"])},
    )
    if active:
        reject("reclaim_claimed")
    identity, task_id = uuid.uuid4(), str(uuid.uuid4())
    job = await jobs.create_job(
        db,
        kind=KIND,
        user_id=actor.id,
        celery_task_id=task_id,
        payload={"retirement_id": str(retirement_id), "reclaim_id": str(identity)},
    )
    facts = dict(
        id=identity,
        retirement_id=retirement_id,
        creation_id=uuid.UUID(target["creation_id"]),
        async_job_id=job.id,
        retry_of_id=payload.retry_of_id,
        requested_by=actor.id,
        task_id=task_id,
        authority={
            "api_key_id": str(api_key_id) if api_key_id else None,
            "key_proof": proof,
        },
        target=target,
        input_fingerprint=value["fingerprint"],
        version_id=target["version_id"],
        expected_sha256=target["sha256"],
        expected_size_bytes=target["size_bytes"],
        maintenance_revision=target["maintenance_revision"],
        reason=payload.reason,
    )
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.admit_original_reclaim(CAST(:facts AS jsonb), :claim, :revision)"
            ),
            {
                "facts": original_json(facts),
                "claim": existing.id if existing else None,
                "revision": existing.revision if existing else None,
            },
        )
    ).one()
    await AuditService.log(
        db,
        actor=actor,
        action="managed_original.reclaim_start",
        target_type="managed_original_retirement",
        target_id=str(retirement_id),
        request=request,
        status_code=202,
        detail={
            "reclaim_id": str(identity),
            "async_job_id": str(job.id),
            "version_id": row.version_id,
            "reason": payload.reason,
        },
    )
    return row, job, False


async def start(db, retirement_id, payload, actor, *, api_key_id=None, request=None):
    async with db.begin_nested():
        return await _start(
            db, retirement_id, payload, actor, api_key_id=api_key_id, request=request
        )


def start_response(row, job, existing=False):
    return ManagedOriginalReclaimStart(
        retirement_id=row.retirement_id,
        reclaim_id=row.id,
        async_job_id=job.id,
        existing=existing,
    )


async def by_job(db, job_id):
    return await reclaim_attempt_for_job(db, job_id)


async def locked(db, row):
    await _exclusive(db, row.target["bucket"], row.target["object_key"])
    row = await reclaim_attempt(db, row.id, lock=True)
    claim = await reclaim_claim(
        db, row.target["bucket"], row.target["object_key"], lock=True
    )
    job = (
        await db.scalar(
            job_lock_statement(row.async_job_id, mode="update", populate_existing=True)
        )
        if row.async_job_id
        else None
    )
    if claim is None or claim.current_attempt_id != row.id:
        reject("reclaim_claim_changed")
    return row, claim, job


async def _finish(db, row, _claim, job, *, status, outcome, error_code=None):
    completed_at = now()
    if not await db.scalar(
        text("""SELECT yoao_assets_v1.finish_original_reclaim_attempt(
            :id, :task, :previous, :status, :outcome, :error, :at,
            :cancel, :settled, CAST(:ack AS jsonb), CAST(:observation AS jsonb))"""),
        {
            "id": row.id,
            "task": row.task_id,
            "previous": row.status,
            "status": status,
            "outcome": outcome,
            "error": error_code,
            "at": completed_at,
            "cancel": row.cancel_requested_at,
            "settled": row.physical_settled,
            "ack": original_json(row.delete_ack),
            "observation": original_json(row.observation),
        },
    ):
        return False
    if job and job.kind == KIND and job.celery_task_id == row.task_id:
        committed_job(
            job,
            status="failed" if status == "unknown" else status,
            completed_at=completed_at,
            error_message=error_code,
            progress_pct=100 if status == "completed" else job.progress_pct,
            result={
                "reclaim_id": str(row.id),
                "outcome": outcome,
                "error_code": error_code,
                "delete_started": row.delete_intent_at is not None,
                "absence_proven": row.absence_verified_at is not None,
                "physical_settled": row.physical_settled,
            },
        )
    return True


async def cancel(db, job_id):
    row = await by_job(db, job_id)
    if row is None:
        reject("reclaim_not_found", 404)
    row, claim, job = await locked(db, row)
    if row.status not in ACTIVE:
        reject("reclaim_terminal")
    if not job_matches(row, job):
        reject("reclaim_job_unavailable")
    row.cancel_requested_at = row.cancel_requested_at or now()
    if row.status == "pending" or (
        row.operation_id is None
        and row.physical_settled
        and row.delete_intent_at is None
    ):
        await _finish(db, row, claim, job, status="cancelled", outcome="cancelled")
        return "cancelled"
    await jobs.request_cancel(db, job.id)
    job.result = {**job.result, "cancel_requested": True}
    await _persist_cancel(db, row)
    await db.flush()
    return "cancel_requested"


async def _persist_cancel(db, row):
    await db.execute(
        text("SELECT yoao_assets_v1.request_original_reclaim_cancel(:id, :task, :at)"),
        {"id": row.id, "task": row.task_id, "at": row.cancel_requested_at},
    )


async def redispatch(db, attempt_id, actor, *, api_key_id=None):
    await authority(db, actor, api_key_id, lock=True)
    row = await reclaim_attempt(db, attempt_id)
    if row is None:
        reject("reclaim_not_found", 404)
    row, _claim, job = await locked(db, row)
    if row.status != "pending" or not job_matches(row, job):
        reject("reclaim_redispatch_unavailable")
    return row, job


async def claim(db, job_id, task_id):
    row = await by_job(db, job_id)
    if row is None:
        return None
    failure = None
    try:
        await attempt_authority(db, row, lock=True)
    except HTTPException:
        failure = "reclaim_authority_changed"
    row, physical, job = await locked(db, row)
    if row.status != "pending" or row.task_id != task_id or not job_matches(row, job):
        return None
    if row.cancel_requested_at:
        await _finish(db, row, physical, job, status="cancelled", outcome="cancelled")
        return None
    value = await context(db, row.retirement_id, lock=True, current_attempt=row)
    if value["blockers"] or value["fingerprint"] != row.input_fingerprint:
        failure = failure or (
            value["blockers"][0] if value["blockers"] else "reclaim_context_changed"
        )
    if failure:
        await _finish(
            db,
            row,
            physical,
            job,
            status="failed",
            outcome="failed",
            error_code=failure,
        )
        return None
    started_at = now()
    if not await db.scalar(
        text("SELECT yoao_assets_v1.claim_original_reclaim_attempt(:id, :task, :at)"),
        {"id": row.id, "task": task_id, "at": started_at},
    ):
        return None
    row.status, row.started_at, row.phase = "running", started_at, "reading"
    committed_job(job, status="running", started_at=started_at)
    return row


async def acquire(db, attempt_id, task_id):
    """A dedicated stopped-Gate permit, atomically bound to the real running owner."""
    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False
    ) as owned:
        row = await reclaim_attempt(owned, attempt_id)
        if row is None:
            reject("reclaim_not_found", 404)
        await attempt_authority(owned, row, lock=True)
        row, physical, job = await locked(owned, row)
        if (
            row.status != "running"
            or row.task_id != task_id
            or not job_matches(row, job)
            or row.cancel_requested_at
            or row.operation_id is not None
        ):
            reject("reclaim_operation_invalid")
        value = await context(owned, row.retirement_id, lock=True, current_attempt=row)
        if value["blockers"] or value["fingerprint"] != row.input_fingerprint:
            reject("reclaim_context_changed")
        operation = (
            await owned.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.acquire_original_reclaim_operation(:id, :task, :operation, :node)"
                ),
                {
                    "id": attempt_id,
                    "task": task_id,
                    "operation": uuid.uuid4(),
                    "node": f"{socket.gethostname()}:{os.getpid()}",
                },
            )
        ).one_or_none()
        if operation is None:
            reject("reclaim_operation_invalid")
        await owned.commit()
        return storage_access.Permit(
            storage_access.engine(db),
            operation.id,
            operation.location_id,
            operation.revision,
        )


async def io_options(db, row):
    from app.services import dataset_storage

    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False
    ) as owned:
        target = await dataset_storage.target_for_ref(owned, row.target["saved_ref"])
    from app.services import saved_media_io

    if saved_media_io.registered(target):
        return {"registered": True}
    return {
        "client_context": partial(target.client, single_attempt=True, read_timeout=2)
    }


async def recover_may_delete(db, row):
    return await db.scalar(
        text(
            "SELECT yoao_assets_v1.reclaim_version_may_be_deleted(:creation, :version)"
        ),
        {"creation": row.creation_id, "version": row.version_id},
    )


async def checkpoint(db, attempt_id, task_id, phase, payload):
    """Independent facts survive authority loss and business/job rollback."""
    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False
    ) as owned:
        row = await reclaim_attempt(owned, attempt_id)
        if row is None or row.task_id != task_id or row.status != "running":
            reject("reclaim_operation_invalid")
        if phase == "delete_started":
            await attempt_authority(owned, row, lock=True)
        row, _physical, job = await locked(owned, row)
        if payload.get("version_id") != row.version_id:
            reject("reclaim_version_changed")
        if phase in {"read_verified", "delete_started"} and (
            payload.get("sha256") != row.expected_sha256
            or payload.get("size_bytes") != row.expected_size_bytes
        ):
            reject("reclaim_content_changed")
        if phase == "delete_started":
            if row.cancel_requested_at or not job_matches(row, job):
                reject("reclaim_cancelled")
            value = await context(
                owned, row.retirement_id, lock=True, current_attempt=row
            )
            if value["blockers"] or value["fingerprint"] != row.input_fingerprint:
                reject("reclaim_context_changed")
        fields = (
            ("version_id", "request_id", "http_status", "delete_marker")
            if phase == "delete_ack"
            else ("sha256", "size_bytes", "etag", "version_id")
        )
        error = await owned.scalar(
            text(
                "SELECT yoao_assets_v1.checkpoint_original_reclaim(:id, :task, :phase, CAST(:facts AS jsonb), :at)"
            ),
            {
                "id": row.id,
                "task": task_id,
                "phase": phase,
                "at": now(),
                "facts": original_json(
                    {key: payload[key] for key in fields if key in payload}
                ),
            },
        )
        if error is not None:
            reject(error)
        await owned.commit()


async def publish(
    db, job_id, task_id, *, result=None, error_code=None, cancelled=False, settled=False
):
    row = await by_job(db, job_id)
    if row is None:
        return False
    row, physical, job = await locked(db, row)
    if row.status != "running" or row.task_id != task_id:
        return False
    remaining = row.operation_id and await storage_access.operation_receipt(
        db, row.operation_id
    )
    row.physical_settled = bool(settled and not remaining)
    if result is not None:
        if result.delete_ack and row.delete_ack is None and row.delete_intent_at:
            row.delete_ack = dict(result.delete_ack)
        row.observation = dict(result.observation)
    possible = row.delete_intent_at is not None or (
        result is not None and result.delete_started
    )
    proven = row.absence_verified_at is not None and row.physical_settled
    outcome = (result.outcome if result else None) or (
        "deleted" if proven else "unknown" if possible else "failed"
    )
    if not row.physical_settled:
        status, outcome, error_code = (
            "unknown",
            "unknown",
            "storage_operation_unsettled",
        )
    elif proven:
        status, outcome, error_code = (
            "completed",
            "already_absent" if outcome == "already_absent" else "deleted",
            None,
        )
    elif possible:
        status, outcome, error_code = (
            "unknown",
            "unknown",
            error_code
            or (result.error_code if result else None)
            or "reclaim_delete_unknown",
        )
    elif cancelled or row.cancel_requested_at:
        status, outcome, error_code = "cancelled", "cancelled", None
    else:
        status, outcome, error_code = (
            "failed",
            outcome,
            error_code or (result.error_code if result else None) or "reclaim_failed",
        )
    return await _finish(
        db, row, physical, job, status=status, outcome=outcome, error_code=error_code
    )


async def signal_interrupted(db, job_id, task_id, *, cancelled):
    row = await by_job(db, job_id)
    if row is None:
        state = await reclaim_reconciliation(db, lock=True)
        if (
            state is None
            or state.async_job_id != job_id
            or state.task_id != task_id
            or state.status not in ACTIVE
        ):
            return False
        job = await db.scalar(
            job_lock_statement(job_id, mode="update", populate_existing=True)
        )
        error_code = "reference_reconciliation_interrupted"
        completed_at = now()
        if not await advance_reconciliation(
            db, state, "failed", at=completed_at, error=error_code
        ):
            return False
        if job and job.kind == REFERENCE_KIND and job.celery_task_id == task_id:
            committed_job(
                job,
                status="failed",
                completed_at=completed_at,
                error_message=error_code,
            )
        return True
    if row.task_id != task_id or row.status not in ACTIVE:
        return False
    row, physical, job = await locked(db, row)
    if row.status == "pending" or (
        row.operation_id is None
        and row.physical_settled
        and row.delete_intent_at is None
    ):
        if cancelled:
            row.cancel_requested_at = row.cancel_requested_at or now()
        return await _finish(
            db,
            row,
            physical,
            job,
            status="cancelled" if cancelled else "failed",
            outcome="cancelled" if cancelled else "failed",
            error_code=None if cancelled else "reclaim_interrupted",
        )
    row.cancel_requested_at = row.cancel_requested_at or (now() if cancelled else None)
    if cancelled:
        await _persist_cancel(db, row)
    return False


async def reconciliation_out(db):
    row = await reclaim_reconciliation(db)
    return (
        ReferenceReconciliationOut(
            **{
                key: getattr(row, key)
                for key in ReferenceReconciliationOut.model_fields
            }
        )
        if row
        else ReferenceReconciliationOut()
    )


async def start_reconciliation(db, actor=None, *, api_key_id=None):
    if actor is not None:
        actor, proof = await authority(db, actor, api_key_id, lock=True)
    else:
        proof = None
    row = await reclaim_reconciliation(db, lock=True, initialize=True)
    if row.status in ACTIVE:
        job = await jobs.get_job(db, row.async_job_id) if row.async_job_id else None
        if (
            job is not None
            and job.celery_task_id == row.task_id
            and job.kind == REFERENCE_KIND
            and job.status in ACTIVE
        ):
            return job
    task_id = str(uuid.uuid4())
    job = await jobs.create_job(
        db,
        kind=REFERENCE_KIND,
        user_id=actor.id if actor else None,
        celery_task_id=task_id,
        payload={
            "scope": "registered_retirement_references",
            "authority": {
                "api_key_id": str(api_key_id) if api_key_id else None,
                "key_proof": proof,
            }
            if actor
            else None,
        },
    )
    if not await db.scalar(
        text(
            "SELECT yoao_assets_v1.queue_original_reconciliation(:job, :task, :expected, :previous)"
        ),
        {
            "job": job.id,
            "task": task_id,
            "expected": row.async_job_id,
            "previous": row.status,
        },
    ):
        reject("reference_reconciliation_changed")
    return job


async def reconcile(db, job_id, task_id):
    initial = await jobs.get_job(db, job_id, populate_existing=True)
    authority_failure = False
    initial_payload = dict(initial.payload) if initial else {}
    if initial and initial.user_id is not None:
        try:
            actor = await IdentityAccountLockData(db).read_actor_status(initial.user_id)
            proof = initial_payload.get("authority") or {}
            if actor is None:
                reject("reclaim_authority_changed", 403)
            _, current = await authority(db, actor, proof.get("api_key_id"), lock=True)
            if current != proof.get("key_proof"):
                reject("reclaim_authority_changed", 403)
        except HTTPException:
            authority_failure = True
    row = await reclaim_reconciliation(db, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if (
        not row
        or row.status != "pending"
        or row.async_job_id != job_id
        or row.task_id != task_id
        or not job
        or job.kind != REFERENCE_KIND
        or job.celery_task_id != task_id
        or job.status != "pending"
    ):
        return False
    if authority_failure or job.payload != initial_payload:
        error_code = "reclaim_authority_changed"
        completed_at = now()
        if not await advance_reconciliation(
            db, row, "failed", at=completed_at, error=error_code
        ):
            return False
        committed_job(
            job, status="failed", completed_at=completed_at, error_message=error_code
        )
        await db.commit()
        return True
    started_at = now()
    if not await advance_reconciliation(db, row, "running", at=started_at):
        return False
    committed_job(job, status="running", started_at=started_at)
    await db.commit()
    from app.services.managed_original_reclamation import reconciliation_observe

    try:
        value = await reconciliation_observe(db)
        await db.rollback()
    except Exception:
        await db.rollback()
        value = None
    row = await reclaim_reconciliation(db, lock=True)
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if (
        row.async_job_id != job_id
        or row.task_id != task_id
        or row.status != "running"
        or job is None
        or job.status != "running"
        or job.kind != REFERENCE_KIND
        or job.celery_task_id != task_id
    ):
        return False
    result = None
    if value is not None:
        counts = {
            key: int(value["counts"][key])
            for key in (
                "total_assertions",
                "physical_groups",
                "protected_groups",
                "retained_groups",
                "blocked_groups",
                "unreferenced_groups",
            )
        }
        row.deltas = [
            {
                "metric": key,
                "before": row.counts.get(key, 0),
                "after": count,
                "change": count - row.counts.get(key, 0),
            }
            for key, count in counts.items()
            if count != row.counts.get(key, 0)
        ][:6]
        row.counts, row.unknown = counts, dict(value["unknown"])
        row.measured_at, row.last_success_at = (
            value["measured_at"],
            value["measured_at"],
        )
        status, error_code = "completed", None
        result = {
            "counts": row.counts,
            "unknown": row.unknown,
            "deltas": row.deltas,
            "measured_at": row.measured_at,
        }
    else:
        status, error_code = "failed", "reference_reconciliation_failed"
    completed_at = now()
    job_result = {
        "counts": row.counts,
        "unknown": row.unknown,
        "measured_at": row.measured_at.isoformat() if row.measured_at else None,
        "observation_only": True,
    }
    if not await advance_reconciliation(
        db,
        row,
        status,
        at=completed_at,
        error=error_code,
        result=result,
        job_result=job_result,
    ):
        return False
    committed_job(
        job,
        status=status,
        completed_at=completed_at,
        error_message=error_code,
        progress_pct=100 if status == "completed" else job.progress_pct,
        result=job_result,
    )
    await db.commit()
    return True
