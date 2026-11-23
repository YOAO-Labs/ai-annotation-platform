"""Review and atomically change new bug_attachment allocation, preserving saved owners."""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.services import storage_access, storage_maintenance, storage_registry_data
from app.services.audit import AuditAction, AuditService
from app.services.bug_attachment_storage import BugAttachmentStorageError
from app.services.bug_attachment_storage_target import probe_target, target_for
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import validate_legacy_bindings
from app.services.filesystem_storage import FilesystemStorageError
from app.services.storage_location_change import digest

AUDIENCE = "bug_attachment-binding-change"
PREVIEW_TTL = timedelta(minutes=5)


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def binding_facts(binding):
    return {
        "location_id": str(binding.location_id),
        "generation": binding.generation,
        "bucket": binding.bucket,
    }


async def candidates(db, *, limit, offset):
    rows, total = await storage_registry_data.list_binding_candidates(
        db, limit=limit, offset=offset
    )
    return {
        "items": [
            {
                "location_id": row.id,
                "name": row.display_name,
                "config_revision": row.config_revision,
            }
            for row in rows
        ],
        "total": total,
    }


async def history(db, actor_id):
    if db.autoflush:
        await db.flush()
    counts = await db.scalar(
        text("SELECT yoao_diagnostics_v1.bug_attachment_history(CAST(:actor AS uuid))"),
        {"actor": actor_id},
    )
    if counts is None:
        reject("storage_binding_authority_revoked", 403)
    return counts


async def _binding(db, *, lock=False):
    row = await storage_registry_data.get_binding(
        db,
        "bug-reports",
        lock="update_nowait" if lock else None,
    )
    if row is None:
        reject("bug_attachment_storage_binding_unproven")
    return row


async def _lock_positions(db, current, target_id):
    ids = sorted({current.location_id, target_id})
    initial = [
        await storage_registry_data.get_location(db, identifier) for identifier in ids
    ]
    if any(row is None for row in initial):
        reject("bug_attachment_binding_target_missing", 404)
    connections = {row.id: row.connection_id for row in initial}
    connection_ids = sorted(
        {value for value in connections.values() if value is not None}
    )
    for identifier in connection_ids:
        await storage_registry_data.get_connection(db, identifier, lock="share_nowait")
    rows = [
        await storage_registry_data.get_location(db, identifier, lock="share_nowait")
        for identifier in ids
    ]
    if {row.id: row.connection_id for row in rows if row is not None} != connections:
        reject("storage_binding_preview_changed")


async def capture(db, payload, actor, api_key_id, *, lock=False):
    actor, proof = await storage_maintenance.authority(
        db, actor, api_key_id, "datasets:write", lock=lock
    )
    initial = await _binding(db)
    before = binding_facts(initial)
    if lock:
        try:
            async with db.begin_nested():
                await _lock_positions(db, initial, payload.target_location_id)
                binding = await _binding(db, lock=True)
                if binding_facts(binding) != before:
                    reject("storage_binding_preview_changed")
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("storage_binding_busy")
            raise
    else:
        binding = initial
    if binding.generation != payload.expected_generation:
        reject("storage_binding_preview_changed")
    if binding.location_id == payload.target_location_id:
        reject("bug_attachment_binding_unchanged")
    if await validate_legacy_bindings(db):
        reject("bug_attachment_storage_binding_unproven")
    location = await storage_registry_data.get_location(db, payload.target_location_id)
    if location is None:
        reject("bug_attachment_binding_target_missing", 404)
    if location.config_revision != payload.target_config_revision:
        reject("storage_binding_preview_changed")
    target = await target_for(db, location)
    connection = (
        await storage_registry_data.get_connection(db, location.connection_id)
        if location.connection_id is not None
        else None
    )
    revision = await storage_access.check(db, location.id, lock=lock)
    counts = await history(db, actor.id)
    current = await storage_registry_data.get_location(db, binding.location_id)
    previous = {
        **before,
        "location_config_revision": current.config_revision,
        "state": "configured",
        "read_policy": "consumer_owned_compatibility",
        "write_policy": "current_binding",
        "change_policy": "reviewed_bug_attachment_switch",
    }
    reviewed = {
        "actor": str(actor.id),
        "key": str(api_key_id) if api_key_id else None,
        "authority": proof,
        "current": before,
        "target": target.facts,
        "target_name": connection.name if connection is not None else location.name,
        "maintenance_revision": revision,
        "history": counts,
    }
    return binding, target, previous, reviewed


def decode_token(token):
    try:
        value = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"], audience=AUDIENCE
        )
        if not isinstance(value.get("review"), str) or not isinstance(
            value.get("measured_at"), str
        ):
            raise ValueError()
        if value["exp"] <= datetime.now(timezone.utc).timestamp():
            raise ExpiredSignatureError()
        return value
    except ExpiredSignatureError:
        reject("storage_binding_preview_expired")
    except (JWTError, ValueError, TypeError, KeyError):
        reject("storage_binding_preview_invalid")


async def preview(db, payload, actor, *, api_key_id=None):
    _, target, _, initial = await capture(db, payload, actor, api_key_id)
    await db.commit()
    try:
        capabilities = await storage_access.run(
            db,
            target.location_id,
            "bug_attachment_binding_probe",
            probe_target,
            target,
            expected_revision=initial["maintenance_revision"],
        )
    except PUBLIC_ERRORS:
        raise
    except Exception as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise storage_access.mark_unsettled(
                ContentError("storage_operation_unsettled", 503)
            ) from None
        raise ContentError("bug_attachment_storage_unavailable", 503) from None
    _, target, previous, reviewed = await capture(
        db, payload, actor, api_key_id, lock=True
    )
    if reviewed != initial:
        reject("storage_binding_preview_changed")
    measured_at = datetime.now(timezone.utc)
    expires_at = measured_at + PREVIEW_TTL
    return {
        "purpose": "bug-reports",
        "previous": previous,
        "target": {
            **{
                key: target.facts[key]
                for key in (
                    "location_id",
                    "config_revision",
                    "connection_revision",
                    "bucket",
                    "key_prefix",
                )
            },
            "name": reviewed["target_name"],
        },
        "history": reviewed["history"],
        "capabilities": capabilities,
        "can_apply": True,
        "measured_at": measured_at,
        "expires_at": expires_at,
        "impact_token": jwt.encode(
            {
                "aud": AUDIENCE,
                "exp": expires_at,
                "review": digest(reviewed),
                "measured_at": measured_at.isoformat(),
            },
            settings.secret_key,
            algorithm="HS256",
        ),
    }


async def apply(db, payload, actor, *, api_key_id=None, request=None):
    token = decode_token(payload.impact_token)
    binding, target, previous, reviewed = await capture(
        db, payload, actor, api_key_id, lock=True
    )
    if digest(reviewed) != token["review"]:
        reject("storage_binding_preview_changed")
    decode_token(payload.impact_token)
    if not await storage_registry_data.switch_binding(
        db,
        "bug-reports",
        expected_generation=binding.generation,
        location_id=target.location_id,
        bucket=target.bucket,
    ):
        reject("storage_binding_preview_changed")
    generation = binding.generation + 1
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.STORAGE_BINDING_UPDATE,
        target_type="storage_binding",
        target_id="bug-reports",
        request=request,
        detail={
            "previous": {
                key: previous[key] for key in ("location_id", "generation", "bucket")
            },
            "target": {
                "location_id": str(target.location_id),
                "bucket": target.bucket,
                "key_prefix": target.key_prefix,
                "generation": generation,
            },
            "history": reviewed["history"],
            "reviewed_at": token["measured_at"],
        },
    )
    return {
        **previous,
        "location_id": target.location_id,
        "generation": generation,
        "bucket": target.bucket,
        "location_config_revision": target.config_revision,
    }


def http_error(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return HTTPException(503, detail={"code": "storage_operation_unsettled"})
    return HTTPException(
        getattr(exc, "status_code", getattr(exc, "status", 409)),
        detail={"code": exc.code},
    )


PUBLIC_ERRORS = (BugAttachmentStorageError, ContentError, FilesystemStorageError)
