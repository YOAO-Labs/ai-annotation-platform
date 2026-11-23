"""Fence registered access while admitting only bound migration source readers."""

from dataclasses import dataclass
from datetime import datetime
import os
import socket
import uuid

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.storage_migration_window import (
    StorageMigrationWindowHistory,
    StorageMigrationWindowItem,
)
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.storage_migration_observations import endpoint_identity

_WINDOW = text("SELECT * FROM yoao_assets_v1.read_migration_window(:window_id, :lock)")
_HISTORY = text(
    "SELECT * FROM yoao_assets_v1.migration_window_history(:limit, :offset)"
)
_HISTORY_COUNT = text("SELECT yoao_assets_v1.migration_window_history_count()")
_OPEN_OPERATIONS = text(
    "SELECT yoao_assets_v1.migration_window_open_operations(:window_id)"
)
_ACTIVE = text("SELECT yoao_assets_v1.migration_window_exists()")
_OPEN = text(
    "SELECT yoao_assets_v1.open_migration_window("
    ":window_id, :parent_id, :actor_id, :expected_gate_revision, :source, :target)"
).bindparams(bindparam("source", type_=JSONB), bindparam("target", type_=JSONB))
_TRANSITION = text(
    "SELECT yoao_assets_v1.transition_migration_window("
    ":window_id, :expected_revision, :action, :actor_id)"
)
_BUMP_FINALIZATION = text(
    "SELECT yoao_assets_v1.bump_migration_window_finalization_revision("
    ":window_id, :expected_revision)"
)
_REGISTER_SOURCE = text(
    "SELECT yoao_assets_v1.register_migration_window_source_read("
    ":window_id, :window_revision, :gate_revision, :owner_kind, :owner_id, :task_id, "
    ":actor_id, :authority, :source, :operation_id, :node)"
).bindparams(bindparam("authority", type_=JSONB), bindparam("source", type_=JSONB))


@dataclass(frozen=True, slots=True)
class WindowSummary:
    id: uuid.UUID
    parent_copy_id: uuid.UUID
    target_location_id: uuid.UUID
    state: str
    revision: int
    source_gate_revision: int
    source_config_revision: int
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WindowFacts(WindowSummary):
    source_location_id: uuid.UUID
    source: dict
    target: dict


def reject(code, status=409):
    raise HTTPException(status, detail={"code": code})


def enabled():
    if not settings.storage_maintenance_protocol_enabled:
        reject("storage_maintenance_protocol_unqualified")


async def authority(db, actor, key_id=None, *, lock=False):
    return await storage_maintenance.authority(db, actor, key_id, "*", lock=lock)


def physical_source(source):
    endpoint = endpoint_identity(source.get("endpoint"))
    buckets = source.get("purpose_buckets")
    from app.services.storage_migration_inventory import PURPOSES

    if (
        endpoint is None
        or not isinstance(buckets, dict)
        or set(buckets) != PURPOSES
        or any(not isinstance(value, str) or not value for value in buckets.values())
    ):
        reject("migration_window_source_changed")
    return endpoint, buckets


async def _window(db, window_id, lock) -> WindowFacts | None:
    if db.autoflush:
        await db.flush()
    row = (
        (await db.execute(_WINDOW, {"window_id": window_id, "lock": lock}))
        .mappings()
        .one_or_none()
    )
    return WindowFacts(**row) if row is not None else None


async def copy_scope(db, window_id, *, lock=False) -> WindowFacts | None:
    return await _window(db, window_id, "share" if lock else None)


async def owner(db, window_id, *, lock=False, exclusive=False) -> WindowFacts:
    mode = ("update_nowait" if exclusive else "share_nowait") if lock else None
    try:
        row = await _window(db, window_id, mode)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    if row is None:
        raise HTTPException(404, "Migration window not found")
    return row


async def gate(db, row, *, exclusive=False):
    try:
        current = await storage_access.gate_facts(
            db,
            row.source_location_id,
            lock="update_nowait" if exclusive else "share_nowait",
        )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    expected = "draining" if row.state == "draining" else "stopped"
    if row.state == "closing":
        valid_state = current is not None and current["state"] in {
            "draining",
            "stopped",
        }
    else:
        valid_state = current is not None and current["state"] == expected
    if not valid_state or current["revision"] != row.source_gate_revision:
        reject("migration_window_gate_changed")
    return current


async def operations(db, row):
    if db.autoflush:
        await db.flush()
    return await db.scalar(_OPEN_OPERATIONS, {"window_id": row.id})


async def item(db, row: WindowSummary) -> StorageMigrationWindowItem:
    from app.services.storage_migration_finalizations import public_for_window

    count = await operations(db, row)
    return StorageMigrationWindowItem(
        window_id=row.id,
        parent_copy_id=row.parent_copy_id,
        target_location_id=row.target_location_id,
        state=row.state,
        revision=row.revision,
        source_gate_revision=row.source_gate_revision,
        source_config_revision=row.source_config_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        registered_operations=count,
        registered_access_drained=row.state in {"stopped", "closing"} and count == 0,
        finalization=await public_for_window(db, row.id),
    )


async def view(db, window_id, actor, *, api_key_id=None):
    actor, proof = await authority(db, actor, api_key_id)
    value = await item(db, await owner(db, window_id))
    _, current = await authority(db, actor, api_key_id)
    if current != proof:
        reject("migration_window_authority_changed", 403)
    return value


async def history(db, actor, *, api_key_id=None, limit=20, offset=0):
    actor, proof = await authority(db, actor, api_key_id)
    total = await db.scalar(_HISTORY_COUNT)
    rows = (
        (await db.execute(_HISTORY, {"limit": limit, "offset": offset}))
        .mappings()
        .all()
    )
    values = [await item(db, WindowSummary(**row)) for row in rows]
    _, current = await authority(db, actor, api_key_id)
    if current != proof:
        reject("migration_window_authority_changed", 403)
    return StorageMigrationWindowHistory(
        items=values, total=total, limit=limit, offset=offset
    )


async def open_window(db, actor, body, *, api_key_id=None, request=None):
    from app.services import (
        storage_migration_copies as copies,
        storage_migration_copy_target as targets,
        storage_migration_inventories as inventories,
    )

    enabled()
    actor, _ = await authority(db, actor, api_key_id, lock=True)
    await db.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(8592650719001)"))
    initial = await copies.owner(db, body.parent_copy_id)
    await targets.lock_scope(db, initial.target_location_id)
    source = await inventories.source_scope(db)
    target, _, _ = await targets.facts(db, initial.target_location_id)
    if physical_source(source) != physical_source(initial.source):
        reject("migration_window_source_changed")
    if target != initial.target:
        reject("copy_target_changed")
    if (
        endpoint_identity(target["endpoint_url"]) == physical_source(source)[0]
        and target["bucket"] in source["purpose_buckets"].values()
    ):
        reject("target_is_source")
    await storage_access.check(db, initial.target_location_id, lock=True)
    await storage_access.ensure(db, LEGACY_LOCATION_ID)
    if await db.scalar(_ACTIVE):
        reject("migration_window_exists")
    try:
        async with db.begin_nested():
            current = await storage_access.gate_facts(
                db, LEGACY_LOCATION_ID, lock="update_nowait"
            )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    if current is None or current["state"] != "active":
        reject("migration_window_gate_changed")
    parent = await copies.owner(db, body.parent_copy_id, lock=True)
    if parent.target != target or physical_source(parent.source) != physical_source(
        source
    ):
        reject("copy_context_changed")
    if parent.baseline_sha256 is None:
        reject("copy_not_prepared")
    await copies.writable(db, parent)
    await copies.settled_parent(db, parent)
    window_id = uuid.uuid4()
    opened = await db.scalar(
        _OPEN,
        {
            "window_id": window_id,
            "parent_id": parent.id,
            "actor_id": actor.id,
            "expected_gate_revision": current["revision"],
            "source": source,
            "target": target,
        },
    )
    if not opened:
        reject("migration_window_gate_changed")
    await AuditService.log(
        db,
        actor=actor,
        action="storage.migration_window_open",
        target_type="storage_migration_window",
        target_id=str(window_id),
        request=request,
        status_code=201,
        detail={"registered_access_only": True, "source_quiescence_proven": False},
    )
    return await item(db, await owner(db, window_id))


async def transition(db, window_id, actor, body, *, api_key_id=None, request=None):
    from app.services import storage_migration_inventories as inventories

    if body.action != "close":
        enabled()
    actor, _ = await authority(db, actor, api_key_id, lock=True)
    if body.action == "stop":
        source = await inventories.source_scope(db, lock=True)
    row = await owner(db, window_id, lock=True, exclusive=True)
    if row.revision != body.expected_revision:
        reject("migration_window_changed")
    if row.state == "closed":
        if body.action == "close":
            return await item(db, row)
        reject("migration_window_state_invalid")
    await gate(db, row, exclusive=True)
    if body.action == "stop":
        if row.state != "draining":
            reject("migration_window_state_invalid")
        if (
            source != row.source
            or source["location_revision"] != row.source_config_revision
        ):
            reject("migration_window_source_changed")
    failure = await db.scalar(
        _TRANSITION,
        {
            "window_id": window_id,
            "expected_revision": body.expected_revision,
            "action": body.action,
            "actor_id": actor.id,
        },
    )
    if failure is not None:
        reject(failure)
    row = await owner(db, window_id)
    await AuditService.log(
        db,
        actor=actor,
        action="storage.migration_window_" + body.action,
        target_type="storage_migration_window",
        target_id=str(row.id),
        request=request,
        status_code=200,
        detail={
            "state": row.state,
            "revision": row.revision,
            "registered_access_only": True,
            "source_quiescence_proven": False,
        },
    )
    return await item(db, row)


async def bump_finalization_revision(db, window: WindowFacts) -> WindowFacts:
    if not await db.scalar(
        _BUMP_FINALIZATION,
        {"window_id": window.id, "expected_revision": window.revision},
    ):
        reject("finalization_changed")
    return await owner(db, window.id)


async def _source_state(db, window_id, source, *, lock=False, window_revision=None):
    from app.services import storage_migration_inventories as inventories

    enabled()
    current = await inventories.source_scope(db, lock=lock)
    row = await owner(db, window_id, lock=lock)
    if row.state != "stopped":
        reject("migration_window_state_invalid")
    from app.services.storage_migration_finalizations import require_window_open

    await require_window_open(db, row.id)
    if window_revision is not None and row.revision != window_revision:
        reject("migration_window_changed")
    if (
        row.source_location_id != LEGACY_LOCATION_ID
        or physical_source(source) != physical_source(row.source)
        or physical_source(current) != physical_source(row.source)
        or source.get("location_revision") != row.source_config_revision
        or current["location_revision"] != row.source_config_revision
    ):
        reject("migration_window_source_changed")
    current_gate = await gate(db, row)
    return row, current_gate["revision"]


async def source_revision(db, window_id, source, *, lock=False):
    _, revision = await _source_state(db, window_id, source, lock=lock)
    return revision


async def check_source(db, window_id, window_revision, source, *, lock=False):
    _, revision = await _source_state(
        db, window_id, source, lock=lock, window_revision=window_revision
    )
    return revision


async def acquire_source_read(
    db, *, window_id, owner_kind, owner_id, task_id, node_id=None
):
    from app.services import (
        storage_migration_inventories as inventories,
        storage_migration_copies as copies,
    )

    if owner_kind not in {"inventory", "copy"}:
        reject("migration_window_owner_invalid")
    node = node_id or f"{socket.gethostname()}:{os.getpid()}"
    if not storage_access.SAFE_NODE.fullmatch(node):
        raise ValueError("Invalid storage operation node")
    bind = storage_access.engine(db)
    operation_id = uuid.uuid4()
    try:
        async with (
            bind.begin() as connection,
            AsyncSession(
                bind=connection, expire_on_commit=False, autoflush=False
            ) as owned,
        ):
            if owner_kind == "inventory":
                record = bound = await inventories.by_job(owned, owner_id)
            else:
                record = await copies.by_attempt(owned, owner_id)
                bound = (
                    await copies.window_copy(owned, record.copy_id) if record else None
                )
            if record is None or bound is None:
                reject("migration_window_owner_invalid")
            actor = await IdentityAccountLockData(owned).read_actor_status(
                record.requested_by
            )
            if actor is None:
                reject("migration_window_authority_changed", 403)
            _, proof = await authority(
                owned, actor, record.authority.get("api_key_id"), lock=True
            )
            if proof != record.authority.get("key_proof"):
                reject("migration_window_authority_changed", 403)
            if bound.window_id != window_id or bound.window_revision is None:
                reject("migration_window_owner_invalid")
            window, revision = await _source_state(
                owned,
                window_id,
                bound.source,
                lock=True,
                window_revision=bound.window_revision,
            )
            registered = await owned.scalar(
                _REGISTER_SOURCE,
                {
                    "window_id": window_id,
                    "window_revision": bound.window_revision,
                    "gate_revision": revision,
                    "owner_kind": owner_kind,
                    "owner_id": owner_id,
                    "task_id": task_id,
                    "actor_id": actor.id,
                    "authority": record.authority,
                    "source": bound.source,
                    "operation_id": operation_id,
                    "node": node,
                },
            )
            if not registered:
                reject("migration_window_owner_invalid")
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    permit = storage_access.Permit(bind, operation_id, LEGACY_LOCATION_ID, revision)
    try:
        from app.services.managed_original_reclaim_fence import require_source_readable

        async with AsyncSession(
            bind=bind, expire_on_commit=False, autoflush=False
        ) as guarded:
            await require_source_readable(guarded, window.source)
    except BaseException:
        await permit.__aexit__(None, None, None)
        raise
    return permit
