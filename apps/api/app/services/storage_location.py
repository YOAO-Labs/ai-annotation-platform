"""Registry authorization, bounded directory cursors and node observations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import socket
import uuid

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.enums import UserRole
from app.db.models.dataset_asset import (
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
    StorageLocationProbe,
)
from app.db.models.storage_connection import StorageConnection
from app.db.models.user import User
from app.services import filesystem_storage as fs
from app.services.dataset_assets import (
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)
from app.services.storage_connection import can_use_connection

PROBE_TTL = timedelta(minutes=5)


def api_node_id():
    return f"api:{socket.gethostname()}"


def connector_location_id(connection_id):
    return uuid.UUID(
        hashlib.md5(f"aap:storage-connection:{connection_id}".encode()).hexdigest()
    )


async def register_connection(db, connection):
    location = StorageLocation(
        id=connector_location_id(connection.id),
        name=f"connector-{connection.id}",
        kind=connection.kind,
        config={},
        config_revision=1,
        enabled=True,
        connection_id=connection.id,
    )
    db.add(location)
    await db.flush()
    return location


async def location_is_referenced(db, location_id):
    return bool(
        await db.scalar(
            select(DatasetAssetLocation.asset_id)
            .where(DatasetAssetLocation.location_id == location_id)
            .limit(1)
        )
        or await db.scalar(
            select(StorageBinding.purpose)
            .where(StorageBinding.location_id == location_id)
            .limit(1)
        )
    )


def visible_clause(actor):
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    return or_(
        StorageLocation.connection_id.in_(
            select(StorageConnection.id).where(
                or_(
                    StorageConnection.scope == "global",
                    StorageConnection.created_by == actor.id,
                )
            )
        ),
        (StorageLocation.kind == "filesystem")
        & or_(
            StorageLocation.config["scope"].astext == "global",
            StorageLocation.config["use_user_ids"].contains([str(actor.id)]),
        ),
    )


async def get_visible(db, location_id, actor, *, lock=False):
    stmt = (
        select(StorageLocation)
        .where(StorageLocation.id == location_id, visible_clause(actor))
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update()
    location = await db.scalar(stmt)
    if location is None:
        raise HTTPException(404, "Storage location not found")
    if location.connection_id is not None:
        connection = await db.get(
            StorageConnection, location.connection_id, populate_existing=True
        )
        if connection is None or not can_use_connection(actor, connection):
            raise HTTPException(404, "Storage location not found")
    return location


def require_filesystem(location):
    if location.kind != "filesystem":
        raise HTTPException(
            409, "Filesystem capability is not available for this location"
        )
    if not location.enabled:
        raise HTTPException(409, "Storage location disabled")


async def to_out(db, location, actor):
    connection = (
        await db.get(StorageConnection, location.connection_id)
        if location.connection_id
        else None
    )
    config = location.config or {}
    return {
        "id": location.id,
        "name": connection.name if connection else location.name,
        "kind": location.kind,
        "enabled": location.enabled,
        "config_revision": location.config_revision,
        "connection_id": location.connection_id,
        "root_handle": config.get("root_handle")
        if location.kind == "filesystem"
        else None,
        "relative_path": config.get("relative_path")
        if location.kind == "filesystem"
        else None,
        "scope": config.get("scope")
        if location.kind == "filesystem"
        else (connection.scope if connection else None),
        "use_user_ids": config.get("use_user_ids", [])
        if actor.role == UserRole.SUPER_ADMIN
        else [],
        "declared_capabilities": {
            "list": True,
            "read": True,
            "range": location.kind == "filesystem",
            "write": False,
            "delete": False,
            "presign": False,
        },
    }


async def validate_grants(db, user_ids):
    ids = set(user_ids)
    if not ids:
        return
    count = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.id.in_(ids),
            User.is_active.is_(True),
            User.role.in_([UserRole.SUPER_ADMIN.value, UserRole.PROJECT_ADMIN.value]),
        )
    )
    if count != len(ids):
        raise HTTPException(422, "Location users must be active platform managers")


async def create_filesystem(db, payload):
    await validate_grants(db, payload.use_user_ids)
    config = {
        "root_handle": payload.root_handle,
        "relative_path": fs.relative_path(payload.relative_path),
        "scope": payload.scope,
        "use_user_ids": sorted({str(value) for value in payload.use_user_ids}),
    }
    anchor = await run_in_threadpool(fs.capture_anchor, config)
    config.update(registration_node=api_node_id(), registration_anchor=anchor)
    location = StorageLocation(
        id=uuid.uuid4(),
        name=payload.name,
        kind="filesystem",
        config_revision=1,
        config=config,
        enabled=True,
    )
    db.add(location)
    await db.flush()
    return location


async def update_filesystem(db, location, payload):
    if location.kind != "filesystem":
        raise HTTPException(409, "Manage connector locations through their connection")
    if location.config_revision != payload.expected_revision:
        raise HTTPException(409, "Storage configuration revision changed")
    if payload.use_user_ids is not None:
        await validate_grants(db, payload.use_user_ids)
    config = dict(location.config)
    if payload.scope is not None:
        config["scope"] = payload.scope
    if payload.use_user_ids is not None:
        config["use_user_ids"] = sorted({str(value) for value in payload.use_user_ids})
    if payload.name is not None:
        location.name = payload.name
    if payload.enabled is not None:
        location.enabled = payload.enabled
    location.config = config
    location.config_revision += 1
    await db.flush()


async def node_anchor(db, location, node_id):
    if node_id == location.config.get("registration_node"):
        return location.config["registration_anchor"]
    prior = await db.get(StorageLocationProbe, (location.id, node_id))
    return (prior.result or {}).get("anchor") if prior else None


async def browse(db, location, *, path, limit, cursor):
    require_filesystem(location)
    path = fs.relative_path(path)
    path_digest = hashlib.sha256(path.encode()).hexdigest()
    node = api_node_id()
    anchor = await node_anchor(db, location, node)
    if anchor is None:
        raise HTTPException(409, "This API node must probe the location first")
    data = None
    if cursor:
        try:
            data = jwt.decode(
                cursor,
                settings.secret_key,
                algorithms=["HS256"],
                audience="storage-directory",
            )
            if (
                data["location"] != str(location.id)
                or data["revision"] != location.config_revision
                or data["path_digest"] != path_digest
                or data["node"] != node
            ):
                raise ValueError()
        except (JWTError, ValueError, KeyError, TypeError):
            raise HTTPException(409, "Directory cursor is invalid or stale") from None
    names, more, snapshot = await run_in_threadpool(
        fs.browse_directory,
        location.config,
        path,
        expected_anchor=anchor,
        limit=limit,
        after=data["after"] if data else None,
        snapshot=data["snapshot"] if data else None,
    )
    next_cursor = (
        jwt.encode(
            {
                "aud": "storage-directory",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
                "location": str(location.id),
                "revision": location.config_revision,
                "node": node,
                "path_digest": path_digest,
                "after": names[-1],
                "snapshot": snapshot,
            },
            settings.secret_key,
            algorithm="HS256",
        )
        if more
        else None
    )
    return {
        "entries": [
            {"name": name, "relative_path": f"{path}/{name}" if path else name}
            for name in names
        ],
        "next_cursor": next_cursor,
    }


async def perform_probe(db, location, node_id, sample_file):
    require_filesystem(location)
    anchor = await node_anchor(db, location, node_id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                fs.probe_directory, location.config, sample_file, expected_anchor=anchor
            ),
            timeout=3,
        )
    except TimeoutError:
        result = {"ok": False, "code": "node_timeout", "capabilities": {}}
    result["anchor"] = anchor or result.get("anchor")
    return result


async def store_probe(db, location, node_id, result, observed_at):
    previous = await db.get(
        StorageLocationProbe, (location.id, node_id), populate_existing=True
    )
    anchor = (previous.result or {}).get("anchor") if previous else None
    if anchor is not None:
        result = dict(result)
        if result.get("anchor") not in (None, anchor):
            result.update(ok=False, code="root_identity_changed", capabilities={})
        result["anchor"] = anchor
    stmt = insert(StorageLocationProbe).values(
        location_id=location.id,
        node_id=node_id,
        config_revision=location.config_revision,
        observed_at=observed_at,
        result=result,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[
                StorageLocationProbe.location_id,
                StorageLocationProbe.node_id,
            ],
            set_={
                "config_revision": stmt.excluded.config_revision,
                "observed_at": stmt.excluded.observed_at,
                "result": stmt.excluded.result,
            },
            where=StorageLocationProbe.observed_at <= observed_at,
        )
    )


def probe_out(probe, location):
    result = probe.result
    expires_at = probe.observed_at + PROBE_TTL
    stale = (
        probe.config_revision != location.config_revision
        or expires_at <= datetime.now(timezone.utc)
        or not location.enabled
    )
    capabilities = result.get("capabilities", {})
    if stale:
        capabilities = {
            key: "unknown" if value != "unsupported" else value
            for key, value in capabilities.items()
        }
    return {
        "node_id": probe.node_id,
        "config_revision": probe.config_revision,
        "observed_at": probe.observed_at,
        "expires_at": expires_at,
        "ok": not stale and result.get("ok", False),
        "code": "stale" if stale else result["code"],
        "capabilities": capabilities,
    }


async def require_scan_node(db, location, node_id):
    require_filesystem(location)
    if not node_id.startswith("worker:"):
        raise HTTPException(409, "Scanning requires a verified worker node")
    probe = await db.get(StorageLocationProbe, (location.id, node_id))
    if (
        probe is None
        or not probe_out(probe, location)["ok"]
        or probe_out(probe, location)["capabilities"].get("list") != "verified"
    ):
        raise HTTPException(
            409, "Selected scan node has no current verified capability"
        )


async def binding_rows(db):
    findings = await validate_legacy_bindings(db)
    bindings = {row.purpose: row for row in await db.scalars(select(StorageBinding))}
    result = []
    for purpose in PURPOSE_BUCKET_ATTRIBUTES:
        row = bindings.get(purpose)
        mismatch = row is not None and (
            f"binding_mismatch:{purpose}" in findings
            or "platform_location_mismatch" in findings
            or "missing_platform_location" in findings
        )
        result.append(
            {
                "purpose": purpose,
                "location_id": row.location_id if row else None,
                "generation": row.generation if row else None,
                "bucket": row.bucket if row else None,
                "state": "missing"
                if row is None
                else ("mismatch" if mismatch else "configured"),
            }
        )
    return result
