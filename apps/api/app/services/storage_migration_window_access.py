"""Read and validate saved migration-window and legacy source identities."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.services import storage_access, storage_registry_data
from app.services.dataset_assets import LEGACY_LOCATION_ID, PURPOSE_BUCKET_ATTRIBUTES
from app.services.storage import storage_service
from app.services.storage_migration_guards import reject, require_window_open
from app.services.storage_migration_inventory import PURPOSES
from app.services.storage_migration_observations import endpoint_identity

_WINDOW = text("SELECT * FROM yoao_assets_v1.read_migration_window(:window_id, :lock)")


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


def enabled():
    if not settings.storage_maintenance_protocol_enabled:
        reject("storage_maintenance_protocol_unqualified")


def physical_source(source):
    endpoint = endpoint_identity(source.get("endpoint"))
    buckets = source.get("purpose_buckets")
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


async def source_scope(db, *, lock=False):
    location = await storage_registry_data.get_location(
        db, LEGACY_LOCATION_ID, lock="share" if lock else None
    )
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    if (
        not location
        or not location.enabled
        or location.kind != "s3"
        or location.connection_id is not None
    ):
        reject("inventory_source_unavailable")
    configured = location.config.get("endpoint")
    if isinstance(configured, str) and "://" not in configured:
        configured = (
            f"{'https' if location.config.get('use_ssl') else 'http'}://{configured}"
        )
    if endpoint_identity(configured) != endpoint_identity(
        endpoint
    ) or endpoint_identity(
        storage_service.client.meta.endpoint_url
    ) != endpoint_identity(endpoint):
        reject("inventory_source_changed")
    return {
        "endpoint": endpoint,
        "location_revision": location.config_revision,
        "purpose_buckets": {
            purpose: getattr(storage_service, attribute)
            for purpose, attribute in PURPOSE_BUCKET_ATTRIBUTES.items()
        },
    }


async def _source_state(db, window_id, source, *, lock=False, window_revision=None):
    enabled()
    current = await source_scope(db, lock=lock)
    row = await owner(db, window_id, lock=lock)
    if row.state != "stopped":
        reject("migration_window_state_invalid")
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
