"""Review assertion policies and operator-declared requirements, never object I/O."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import settings
from app.schemas.managed_original_retention import (
    ManagedOriginalRetentionPreview,
    ManagedOriginalRetentionState,
)
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_lifecycle_data import retirement_anchor
from app.services.storage import storage_service

AUDIENCE = "managed-original-retention-change"
PREVIEW_TTL = timedelta(minutes=5)

_CONFIGURATION = text("SELECT yoao_assets_v1.legacy_original_registry_snapshot()")
_OBSERVATION = text("""
SELECT measured_at, anchor, configuration, peer_fingerprint, hold_fingerprint,
    before_state AS before, after_state AS after, total_holds, history, selected_hold, duplicate_reference
FROM yoao_assets_v1.observe_original_retention(
    :retirement_id, :platform_id, :action, :retain_until, :hold_id, :hold_kind,
    :reference_code, :limit, :offset
)
""")


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


def configured_identity():
    return (
        storage_service.client.meta.endpoint_url,
        storage_service.datasets_bucket,
        settings.minio_endpoint,
        settings.minio_use_ssl,
    )


def group_lock_identity(bucket, object_key):
    return json.dumps(
        ["managed-original-retention", bucket, object_key], separators=(",", ":")
    )


async def _authority(db, actor, api_key_id, *, lock=False):
    try:
        return await storage_maintenance.authority(
            db, actor, api_key_id, "*", lock=lock
        )
    except HTTPException as exc:
        if exc.detail == {"code": "storage_maintenance_busy"}:
            reject("managed_retention_busy")
        raise


async def _observe(db, retirement_id, payload=None, *, limit=20, offset=0):
    with db.no_autoflush:
        row = (
            (
                await db.execute(
                    _OBSERVATION,
                    {
                        "retirement_id": retirement_id,
                        "platform_id": LEGACY_LOCATION_ID,
                        "action": payload.action if payload else "read",
                        "retain_until": payload.retain_until if payload else None,
                        "hold_id": payload.hold_id if payload else None,
                        "hold_kind": payload.hold_kind if payload else None,
                        "reference_code": payload.reference_code if payload else None,
                        "limit": limit,
                        "offset": offset,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(404, "Retirement assertion not found")
    return row


async def _recheck(db, actor, api_key_id, proof, configured, configuration):
    current, current_proof = await _authority(db, actor, api_key_id)
    if proof != current_proof:
        reject("api_key_authority_revoked", 403)
    current_config = await db.scalar(_CONFIGURATION)
    if configured != configured_identity() or configuration != current_config:
        reject("managed_retention_configuration_changed")
    return current


def _state(row, limit, offset):
    return ManagedOriginalRetentionState(
        retirement_id=row["anchor"]["id"],
        policy_revision=row["anchor"]["policy_revision"],
        measured_at=row["measured_at"],
        summary=row["before"],
        holds=row["history"],
        total_holds=row["total_holds"],
        limit=limit,
        offset=offset,
        has_more=offset + len(row["history"]) < row["total_holds"],
    )


async def state(db, retirement_id, actor, *, api_key_id=None, limit=20, offset=0):
    with db.no_autoflush:
        actor, proof = await _authority(db, actor, api_key_id)
        configured = configured_identity()
        row = await _observe(db, retirement_id, limit=limit, offset=offset)
        await _recheck(db, actor, api_key_id, proof, configured, row["configuration"])
    return _state(row, limit, offset)


def _validate_candidate(row, payload, *, applying=False):
    if row["anchor"]["policy_revision"] != payload.expected_revision:
        reject("managed_retention_changed")
    if payload.action == "set_retention":
        if (
            payload.retain_until is not None
            and payload.retain_until <= row["measured_at"]
        ):
            reject("managed_retention_deadline_not_future", 409 if applying else 422)
    elif payload.action == "add_hold":
        if row["selected_hold"] is not None:
            reject("managed_retention_hold_id_exists")
        if row["duplicate_reference"]:
            reject("managed_retention_hold_reference_exists")
    else:
        hold = row["selected_hold"]
        if hold is None or hold["retirement_id"] != row["anchor"]["id"]:
            raise HTTPException(404, "Retention hold not found at this assertion")
        if (
            hold["revision"] != payload.expected_hold_revision
            or hold["released_at"] is not None
        ):
            reject("managed_retention_hold_changed")


async def _review(db, retirement_id, payload, actor, api_key_id, *, applying=False):
    actor, proof = await _authority(db, actor, api_key_id)
    configured = configured_identity()
    row = await _observe(db, retirement_id, payload, limit=0)
    _validate_candidate(row, payload, applying=applying)
    actor = await _recheck(
        db, actor, api_key_id, proof, configured, row["configuration"]
    )
    binding = digest(
        {
            "actor": actor.id,
            "key": api_key_id,
            "authority": proof,
            "anchor": row["anchor"],
            "configured": configured,
            "configuration": row["configuration"],
            "candidate": payload.model_dump(
                mode="json", exclude={"impact_token"}, exclude_unset=True
            ),
        }
    )
    dependencies = digest(
        {"peers": row["peer_fingerprint"], "holds": row["hold_fingerprint"]}
    )
    return actor, row, binding, dependencies


async def preview(db, retirement_id, payload, actor, *, api_key_id=None):
    with db.no_autoflush:
        _, row, binding, dependencies = await _review(
            db, retirement_id, payload, actor, api_key_id
        )
    measured_at = row["measured_at"]
    expires_at = measured_at + PREVIEW_TTL
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": expires_at,
            "binding": binding,
            "dependencies": dependencies,
            "measured_at": measured_at.isoformat(),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    before_until = row["before"]["retain_until"]
    before_until = datetime.fromisoformat(before_until) if before_until else None
    selected = row["selected_hold"]
    return ManagedOriginalRetentionPreview(
        retirement_id=retirement_id,
        policy_revision=row["anchor"]["policy_revision"],
        action=payload.action,
        measured_at=measured_at,
        expires_at=expires_at,
        impact_token=token,
        before=row["before"],
        after=row["after"],
        shortens_retention=payload.action == "set_retention"
        and payload.retain_until is not None
        and (before_until is None or payload.retain_until < before_until),
        hold_id=payload.hold_id,
        hold_kind=payload.hold_kind or (selected["kind"] if selected else None),
        reference_code=payload.reference_code
        or (selected["reference_code"] if selected else None),
    )


def decode_token(token):
    try:
        data = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if not all(
            field in data for field in ("exp", "binding", "dependencies", "measured_at")
        ):
            raise ValueError()
        if data["exp"] <= datetime.now(timezone.utc).timestamp():
            raise ExpiredSignatureError()
        return data
    except ExpiredSignatureError:
        reject("managed_retention_preview_expired")
    except (JWTError, ValueError, TypeError):
        reject("managed_retention_preview_invalid")


async def _locked_anchor(db, retirement_id):
    facts = await retirement_anchor(db, retirement_id)
    if facts is None:
        raise HTTPException(404, "Retirement assertion not found")
    locator = (facts.bucket, facts.object_key)
    from app.services.managed_original_reclaim_fence import exclusive, shared

    try:
        await shared(db, *locator)
        await exclusive(db, *locator)
    except HTTPException as exc:
        if exc.detail == {"code": "managed_original_reclaim_busy"}:
            reject("managed_retention_busy")
        raise
    try:
        async with db.begin_nested():
            anchor = await retirement_anchor(db, retirement_id, lock=True)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("managed_retention_busy")
        raise
    if anchor is None or (anchor.bucket, anchor.object_key) != tuple(locator):
        reject("managed_retention_changed")
    return anchor


async def apply(db, retirement_id, payload, actor, *, api_key_id=None, request=None):
    token = decode_token(payload.impact_token)
    actor, _ = await _authority(db, actor, api_key_id, lock=True)
    anchor = await _locked_anchor(db, retirement_id)
    if payload.action == "release_hold":
        try:
            async with db.begin_nested():
                await db.scalar(
                    text(
                        "SELECT yoao_assets_v1.lock_original_retention_hold(:retirement, :hold)"
                    ),
                    {"retirement": retirement_id, "hold": payload.hold_id},
                )
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("managed_retention_busy")
            raise
    actor, row, binding, dependencies = await _review(
        db, retirement_id, payload, actor, api_key_id, applying=True
    )
    if token["binding"] != binding or token["dependencies"] != dependencies:
        reject("managed_retention_preview_changed")
    decode_token(payload.impact_token)
    changed_at = await db.scalar(select(func.statement_timestamp()))
    if (
        payload.action == "set_retention"
        and payload.retain_until is not None
        and payload.retain_until <= changed_at
    ):
        reject("managed_retention_deadline_not_future")
    previous_revision = anchor.policy_revision
    try:
        async with db.begin_nested():
            revision = await db.scalar(
                text("""SELECT yoao_assets_v1.apply_original_retention(
                    :retirement, :previous, :action, :deadline, :hold, :hold_revision,
                    :kind, :reference, :reason, :actor, :at
                )"""),
                {
                    "retirement": retirement_id,
                    "previous": previous_revision,
                    "action": payload.action,
                    "deadline": payload.retain_until,
                    "hold": payload.hold_id,
                    "hold_revision": payload.expected_hold_revision,
                    "kind": payload.hold_kind,
                    "reference": payload.reference_code,
                    "reason": payload.reason,
                    "actor": actor.id,
                    "at": changed_at,
                },
            )
            if revision is None:
                reject("managed_retention_changed")
            await AuditService.log(
                db,
                actor=actor,
                action=f"managed_original.{payload.action}",
                target_type="managed_original_retirement",
                target_id=str(anchor.id),
                request=request,
                status_code=200,
                detail={
                    "action": payload.action,
                    "previous_revision": previous_revision,
                    "policy_revision": revision,
                    "reason": payload.reason,
                    "before": row["before"],
                    "after": row["after"],
                    "hold_id": str(payload.hold_id) if payload.hold_id else None,
                    "hold_kind": payload.hold_kind
                    or (row["selected_hold"]["kind"] if row["selected_hold"] else None),
                    "reference_code": payload.reference_code
                    or (
                        row["selected_hold"]["reference_code"]
                        if row["selected_hold"]
                        else None
                    ),
                    "reviewed_at": token["measured_at"],
                    "preview_expires_at": datetime.fromtimestamp(
                        token["exp"], timezone.utc
                    ).isoformat(),
                    "rechecked_at": changed_at.isoformat(),
                    "coverage": "declared_requirements_only",
                    "execution_permission": False,
                },
            )
            result = await state(db, retirement_id, actor, api_key_id=api_key_id)
    except IntegrityError:
        reject("managed_retention_conflict")
    return result
