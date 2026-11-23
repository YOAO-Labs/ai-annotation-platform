"""Registry authorization, bounded directory cursors and node observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import socket
import uuid

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.enums import UserRole
from app.db.models.dataset_asset import StorageLocation
from app.services import filesystem_storage as fs, storage_access
from app.services import storage_registry_data as registry
from app.services.dataset_assets import (
    PURPOSE_BUCKET_ATTRIBUTES,
    validate_legacy_bindings,
)

PROBE_TTL = timedelta(minutes=5)


async def _registered_io(*args, **kwargs):
    try:
        return await storage_access.run(*args, **kwargs)
    except BaseException as exc:
        if isinstance(exc, Exception) and getattr(
            exc, "storage_operation_unsettled", False
        ):
            from app.services.content_snapshot import ContentError

            raise storage_access.mark_unsettled(
                ContentError("storage_operation_unsettled", 503)
            ) from None
        raise


def api_node_id():
    return f"api:{socket.gethostname()}"


def connector_location_id(connection_id):
    return uuid.UUID(
        hashlib.md5(f"aap:storage-connection:{connection_id}".encode()).hexdigest()
    )


async def register_connection(db, connection):
    return await db.scalar(
        select(StorageLocation).from_statement(
            text(
                "SELECT * FROM yoao_assets_storage_v1.register_connector_location(:connection)"
            )
        ),
        {"connection": connection.id},
    )


async def location_is_referenced(db, location_id):
    if db.autoflush:
        await db.flush()
    return bool(
        await db.scalar(
            select(func.yoao_assets_storage_v1.location_storage_referenced(location_id))
        )
    )


def visible_clause(actor):
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    return func.yoao_assets_storage_v1.location_visible(
        actor.id, actor.role, registry.location_row.c.id
    )


async def get_visible(db, location_id, actor, *, lock=False):
    location = await registry.get_location(
        db, location_id, actor=actor, lock="update" if lock else None
    )
    if location is None:
        raise HTTPException(404, "Storage location not found")
    return location


def require_filesystem(location):
    if location.kind != "filesystem":
        raise HTTPException(
            409, "Filesystem capability is not available for this location"
        )
    if not location.enabled:
        raise HTTPException(409, "Storage location disabled")


def require_reference_backend(location):
    if location.kind not in {"filesystem", "s3", "sftp", "agent"} or (
        location.kind in {"s3", "sftp"} and location.connection_id is None
    ):
        raise HTTPException(409, detail={"code": "reference_backend_not_supported"})
    if not location.enabled:
        raise HTTPException(409, "Storage location disabled")


async def to_out(db, location, actor):
    connection = (
        await registry.get_connection(db, location.connection_id)
        if location.connection_id
        else None
    )
    maintenance = await storage_access.state(db, location.id)
    return location_out(location, actor, connection, maintenance)


def location_out(location, actor, connection, maintenance):
    """Project an already-loaded location without queries or provider access."""
    config = location.config or {}
    reference = location.kind in {"filesystem", "agent"} or (
        location.kind == "s3" and connection is not None
    )
    if location.kind == "sftp" and connection is not None:
        from app.services.reference_storage import sftp_location_anchor
        from yoao_assets_source.reference_protocol import ReferenceError

        try:
            sftp_location_anchor(connection.config)
            reference = True
        except ReferenceError:
            pass
    actions = ["read"]
    if location.enabled and reference and maintenance["state"] == "active":
        actions.extend(["probe", "browse"])
    if actor.role == UserRole.SUPER_ADMIN and (
        location.kind in {"filesystem", "agent"} or connection is not None
    ):
        actions.append("manage_location")
    if connection is not None and (
        actor.role == UserRole.SUPER_ADMIN
        or (connection.scope != "global" and connection.created_by == actor.id)
    ):
        actions.append("manage_connection")
    return {
        "id": location.id,
        "name": connection.name if connection else location.name,
        "kind": location.kind,
        "enabled": location.enabled,
        "config_revision": location.config_revision,
        "maintenance_state": maintenance["state"],
        "maintenance_revision": maintenance["revision"],
        "cache_policy": config.get("cache_policy", "preview"),
        "original_cache_quota_bytes": config.get("original_cache_quota_bytes", 0),
        "original_cache_ttl_seconds": config.get("original_cache_ttl_seconds", 86400),
        "connection_id": location.connection_id,
        "agent_id": config.get("agent_id") if location.kind == "agent" else None,
        "site_id": config.get("site_id") if location.kind == "agent" else None,
        "root_revision": config.get("root_revision")
        if location.kind == "agent"
        else None,
        "root_handle": config.get("root_handle")
        if location.kind in {"filesystem", "agent"}
        else None,
        "relative_path": config.get("relative_path")
        if location.kind in {"filesystem", "agent"}
        else None,
        "scope": config.get("scope")
        if location.kind in {"filesystem", "agent"}
        else (connection.scope if connection else None),
        "use_user_ids": config.get("use_user_ids", [])
        if actor.role == UserRole.SUPER_ADMIN
        else [],
        "declared_capabilities": {
            "reference": reference,
            "list": True,
            "read": True,
            "range": location.kind in {"filesystem", "agent"},
            "write": False,
            "delete": False,
            "presign": False,
        },
        "allowed_actions": actions,
    }


async def validate_grants(db, user_ids):
    from app.services.identity_read_relations import account_profiles

    ids = set(user_ids)
    if not ids:
        return
    count = await db.scalar(
        select(func.count())
        .select_from(account_profiles)
        .where(
            account_profiles.c.id.in_(ids),
            account_profiles.c.is_active.is_(True),
            account_profiles.c.role.in_(
                [UserRole.SUPER_ADMIN.value, UserRole.PROJECT_ADMIN.value]
            ),
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
        "cache_policy": payload.cache_policy,
        "original_cache_quota_bytes": payload.original_cache_quota_bytes,
        "original_cache_ttl_seconds": payload.original_cache_ttl_seconds,
    }
    validate_cache_config(config)
    anchor = await run_in_threadpool(fs.capture_anchor, config)
    config.update(registration_node=api_node_id(), registration_anchor=anchor)
    return await registry.create_location(
        db, uuid.uuid4(), payload.name, "filesystem", config
    )


async def create_agent(db, payload):
    from app.services.site_agents import SiteAgentError, get_identity, validate_current

    await validate_grants(db, payload.use_user_ids)
    agent, credential = await get_identity(db, payload.agent_id, lock="share")
    try:
        validate_current(agent, credential)
    except SiteAgentError as exc:
        raise fs.FilesystemStorageError(exc.code) from None
    if {
        "root_handle": payload.root_handle,
        "root_revision": payload.root_revision,
    } not in agent.roots:
        raise fs.FilesystemStorageError("agent_root_revision_changed")
    config = {
        "agent_id": str(agent.id),
        "site_id": str(agent.site_id),
        "root_handle": payload.root_handle,
        "root_revision": payload.root_revision,
        "relative_path": fs.relative_path(payload.relative_path),
        "scope": payload.scope,
        "use_user_ids": sorted({str(value) for value in payload.use_user_ids}),
        "cache_policy": payload.cache_policy,
        "original_cache_quota_bytes": payload.original_cache_quota_bytes,
        "original_cache_ttl_seconds": payload.original_cache_ttl_seconds,
    }
    validate_cache_config(config)
    return await registry.create_location(
        db, uuid.uuid4(), payload.name, "agent", config
    )


async def update_candidate(db, location, payload):
    """Validate and normalize public edits without mutating the stored location."""
    if location.kind not in {"filesystem", "agent"}:
        if location.connection_id is None:
            raise HTTPException(409, "Platform storage bindings are immutable")
        if any(
            getattr(payload, key) is not None
            for key in ("name", "scope", "use_user_ids")
        ):
            raise HTTPException(
                409, "Manage connector identity and scope through its connection"
            )
    if location.config_revision != payload.expected_revision:
        raise HTTPException(409, detail={"code": "storage_location_revision_changed"})
    if payload.use_user_ids is not None:
        await validate_grants(db, payload.use_user_ids)
    config = dict(location.config)
    for key in (
        "cache_policy",
        "original_cache_quota_bytes",
        "original_cache_ttl_seconds",
    ):
        if getattr(payload, key) is not None:
            config[key] = getattr(payload, key)
    validate_cache_config(config)
    if payload.scope is not None:
        config["scope"] = payload.scope
    if payload.use_user_ids is not None:
        config["use_user_ids"] = sorted({str(value) for value in payload.use_user_ids})
    return {
        "name": payload.name if payload.name is not None else location.name,
        "enabled": payload.enabled if payload.enabled is not None else location.enabled,
        "config": config,
    }


async def update_filesystem(db, location, payload):
    candidate = await update_candidate(db, location, payload)
    updated = await registry.edit_location(db, location, **candidate)
    if updated is None:
        raise HTTPException(409, detail={"code": "storage_location_revision_changed"})
    return updated


def validate_cache_config(config):
    if (
        config.get("cache_policy", "preview") == "on_demand"
        and config.get("original_cache_quota_bytes", 0) <= 0
    ):
        raise HTTPException(422, detail={"code": "original_cache_quota_required"})


def probe_node_id(location, node_id):
    return (
        f"agent:{location.config['agent_id']}" if location.kind == "agent" else node_id
    )


async def node_anchor(db, location, node_id):
    if location.kind == "agent" and location.config.get("agent_anchor") is not None:
        return location.config["agent_anchor"]
    node_id = probe_node_id(location, node_id)
    if node_id == location.config.get("registration_node"):
        return location.config["registration_anchor"]
    prior = await registry.get_probe(db, location.id, node_id)
    return (prior.result or {}).get("anchor") if prior else None


async def browse(
    db,
    location,
    *,
    path,
    limit,
    cursor,
    maintenance_revision=None,
    actor_id=None,
    api_key_id=None,
    key_proof=None,
):
    if location.kind == "s3":
        return await browse_s3(
            db,
            location,
            path,
            cursor,
            limit=limit,
            maintenance_revision=maintenance_revision,
            actor_id=actor_id,
            api_key_id=api_key_id,
            key_proof=key_proof,
        )
    require_reference_backend(location)
    maintenance_revision = await storage_access.check(
        db, location.id, expected_revision=maintenance_revision
    )
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
                or data.get("maintenance_revision", 1) != maintenance_revision
                or data["path_digest"] != path_digest
                or data["node"] != node
                or (location.kind == "sftp" and data.get("actor") != str(actor_id))
            ):
                raise ValueError()
        except (JWTError, ValueError, KeyError, TypeError):
            raise HTTPException(409, "Directory cursor is invalid or stale") from None
    receipt = None
    if location.kind == "agent":
        from app.services import agent_transport

        await db.commit()
        receipt = await agent_transport.exchange(
            db,
            location,
            "browse",
            {
                "path": path,
                "expected_anchor": anchor,
                "limit": limit,
                "after": data["after"] if data else None,
                "snapshot": data["snapshot"] if data else None,
            },
            fence={},
            purpose="browse",
            node_id=node,
            expected_revision=maintenance_revision,
        )
        result = receipt.result["result"]
        names, more, snapshot = result["names"], result["has_more"], result["snapshot"]
    elif location.kind == "sftp":
        from app.services.reference_storage import configuration

        access = await configuration(db, location)
        result = await access.command(
            db,
            "browse",
            {
                "path": path,
                "limit": limit,
                "after": data["after"] if data else None,
                "snapshot": data["snapshot"] if data else None,
            },
            actor_id=actor_id,
            key_id=api_key_id,
            key_proof=key_proof,
            maintenance_revision=maintenance_revision,
            node_id=node,
        )
        names, more, snapshot = result["names"], result["has_more"], result["snapshot"]
    else:
        from functools import partial

        browse_directory = partial(fs.browse_directory, location.config)
    if location.kind == "filesystem":
        names, more, snapshot = await _registered_io(
            db,
            location.id,
            "browse",
            browse_directory,
            path,
            expected_revision=maintenance_revision,
            node_id=node,
            expected_anchor=anchor,
            limit=limit,
            after=data["after"] if data else None,
            snapshot=data["snapshot"] if data else None,
        )
    await storage_access.check(db, location.id, expected_revision=maintenance_revision)
    next_cursor = (
        jwt.encode(
            {
                "aud": "storage-directory",
                "maintenance_revision": maintenance_revision,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
                "location": str(location.id),
                "revision": location.config_revision,
                "node": node,
                "path_digest": path_digest,
                **({"actor": str(actor_id)} if location.kind == "sftp" else {}),
                "after": names[-1],
                "snapshot": snapshot,
            },
            settings.secret_key,
            algorithm="HS256",
        )
        if more
        else None
    )
    result = {
        "entries": [
            {"name": name, "relative_path": f"{path}/{name}" if path else name}
            for name in names
        ],
        "next_cursor": next_cursor,
    }
    if receipt is not None:
        result["_agent_command_id"] = receipt.id
    return result


async def perform_probe(
    db,
    location,
    node_id,
    sample_file,
    *,
    maintenance_revision=None,
    actor_id=None,
    api_key_id=None,
    key_proof=None,
    source_request=None,
):
    require_reference_backend(location)
    revision = await storage_access.check(
        db, location.id, expected_revision=maintenance_revision
    )
    if location.kind == "agent":
        from app.services import agent_transport

        anchor = await node_anchor(db, location, node_id)
        await db.commit()
        receipt = await agent_transport.exchange(
            db,
            location,
            "probe",
            {"sample_file": sample_file, "expected_anchor": anchor},
            fence={},
            purpose="probe",
            node_id=probe_node_id(location, node_id),
            expected_revision=revision,
        )
        result = {**receipt.result["result"], "_agent_command_id": receipt.id}
    elif location.kind in {"s3", "sftp"}:
        from app.services.reference_storage import configuration

        access = await configuration(db, location)
        result = await access.command(
            db,
            "probe",
            source_request if source_request is not None else {"path": sample_file},
            actor_id=actor_id,
            key_id=api_key_id,
            key_proof=key_proof,
            maintenance_revision=revision,
            node_id=node_id,
            timeout=3,
        )
    else:
        anchor = await node_anchor(db, location, node_id)
        try:
            result = await _registered_io(
                db,
                location.id,
                "probe",
                fs.probe_directory,
                location.config,
                sample_file,
                expected_anchor=anchor,
                expected_revision=revision,
                node_id=node_id,
                timeout=3,
            )
        except TimeoutError:
            result = {"ok": False, "code": "node_timeout", "capabilities": {}}
        result["anchor"] = anchor or result.get("anchor")
    await storage_access.check(db, location.id, expected_revision=revision)
    return {**result, "maintenance_revision": revision}


async def browse_s3(
    db,
    location,
    path,
    cursor,
    *,
    limit,
    maintenance_revision,
    actor_id,
    api_key_id,
    key_proof,
):
    from app.services.reference_storage import configuration

    require_reference_backend(location)
    maintenance_revision = await storage_access.check(
        db, location.id, expected_revision=maintenance_revision
    )
    access = await configuration(db, location)
    token = None
    if cursor:
        try:
            data = jwt.decode(
                cursor,
                settings.secret_key,
                algorithms=["HS256"],
                audience="s3-directory",
            )
            if (
                data["location"] != str(location.id)
                or data["revision"] != location.config_revision
                or data.get("maintenance_revision", 1) != maintenance_revision
                or data["path"] != path
                or data.get("actor") != str(actor_id)
            ):
                raise ValueError()
            token = data["token"]
        except (JWTError, ValueError, KeyError, TypeError):
            raise HTTPException(409, "Directory cursor is invalid or stale") from None
    page = await access.command(
        db,
        "browse",
        {"path": path, "cursor": token, "limit": limit},
        actor_id=actor_id,
        key_id=api_key_id,
        key_proof=key_proof,
        maintenance_revision=maintenance_revision,
        node_id=api_node_id(),
    )
    await storage_access.check(db, location.id, expected_revision=maintenance_revision)
    next_cursor = (
        jwt.encode(
            {
                "aud": "s3-directory",
                "maintenance_revision": maintenance_revision,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
                "location": str(location.id),
                "revision": location.config_revision,
                "path": path,
                "actor": str(actor_id),
                "token": page["next_cursor"],
            },
            settings.secret_key,
            algorithm="HS256",
        )
        if page["next_cursor"]
        else None
    )
    from app.services.source_readers.s3 import directory_prefix

    return {
        "entries": [
            {"name": name, "relative_path": directory_prefix("", path) + name}
            for name in page["directories"]
        ],
        "next_cursor": next_cursor,
    }


async def store_probe(db, location, node_id, result, observed_at):
    result = dict(result)
    receipt_id = result.pop("_agent_command_id", None)
    node_id = probe_node_id(location, node_id)
    if location.kind == "agent" and result.get("ok"):
        anchor = location.config.get("agent_anchor")
        if anchor is not None and anchor != result.get("anchor"):
            result = {
                **result,
                "ok": False,
                "code": "root_identity_changed",
                "capabilities": {},
            }
    await storage_access.check(
        db,
        location.id,
        expected_revision=result.get("maintenance_revision", 1),
        lock=True,
    )
    previous = await registry.get_probe(db, location.id, node_id)
    anchor = (previous.result or {}).get("anchor") if previous else None
    if anchor is not None:
        result = dict(result)
        if result.get("anchor") not in (None, anchor):
            result.update(ok=False, code="root_identity_changed", capabilities={})
        result["anchor"] = anchor
    await registry.record_probe(db, location, node_id, result, observed_at)
    if receipt_id is not None:
        from app.services.agent_transport import acknowledge, get_receipt

        await acknowledge(db, await get_receipt(db, receipt_id), accepted=True)


def probe_out(probe, location, *, measured_at=None, include_disabled=False):
    result = probe.result
    expires_at = probe.observed_at + PROBE_TTL
    stale = (
        probe.config_revision != location.config_revision
        or expires_at <= (measured_at or datetime.now(timezone.utc))
        or (not include_disabled and not location.enabled)
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
    require_reference_backend(location)
    revision = await storage_access.check(db, location.id, lock=True)
    if not node_id.startswith("worker:"):
        raise HTTPException(409, "Scanning requires a verified worker node")
    probe = await registry.get_probe(db, location.id, probe_node_id(location, node_id))
    if (
        probe is None
        or (probe.result or {}).get("maintenance_revision", 1) != revision
        or not probe_out(probe, location)["ok"]
        or probe_out(probe, location)["capabilities"].get("list") != "verified"
    ):
        raise HTTPException(
            409, "Selected scan node has no current verified capability"
        )


async def binding_rows(db):
    findings = await validate_legacy_bindings(db)
    bindings = {
        row.purpose: (row, row.config_revision)
        for row in (
            await db.execute(
                select(
                    registry.binding_row, registry.location_row.c.config_revision
                ).outerjoin(
                    registry.location_row,
                    registry.location_row.c.id == registry.binding_row.c.location_id,
                )
            )
        ).mappings()
    }
    result = []
    for purpose in PURPOSE_BUCKET_ATTRIBUTES:
        row, revision = bindings.get(purpose, (None, None))
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
                "location_config_revision": revision,
                "bucket": row.bucket if row else None,
                "state": "missing"
                if row is None
                else ("mismatch" if mismatch else "configured"),
                "read_policy": "consumer_owned_compatibility",
                "write_policy": "current_binding"
                if purpose
                in {
                    "avatars",
                    "bug-reports",
                    "audit-archive",
                    "export",
                    "import",
                    "datasets",
                    "media-cache",
                    "annotations",
                    "training",
                }
                else "deployment_mapping",
                "change_policy": "reviewed_avatar_switch"
                if purpose == "avatars"
                else (
                    "reviewed_bug_attachment_switch"
                    if purpose == "bug-reports"
                    else "reviewed_audit_archive_switch"
                    if purpose == "audit-archive"
                    else "reviewed_export_switch"
                    if purpose == "export"
                    else "reviewed_import_switch"
                    if purpose == "import"
                    else "reviewed_dataset_switch"
                    if purpose == "datasets"
                    else "reviewed_media_cache_switch"
                    if purpose == "media-cache"
                    else "reviewed_annotations_switch"
                    if purpose == "annotations"
                    else "reviewed_training_switch"
                    if purpose == "training"
                    else "legacy_mapping_locked"
                ),
            }
        )
    return result
