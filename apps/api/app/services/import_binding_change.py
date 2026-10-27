"""Review new import allocation without relocating saved temporary inputs."""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditAction, AuditService
from app.services.import_storage import ImportStorageError
from app.services.import_storage_target import probe_target, target_for
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, validate_legacy_bindings
from app.services.filesystem_storage import FilesystemStorageError
from app.services.storage_location_change import digest

AUDIENCE = "import-binding-change"
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
    display_name = func.coalesce(StorageConnection.name, StorageLocation.name)
    eligible = or_(
        StorageLocation.id == LEGACY_LOCATION_ID,
        (StorageConnection.kind == "s3") & (StorageConnection.scope == "global"),
    )
    query = (
        select(StorageLocation, display_name.label("display_name"))
        .outerjoin(
            StorageConnection, StorageConnection.id == StorageLocation.connection_id
        )
        .where(StorageLocation.enabled, StorageLocation.kind == "s3", eligible)
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = await db.execute(
        query.order_by(display_name, StorageLocation.id).limit(limit).offset(offset)
    )
    return {
        "items": [
            {
                "location_id": row.id,
                "name": name,
                "config_revision": row.config_revision,
            }
            for row, name in rows
        ],
        "total": total,
    }


async def history(db):
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.managed_original_creation import ManagedOriginalCreation
    from app.db.models.mask_format_upload import MaskFormatUpload
    from app.db.models.mask_format_import import MaskFormatImport

    counts = dict(
        (
            await db.execute(
                select(
                    *(
                        func.count()
                        .filter(PredictionInputLease.state == state)
                        .label("lease_" + state)
                        for state in ("active", "cleanup_pending", "deleted")
                    )
                ).select_from(PredictionInputLease)
            )
        )
        .mappings()
        .one()
    )
    counts["creation_staging"] = await db.scalar(
        select(func.count())
        .select_from(ManagedOriginalCreation)
        .where(ManagedOriginalCreation.staging_key.is_not(None))
    )
    counts["mask_uploads"] = await db.scalar(
        select(func.count()).select_from(MaskFormatUpload)
    )
    counts["mask_batches"] = await db.scalar(
        select(func.count()).select_from(MaskFormatImport)
    )
    return counts


async def _binding(db, *, lock=False):
    query = select(StorageBinding).where(StorageBinding.purpose == "import")
    if lock:
        query = query.with_for_update(nowait=True)
    row = await db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        reject("import_storage_binding_unproven")
    return row


async def _lock_positions(db, current, target_id):
    ids = sorted({current.location_id, target_id})
    initial = list(
        await db.scalars(select(StorageLocation).where(StorageLocation.id.in_(ids)))
    )
    if len(initial) != len(ids):
        reject("import_binding_target_missing", 404)
    connections = {row.id: row.connection_id for row in initial}
    connection_ids = sorted(
        {value for value in connections.values() if value is not None}
    )
    if connection_ids:
        await db.execute(
            select(StorageConnection.id)
            .where(StorageConnection.id.in_(connection_ids))
            .order_by(StorageConnection.id)
            .with_for_update(read=True, nowait=True)
        )
    rows = list(
        await db.scalars(
            select(StorageLocation)
            .where(StorageLocation.id.in_(ids))
            .order_by(StorageLocation.id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    )
    if {row.id: row.connection_id for row in rows} != connections:
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
        reject("import_binding_unchanged")
    if await validate_legacy_bindings(db):
        reject("import_storage_binding_unproven")
    location = await db.get(
        StorageLocation, payload.target_location_id, populate_existing=True
    )
    if location is None:
        reject("import_binding_target_missing", 404)
    if location.config_revision != payload.target_config_revision:
        reject("storage_binding_preview_changed")
    target = await target_for(db, location)
    connection = (
        await db.get(StorageConnection, location.connection_id, populate_existing=True)
        if location.connection_id is not None
        else None
    )
    revision = await storage_access.check(db, location.id, lock=lock)
    counts = await history(db)
    current = await db.get(StorageLocation, binding.location_id, populate_existing=True)
    previous = {
        "purpose": "import",
        **before,
        "location_config_revision": current.config_revision,
        "state": "configured",
        "read_policy": "consumer_owned_compatibility",
        "write_policy": "current_binding",
        "change_policy": "reviewed_import_switch",
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
    # Never retain authority, location or binding row locks across provider I/O.
    await db.commit()
    try:
        capabilities = await storage_access.run(
            db,
            target.location_id,
            "import_binding_probe",
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
        raise ContentError("import_storage_unavailable", 503) from None
    _, target, previous, reviewed = await capture(
        db, payload, actor, api_key_id, lock=True
    )
    if reviewed != initial:
        reject("storage_binding_preview_changed")
    measured_at = datetime.now(timezone.utc)
    expires_at = measured_at + PREVIEW_TTL
    return {
        "purpose": "import",
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
    binding.location_id = target.location_id
    binding.bucket = target.bucket
    binding.generation += 1
    await db.flush()
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.STORAGE_BINDING_UPDATE,
        target_type="storage_binding",
        target_id="import",
        request=request,
        detail={
            "previous": {
                key: previous[key] for key in ("location_id", "generation", "bucket")
            },
            "target": {
                "location_id": str(target.location_id),
                "bucket": target.bucket,
                "key_prefix": target.key_prefix,
                "generation": binding.generation,
            },
            "history": reviewed["history"],
            "reviewed_at": token["measured_at"],
        },
    )
    return {
        **previous,
        "location_id": target.location_id,
        "generation": binding.generation,
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


PUBLIC_ERRORS = (ImportStorageError, ContentError, FilesystemStorageError)
