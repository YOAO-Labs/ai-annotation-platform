"""Fence registered access while admitting only bound migration source readers."""

import json
import os
import socket
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.storage_migration_copy import (
    StorageMigrationCopy as Copy,
    StorageMigrationCopyAttempt as Attempt,
)
from app.db.models.storage_migration_inventory import (
    StorageMigrationInventory as Inventory,
)
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.db.models.user import User
from app.schemas.storage_migration_window import (
    StorageMigrationWindowHistory,
    StorageMigrationWindowItem,
)
from app.services import storage_access, storage_maintenance
from app.services.audit import AuditService
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage_migration_observations import endpoint_identity


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


async def owner(db, window_id, *, lock=False, exclusive=False):
    query = (
        select(Window)
        .where(Window.id == window_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(read=not exclusive, nowait=True)
    try:
        row = await db.scalar(query)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    if row is None:
        raise HTTPException(404, "Migration window not found")
    return row


async def gate(db, row, *, exclusive=False):
    query = (
        select(Gate)
        .where(Gate.location_id == row.source_location_id)
        .with_for_update(read=not exclusive, nowait=True)
        .execution_options(populate_existing=True)
    )
    try:
        current = await db.scalar(query)
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    expected = "draining" if row.state == "draining" else "stopped"
    if row.state == "closing":
        valid_state = current is not None and current.state in {"draining", "stopped"}
    else:
        valid_state = current is not None and current.state == expected
    if not valid_state or current.revision != row.source_gate_revision:
        reject("migration_window_gate_changed")
    return current


async def operations(db, row):
    return int(
        await db.scalar(
            select(func.count())
            .select_from(StorageOperation)
            .where(StorageOperation.location_id == row.source_location_id)
        )
    )


async def item(db, row):
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
    total = await db.scalar(select(func.count()).select_from(Window))
    rows = list(
        await db.scalars(
            select(Window)
            .order_by(Window.created_at.desc(), Window.id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(populate_existing=True)
        )
    )
    values = [await item(db, row) for row in rows]
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
    await db.execute(text("SELECT pg_advisory_xact_lock(8592650719001)"))
    initial = await copies.owner(db, body.parent_copy_id)
    locations = sorted({LEGACY_LOCATION_ID, initial.target_location_id})
    connection_ids = select(StorageLocation.connection_id).where(
        StorageLocation.id.in_(locations), StorageLocation.connection_id.is_not(None)
    )
    await db.execute(
        select(StorageConnection.id)
        .where(StorageConnection.id.in_(connection_ids))
        .order_by(StorageConnection.id)
        .with_for_update(read=True)
    )
    await db.execute(
        select(StorageLocation.id)
        .where(StorageLocation.id.in_(locations))
        .order_by(StorageLocation.id)
        .with_for_update(read=True)
    )
    source = await inventories.source_scope(db)
    target, _, _, _ = await targets.resolve(db, initial.target_location_id)
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
    if await db.scalar(
        select(Window.id).where(
            Window.source_location_id == LEGACY_LOCATION_ID, Window.state != "closed"
        )
    ):
        reject("migration_window_exists")
    current = await storage_maintenance.lock_gate(db, LEGACY_LOCATION_ID)
    if current.state != "active":
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
    stamp = await db.scalar(select(func.statement_timestamp()))
    current.state, current.revision, current.changed_at, current.changed_by = (
        "draining",
        current.revision + 1,
        stamp,
        actor.id,
    )
    row = Window(
        id=uuid.uuid4(),
        source_location_id=LEGACY_LOCATION_ID,
        source=json.loads(json.dumps(source)),
        source_config_revision=source["location_revision"],
        parent_copy_id=parent.id,
        target_location_id=parent.target_location_id,
        target=json.loads(json.dumps(target)),
        state="draining",
        revision=1,
        source_gate_revision=current.revision,
        created_by=actor.id,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    await db.flush()
    await AuditService.log(
        db,
        actor=actor,
        action="storage.migration_window_open",
        target_type="storage_migration_window",
        target_id=str(row.id),
        request=request,
        status_code=201,
        detail={"registered_access_only": True, "source_quiescence_proven": False},
    )
    return await item(db, row)


async def transition(db, window_id, actor, body, *, api_key_id=None, request=None):
    from app.services import storage_migration_inventories as inventories

    if body.action != "close":
        enabled()
    actor, _ = await authority(db, actor, api_key_id, lock=True)
    # Keep source metadata before window/Gate locks, as the reader admission does.
    if body.action == "stop":
        source = await inventories.source_scope(db, lock=True)
    row = await owner(db, window_id, lock=True, exclusive=True)
    if row.revision != body.expected_revision:
        reject("migration_window_changed")
    if row.state == "closed":
        if body.action == "close":
            return await item(db, row)
        reject("migration_window_state_invalid")
    current = await gate(db, row, exclusive=True)
    if body.action == "close":
        from app.services.managed_original_reclaim_fence import require_resumable

        await require_resumable(db, row.source_location_id)
    count = await operations(db, row)
    stamp = await db.scalar(select(func.statement_timestamp()))
    if body.action == "stop":
        if row.state != "draining":
            reject("migration_window_state_invalid")
        if (
            source != row.source
            or source["location_revision"] != row.source_config_revision
        ):
            reject("migration_window_source_changed")
        if count:
            reject("migration_window_unsettled")
        row.state, current.state = "stopped", "stopped"
        current.revision += 1
        current.changed_at, current.changed_by = stamp, actor.id
        row.source_gate_revision = current.revision
    else:
        row.state = "closing"
        if not count:
            row.state, current.state = "closed", "active"
            current.revision += 1
            current.changed_at, current.changed_by = stamp, actor.id
            row.source_gate_revision = current.revision
    row.revision += 1
    row.updated_at = stamp
    await db.flush()
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


async def _source_state(db, window_id, source, *, lock=False, window_revision=None):
    from app.services import storage_migration_inventories as inventories

    enabled()
    current = await inventories.source_scope(db, lock=lock)
    row = await owner(db, window_id, lock=lock)
    if row.state != "stopped":
        reject("migration_window_state_invalid")
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
    return row, current_gate.revision


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
                record = await owned.get(Inventory, owner_id, populate_existing=True)
                copy = None
            else:
                record = await owned.get(Attempt, owner_id, populate_existing=True)
                copy = (
                    await owned.get(Copy, record.copy_id, populate_existing=True)
                    if record is not None
                    else None
                )
            if record is None:
                reject("migration_window_owner_invalid")
            actor = await owned.get(User, record.requested_by, populate_existing=True)
            if actor is None:
                reject("migration_window_authority_changed", 403)
            _, proof = await authority(
                owned, actor, record.authority.get("api_key_id"), lock=True
            )
            if proof != record.authority.get("key_proof"):
                reject("migration_window_authority_changed", 403)
            bound = record if copy is None else copy
            if bound.window_id != window_id or bound.window_revision is None:
                reject("migration_window_owner_invalid")
            window, revision = await _source_state(
                owned,
                window_id,
                bound.source,
                lock=True,
                window_revision=bound.window_revision,
            )
            model = Inventory if owner_kind == "inventory" else Attempt
            record = await owned.scalar(
                select(model)
                .where(
                    (model.async_job_id if owner_kind == "inventory" else model.id)
                    == owner_id
                )
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            job = await owned.scalar(
                select(AsyncJob)
                .where(AsyncJob.id == record.async_job_id)
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
            if (
                record.status != "running"
                or record.task_id != task_id
                or record.cancel_requested_at is not None
                or job is None
                or job.payload.get("cancel_requested")
            ):
                reject("migration_window_owner_invalid")
            if owner_kind == "inventory":
                if (
                    not inventories.matches(record, job)
                    or record.operation_id is not None
                    or record.source_location_id != LEGACY_LOCATION_ID
                    or record.maintenance_revision != revision
                ):
                    reject("migration_window_owner_invalid")
                purpose = inventories.KIND
            else:
                copy = await owned.get(
                    Copy,
                    record.copy_id,
                    populate_existing=True,
                    with_for_update={"read": True, "nowait": True},
                )
                if (
                    not copies.matches(copy, record, job)
                    or record.source_operation_id is not None
                    or copy.parent_copy_id != window.parent_copy_id
                    or copy.target_location_id != window.target_location_id
                    or copy.target != window.target
                    or record.scope.get("window_id") != str(window_id)
                    or record.scope.get("window_revision") != copy.window_revision
                    or record.scope.get("maintenance", {}).get(str(LEGACY_LOCATION_ID))
                    != revision
                ):
                    reject("migration_window_owner_invalid")
                purpose = copies.KIND
            await owned.execute(
                insert(StorageOperation).values(
                    id=operation_id,
                    location_id=LEGACY_LOCATION_ID,
                    revision=revision,
                    purpose=purpose,
                    node_id=node,
                )
            )
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("storage_maintenance_busy")
        raise
    return storage_access.Permit(bind, operation_id, LEGACY_LOCATION_ID, revision)
