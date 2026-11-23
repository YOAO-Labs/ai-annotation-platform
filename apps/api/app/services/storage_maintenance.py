"""Explicit admission transitions require a current dependency review."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.enums import UserRole
from app.services import storage_access
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.storage_registry_data import get_connection, get_location
from app.services.storage_maintenance_targets import confirmation_counts

AUDIENCE = "storage-maintenance"
PREVIEW_TTL = timedelta(minutes=5)
TRANSITIONS = {
    ("active", "drain"): "draining",
    ("draining", "stop"): "stopped",
    ("draining", "resume"): "active",
    ("stopped", "resume"): "active",
}


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


async def authority(db, actor, api_key_id, scope, *, lock=False):
    from app.services.dataset_content import key_authority
    from app.services.storage_maintenance_impact import administrator

    if lock:
        try:
            async with db.begin_nested():
                actor = (
                    await IdentityAccountLockData(db).lock_accounts(
                        (actor.id,), mode="share"
                    )
                ).get(actor.id)
                if (
                    not actor
                    or not actor.is_active
                    or actor.role != UserRole.SUPER_ADMIN
                ):
                    reject("platform_administrator_required", 403)
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("storage_maintenance_busy")
            raise
    actor = await administrator(db, actor)
    return actor, await key_authority(db, actor, api_key_id, scope)


async def location(db, location_id, *, lock=False):
    try:
        if lock:
            async with db.begin_nested():
                row = await get_location(db, location_id, lock="update_nowait")
        else:
            row = await get_location(db, location_id)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    if row is None:
        raise HTTPException(404, "Storage location not found")
    if row.kind != "filesystem" and row.connection_id is None:
        reject("platform_maintenance_unsupported")
    return row


async def lock_gate(db, location_id, *, initialize=False):
    if initialize:
        from app.services.content_snapshot import ContentError

        try:
            await storage_access.ensure(db, location_id)
        except ContentError as exc:
            reject(exc.code, exc.status)
    try:
        async with db.begin_nested():
            return await storage_access.gate_facts(
                db, location_id, lock="update_nowait"
            )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise


_MAINTENANCE_STATUS = text(
    "SELECT * FROM yoao_assets_storage_v1.maintenance_status(:location)"
)
_TRANSITION = text(
    "SELECT * FROM yoao_assets_storage_v1.transition_gate(:location,:revision,:operation,:actor)"
)


async def snapshot(db, location_id):
    rows = await db.execute(_MAINTENANCE_STATUS, {"location": location_id})
    result = dict(rows.mappings().one())
    return {
        "location_id": location_id,
        **result,
        "protocol_enabled": settings.storage_maintenance_protocol_enabled,
        "has_more": result["open_operations"] > len(result["operations"]),
        "coverage": "registered_location_access_only",
        "physical_cleanup_permission": False,
    }


async def status(db, location_id, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id, "datasets:read")
    where = await location(db, location_id)
    config_revision = where.config_revision
    result = await snapshot(db, location_id)
    _, current = await authority(db, actor, api_key_id, "datasets:read")
    if proof != current:
        reject("api_key_authority_revoked", 403)
    if (await location(db, location_id)).config_revision != config_revision:
        reject("storage_maintenance_preview_changed")
    return result


async def review(db, location_id, payload, actor, api_key_id, *, initialize=False):
    from app.services import storage_maintenance_impact as impact_service

    if not settings.storage_maintenance_protocol_enabled:
        reject("storage_maintenance_protocol_unqualified")
    actor, proof = await authority(db, actor, api_key_id, "datasets:write", lock=True)
    where = await location(db, location_id, lock=True)
    gate = await lock_gate(db, location_id, initialize=initialize)
    current_state, revision = (gate.state, gate.revision) if gate else ("active", 1)
    if revision != payload.expected_revision:
        reject("storage_maintenance_changed")
    target = TRANSITIONS.get((current_state, payload.operation))
    if target is None:
        reject("storage_maintenance_transition_invalid")
    if payload.operation == "resume":
        from app.services.managed_original_reclaim_fence import require_resumable
        from app.services.storage_migration_cutovers import require_target_resumable

        await require_resumable(db, location_id)
        await require_target_resumable(db, location_id)
    connection = (
        await get_connection(db, where.connection_id) if where.connection_id else None
    )
    connection_revision = connection.revision if connection else None
    binding = digest(
        {
            "actor": actor.id,
            "key": api_key_id,
            "authority": proof,
            "location": where.id,
            "kind": where.kind,
            "config_revision": where.config_revision,
            "name": where.name,
            "enabled": where.enabled,
            "config": where.config,
            "connection_id": where.connection_id,
            "connection_revision": connection_revision,
            "state": current_state,
            "revision": revision,
            "operation": payload.operation,
            "target_state": target,
        }
    )
    dependencies = dict(
        (
            await db.execute(
                impact_service.dependencies(location_id, with_fingerprint=True)
            )
        )
        .mappings()
        .one()
    )
    measured_at = dependencies.pop("measured_at")
    fingerprint = dependencies.pop("dependency_fingerprint")
    counts = {key: int(value) for key, value in dependencies.items()}
    result = await snapshot(db, location_id)
    if payload.operation == "stop" and result["open_operations"]:
        reject("storage_maintenance_unsettled")
    _, current_proof = await authority(db, actor, api_key_id, "datasets:write")
    if proof != current_proof:
        reject("api_key_authority_revoked", 403)
    if connection:
        current_connection = await get_connection(db, where.connection_id)
        if (
            current_connection is None
            or current_connection.revision != connection_revision
        ):
            reject("storage_maintenance_preview_changed")
    result["measured_at"] = measured_at
    result.update(
        operation=payload.operation,
        target_state=target,
        impact={
            "location_id": location_id,
            "config_revision": where.config_revision,
            "measured_at": measured_at,
            "scope": "stored_dependencies",
            "counts": counts,
            "frozen_coverage": "recorded_location_lineage_only",
            "input_coverage": "current_task_links_only",
            "execution_permission": False,
        },
    )
    return (
        result,
        gate,
        actor,
        binding,
        digest(
            {
                "fingerprint": fingerprint,
                "counts": confirmation_counts(counts),
            }
        ),
    )


async def preview(db, location_id, payload, actor, *, api_key_id=None):
    result, _, _, binding, dependencies = await review(
        db, location_id, payload, actor, api_key_id
    )
    expires_at = result["measured_at"] + PREVIEW_TTL
    result.update(
        expires_at=expires_at,
        impact_token=jwt.encode(
            {
                "aud": AUDIENCE,
                "exp": expires_at,
                "binding": binding,
                "dependencies": dependencies,
                "measured_at": result["measured_at"].isoformat(),
            },
            settings.secret_key,
            algorithm="HS256",
        ),
    )
    return result


def decode_token(token):
    try:
        data = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if not all(
            key in data for key in ("exp", "binding", "dependencies", "measured_at")
        ):
            raise ValueError()
        if data["exp"] <= datetime.now(timezone.utc).timestamp():
            raise ExpiredSignatureError()
        return data
    except ExpiredSignatureError:
        reject("storage_maintenance_preview_expired")
    except (JWTError, TypeError, ValueError):
        reject("storage_maintenance_preview_invalid")


async def apply(db, location_id, payload, actor, *, api_key_id=None, request=None):
    from app.services.audit import AuditService

    if not settings.storage_maintenance_protocol_enabled:
        reject("storage_maintenance_protocol_unqualified")
    token = decode_token(payload.impact_token)
    reviewed, gate, actor, binding, dependencies = await review(
        db, location_id, payload, actor, api_key_id, initialize=True
    )
    if token["binding"] != binding or token["dependencies"] != dependencies:
        reject("storage_maintenance_preview_changed")
    decode_token(payload.impact_token)
    previous_revision, previous_state = gate.revision, gate.state
    result = await db.execute(
        _TRANSITION,
        {
            "location": location_id,
            "revision": previous_revision,
            "operation": payload.operation,
            "actor": actor.id,
        },
    )
    gate = result.mappings().one_or_none()
    if gate is None:
        reject("storage_maintenance_changed")
    await AuditService.log(
        db,
        actor=actor,
        action=f"storage_location.maintenance_{payload.operation}",
        target_type="storage_location",
        target_id=str(location_id),
        request=request,
        status_code=200,
        detail={
            "operation": payload.operation,
            "previous_state": previous_state,
            "state": gate.state,
            "previous_revision": previous_revision,
            "revision": gate.revision,
            "counts": reviewed["impact"]["counts"],
            "reviewed_at": token["measured_at"],
            "preview_expires_at": datetime.fromtimestamp(
                token["exp"], timezone.utc
            ).isoformat(),
            "coverage": "registered_location_access_only",
            "physical_cleanup_permission": False,
        },
    )
    return await snapshot(db, location_id)
