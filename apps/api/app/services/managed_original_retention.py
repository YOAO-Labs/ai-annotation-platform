"""Review assertion policies and operator-declared requirements, never object I/O."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import settings
from app.db.models.managed_original_retention_hold import (
    ManagedOriginalRetentionHold as Hold,
)
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.schemas.managed_original_retention import (
    ManagedOriginalRetentionPreview,
    ManagedOriginalRetentionState,
)
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import storage_service

AUDIENCE = "managed-original-retention-change"
PREVIEW_TTL = timedelta(minutes=5)

_CONFIGURATION_EXPRESSION = """jsonb_build_object(
    'location', (SELECT to_jsonb(l) FROM storage_locations l WHERE l.id = CAST(:platform_id AS uuid)),
    'bindings', (SELECT COALESCE(jsonb_agg(to_jsonb(b) ORDER BY b.purpose), '[]'::jsonb) FROM storage_bindings b)
)"""
_CONFIGURATION = text("SELECT " + _CONFIGURATION_EXPRESSION)
_OBSERVATION = text(
    """
WITH anchor AS MATERIALIZED (
    SELECT * FROM managed_original_retirements WHERE id = CAST(:retirement_id AS uuid)
), peers AS MATERIALIZED (
    SELECT r.* FROM managed_original_retirements r JOIN anchor a
        ON md5(r.bucket) = md5(a.bucket) AND md5(r.object_key) = md5(a.object_key)
        AND r.bucket = a.bucket AND r.object_key = a.object_key
), holds AS MATERIALIZED (
    SELECT h.* FROM managed_original_retention_holds h JOIN peers p ON p.id = h.retirement_id
), deadlines AS (
    SELECT bool_or(retain_until IS NULL) AS indefinite, max(retain_until) AS effective,
        bool_or(CASE WHEN id = CAST(:retirement_id AS uuid) AND :action = 'set_retention'
            THEN CAST(:retain_until AS timestamptz) ELSE retain_until END IS NULL) AS after_indefinite,
        max(CASE WHEN id = CAST(:retirement_id AS uuid) AND :action = 'set_retention'
            THEN CAST(:retain_until AS timestamptz) ELSE retain_until END) AS after_effective,
        count(*) AS peer_assertions FROM peers
), requirements AS (
    SELECT count(*) FILTER (WHERE released_at IS NULL AND kind = 'backup') AS backup_holds,
        count(*) FILTER (WHERE released_at IS NULL AND kind = 'migration') AS migration_holds,
        count(*) AS total_holds FROM holds
), history AS (
    SELECT * FROM holds ORDER BY (released_at IS NULL) DESC, created_at DESC, id LIMIT :limit OFFSET :offset
)
SELECT statement_timestamp() AS measured_at, to_jsonb(a) AS anchor,
    __CONFIGURATION__ AS configuration,
    (SELECT encode(sha256(convert_to(COALESCE(string_agg(
        encode(sha256(convert_to(to_jsonb(p)::text, 'UTF8')), 'hex'), '' ORDER BY p.id
    ), ''), 'UTF8')), 'hex') FROM peers p) AS peer_fingerprint,
    (SELECT encode(sha256(convert_to(COALESCE(string_agg(
        encode(sha256(convert_to(to_jsonb(h)::text, 'UTF8')), 'hex'), '' ORDER BY h.id
    ), ''), 'UTF8')), 'hex') FROM holds h) AS hold_fingerprint,
    jsonb_build_object('retain_until', a.retain_until,
        'effective_retain_until', CASE WHEN d.indefinite THEN NULL ELSE d.effective END,
        'indefinite_retention', d.indefinite, 'peer_assertions', d.peer_assertions,
        'backup_holds', q.backup_holds, 'migration_holds', q.migration_holds) AS before,
    jsonb_build_object('retain_until', CASE WHEN :action = 'set_retention' THEN CAST(:retain_until AS timestamptz) ELSE a.retain_until END,
        'effective_retain_until', CASE WHEN d.after_indefinite THEN NULL ELSE d.after_effective END,
        'indefinite_retention', d.after_indefinite, 'peer_assertions', d.peer_assertions,
        'backup_holds', q.backup_holds + CASE WHEN :action = 'add_hold' AND :hold_kind = 'backup' THEN 1
            WHEN :action = 'release_hold' AND selected.kind = 'backup' AND selected.released_at IS NULL THEN -1 ELSE 0 END,
        'migration_holds', q.migration_holds + CASE WHEN :action = 'add_hold' AND :hold_kind = 'migration' THEN 1
            WHEN :action = 'release_hold' AND selected.kind = 'migration' AND selected.released_at IS NULL THEN -1 ELSE 0 END) AS after,
    q.total_holds, COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY (h.released_at IS NULL) DESC, h.created_at DESC, h.id) FROM history h), '[]'::jsonb) AS history,
    to_jsonb(selected) AS selected_hold,
    EXISTS(SELECT 1 FROM managed_original_retention_holds h
        WHERE h.retirement_id = a.id AND h.kind = :hold_kind AND h.reference_code = :reference_code AND h.released_at IS NULL) AS duplicate_reference
FROM anchor a CROSS JOIN deadlines d CROSS JOIN requirements q
LEFT JOIN managed_original_retention_holds selected ON selected.id = CAST(:hold_id AS uuid)
""".replace("__CONFIGURATION__", _CONFIGURATION_EXPRESSION)
)


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
                        "retirement_id": str(retirement_id),
                        "platform_id": str(LEGACY_LOCATION_ID),
                        "action": payload.action if payload else "read",
                        "retain_until": payload.retain_until if payload else None,
                        "hold_id": str(payload.hold_id)
                        if payload and payload.hold_id
                        else None,
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
    current_config = await db.scalar(
        _CONFIGURATION, {"platform_id": str(LEGACY_LOCATION_ID)}
    )
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
    locator = (
        await db.execute(
            select(Retirement.bucket, Retirement.object_key).where(
                Retirement.id == retirement_id
            )
        )
    ).one_or_none()
    if locator is None:
        raise HTTPException(404, "Retirement assertion not found")
    from app.services.managed_original_reclaim_fence import exclusive, shared

    # Policy mutations remain serialized with one another. The shared boundary
    # first checks the durable claim, using the same identity as GC/publication.
    try:
        await shared(db, *locator)
        await exclusive(db, *locator)
    except HTTPException as exc:
        if exc.detail == {"code": "managed_original_reclaim_busy"}:
            reject("managed_retention_busy")
        raise
    try:
        async with db.begin_nested():
            anchor = await db.scalar(
                select(Retirement)
                .where(Retirement.id == retirement_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
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
    hold = None
    if payload.action == "release_hold":
        try:
            async with db.begin_nested():
                hold = await db.scalar(
                    select(Hold)
                    .where(
                        Hold.id == payload.hold_id, Hold.retirement_id == retirement_id
                    )
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
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
    # A savepoint keeps all local mutations and audit atomic even when a caller
    # catches an audit error and continues using its surrounding transaction.
    try:
        async with db.begin_nested():
            if payload.action == "set_retention":
                anchor.retain_until = payload.retain_until
            elif payload.action == "add_hold":
                db.add(
                    Hold(
                        id=payload.hold_id,
                        retirement_id=anchor.id,
                        kind=payload.hold_kind,
                        reference_code=payload.reference_code,
                        reason=payload.reason,
                        created_by=actor.id,
                        created_at=changed_at,
                    )
                )
            else:
                hold.revision += 1
                hold.released_at, hold.released_by, hold.release_reason = (
                    changed_at,
                    actor.id,
                    payload.reason,
                )
            anchor.policy_revision += 1
            await db.flush()
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
                    "policy_revision": anchor.policy_revision,
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
