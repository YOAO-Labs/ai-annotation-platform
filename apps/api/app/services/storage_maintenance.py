"""Explicit admission transitions require a current dependency review."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, UUID, aggregate_order_by
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.enums import UserRole
from app.db.models.api_key import ApiKey
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation as Operation,
)
from app.db.models.user import User
from app.services import storage_access
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
                actor = await db.scalar(
                    select(User)
                    .where(User.id == actor.id)
                    .with_for_update(read=True, nowait=True)
                    .execution_options(populate_existing=True)
                )
                if (
                    not actor
                    or not actor.is_active
                    or actor.role != UserRole.SUPER_ADMIN
                ):
                    reject("platform_administrator_required", 403)
                if api_key_id:
                    await db.scalar(
                        select(ApiKey)
                        .where(ApiKey.id == api_key_id)
                        .with_for_update(read=True, nowait=True)
                        .execution_options(populate_existing=True)
                    )
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("storage_maintenance_busy")
            raise
    actor = await administrator(db, actor)
    return actor, await key_authority(db, actor, api_key_id, scope)


async def location(db, location_id, *, lock=False):
    statement = (
        select(StorageLocation)
        .where(StorageLocation.id == location_id)
        .execution_options(populate_existing=True)
    )
    try:
        if lock:
            async with db.begin_nested():
                row = await db.scalar(statement.with_for_update(nowait=True))
        else:
            row = await db.scalar(statement)
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
            return await db.scalar(
                select(Gate)
                .where(Gate.location_id == location_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise


def status_query(location_id):
    selected = (
        select(
            Operation.id,
            Operation.purpose,
            Operation.node_id,
            Operation.revision,
            Operation.started_at,
        )
        .where(Operation.location_id == location_id)
        .order_by(Operation.started_at, Operation.id)
        .limit(50)
        .subquery()
    )
    operations = select(
        func.coalesce(
            func.jsonb_agg(
                aggregate_order_by(
                    func.jsonb_build_object(
                        "id",
                        selected.c.id,
                        "purpose",
                        selected.c.purpose,
                        "node_id",
                        selected.c.node_id,
                        "revision",
                        selected.c.revision,
                        "started_at",
                        selected.c.started_at,
                    ),
                    selected.c.started_at,
                    selected.c.id,
                )
            ),
            literal([], type_=JSONB),
        )
    ).scalar_subquery()
    count = (
        select(func.count())
        .select_from(Operation)
        .where(Operation.location_id == location_id)
        .scalar_subquery()
    )
    anchor = select(
        literal(location_id, type_=UUID(as_uuid=True)).label("location_id")
    ).subquery()
    return select(
        func.coalesce(Gate.state, "active").label("state"),
        func.coalesce(Gate.revision, 1).label("revision"),
        Gate.changed_at,
        Gate.changed_by,
        func.statement_timestamp().label("measured_at"),
        count.label("open_operations"),
        operations.label("operations"),
    ).select_from(anchor.outerjoin(Gate, Gate.location_id == anchor.c.location_id))


async def snapshot(db, location_id):
    result = dict((await db.execute(status_query(location_id))).mappings().one())
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
    connection = (
        await db.get(StorageConnection, where.connection_id, populate_existing=True)
        if where.connection_id
        else None
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
        current_connection = await db.get(
            StorageConnection, where.connection_id, populate_existing=True
        )
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
    gate.state = reviewed["target_state"]
    gate.revision += 1
    gate.changed_at = await db.scalar(select(func.statement_timestamp()))
    gate.changed_by = actor.id
    await db.flush()
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
