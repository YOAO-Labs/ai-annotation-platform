"""Trusted publication control and exact-version lookup; callers own transactions.

Native observations and complete checkpoint materialization belong outside these
short database transactions. A publication never releases the Journal claim.
"""

from copy import deepcopy
from datetime import datetime
from functools import wraps
from typing import Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication as Publication,
    StorageMigrationReadRoute as Route,
)
from app.services import (
    storage_access,
    storage_migration_copy as core,
    storage_migration_copy_target as targets,
    storage_migration_finalizations as finalizations,
    storage_registry_data,
)
from app.services.original_read_plan import ReadRoute
from app.services.content_snapshot import ContentError
from app.services.storage_migration_read_evidence import mapping_digest

SCHEMA = "aap.storage-migration-read-mapping/v1"
TOKEN_SCHEMA = "aap.storage-migration-read-publication-token/v1"
ACTIVE = ("active", "withdrawing")
ANY_VERSION = object()
ENTRY_FIELDS = {
    "source_object_id",
    "source_endpoint_url",
    "source_bucket",
    "source_object_key",
    "source_version_id",
    "sha256",
    "size_bytes",
    "source_etag",
    "is_latest",
    "target_object_key",
    "target_version_id",
    "target_etag",
    "target_content_sha256",
    "creation_nonce",
    "creation_plan_uuid",
}
_RENAMES = {
    "source_endpoint_url": "endpoint_url",
    "source_bucket": "bucket",
    "source_object_key": "object_key",
    "target_object_key": "target_key",
    "creation_nonce": "nonce",
}


class PublicationItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    publication_id: uuid.UUID
    finalization_id: uuid.UUID
    window_id: uuid.UUID
    window_revision: int
    copy_id: uuid.UUID
    state: Literal["active", "withdrawing", "withdrawn"]
    revision: int
    finalization_revision: int
    mapping_sha256: str
    route_count: int
    created_at: datetime
    updated_at: datetime
    withdrawing_at: datetime | None
    withdrawn_at: datetime | None
    migration_ready: Literal[False] = False


def reject(code, status=409):
    finalizations.reject(code, status)


def _read_boundary(call):
    @wraps(call)
    async def read(db, *args, **kwargs):
        try:
            with db.no_autoflush:
                return await call(db, *args, **kwargs)
        except HTTPException as exc:
            code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
            raise ContentError(
                code if isinstance(code, str) else "read_publication_changed",
                exc.status_code,
            ) from None
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                raise ContentError("read_publication_busy", 409) from None
            raise

    return read


def item(row):
    return PublicationItem(
        publication_id=row.id,
        finalization_id=row.id,
        window_id=row.window_id,
        window_revision=row.window_revision,
        copy_id=row.copy_id,
        state=row.state,
        revision=row.revision,
        finalization_revision=row.finalization_revision,
        mapping_sha256=row.mapping_sha256,
        route_count=row.route_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        withdrawing_at=row.withdrawing_at,
        withdrawn_at=row.withdrawn_at,
    )


async def _owner(db, publication_id, *, lock=False):
    query = select(Publication).where(Publication.id == publication_id)
    if lock:
        query = query.with_for_update(nowait=True)
    try:
        return await db.scalar(query.execution_options(populate_existing=True))
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            reject("read_publication_busy")
        raise


def _cas(row, revision):
    if type(revision) is not int or row.revision != revision:
        reject("read_publication_changed")


def purpose(publication_id):
    return "migration_read_" + publication_id.hex


async def require_unpublished(db, finalization_id):
    if await db.scalar(
        select(Publication.id).where(
            Publication.id == finalization_id, Publication.state.in_(ACTIVE)
        )
    ):
        reject("read_publication_active")


async def frozen_copy(db, copy_id):
    if db.autoflush:
        await db.flush()
    return await db.scalar(
        text("SELECT yoao_assets_v1.migration_read_publication_holds_copy(:id)"),
        {"id": copy_id},
    )


async def _prepared(db, actor_id, claim, expected_revision, native):
    claim = await finalizations._claim(db, actor_id, claim)
    window = await finalizations._window(
        db, uuid.UUID(claim["window_id"]), context=True
    )
    initial = await finalizations._existing(db, claim, lock=False)
    if initial is None or initial.copy_id is None:
        reject("finalization_unknown")
    copy = await finalizations._copy(db, initial.copy_id, private=True)
    row = await finalizations._existing(db, claim)
    finalizations._cas(row, expected_revision)
    finalizations._bound(row, window)
    native = finalizations._native(native, claim, window.source, row.native)
    if row.state != "sealed" or row.evidence is None:
        reject("read_publication_seal_required")
    if any(
        native["counts"].get(actual) != row.evidence["counts"][expected]
        for actual, expected in (
            ("buckets", "source_buckets"),
            ("versions", "current_objects"),
            ("delete_markers", "current_delete_markers"),
        )
    ):
        reject("finalization_native_changed")
    attempt = await finalizations._verified(db, row, window, copy)
    if row.token != finalizations._token(copy, attempt, window):
        reject("finalization_checkpoint_changed")
    target, revisions, _ = await targets.facts(db, row.target_location_id)
    if target != row.target:
        reject("read_publication_target_changed")
    existing = await _owner(db, row.id, lock=True)
    if existing is not None and existing.state != "active":
        reject("read_publication_withdrawn")
    other = await db.scalar(
        select(Publication.id)
        .where(
            Publication.source_location_id == row.source_location_id,
            Publication.state.in_(ACTIVE),
            Publication.id != row.id,
        )
        .limit(1)
    )
    if other:
        reject("read_publication_active")
    token = {
        "schema": TOKEN_SCHEMA,
        "finalization_id": str(row.id),
        "finalization_revision": row.revision,
        "finalization_token": deepcopy(row.token),
        "target_revisions": revisions,
    }
    return row, copy, native, token, existing


async def preflight_publish(
    db, actor_id, claim, expected_finalization_revision, native
):
    row, copy, _, token, _ = await _prepared(
        db, actor_id, claim, expected_finalization_revision, native
    )
    return {
        "item": finalizations.item(row),
        "values": await finalizations.copies.input_values(db, copy),
        "token": token,
        "evidence": deepcopy(row.evidence),
    }


def _text(value, maximum, *, empty=False):
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value.encode()) <= maximum
        and "\x00" not in value
    )


def _mapping(mapping, row):
    valid = (
        isinstance(mapping, dict)
        and set(mapping) == {"schema", "evidence", "entries", "mapping_sha256"}
        and mapping.get("schema") == SCHEMA
        and mapping.get("evidence") == row.evidence
        and isinstance(mapping.get("entries"), list)
        and len(mapping["entries"]) <= 100000
        and finalizations._sha(mapping.get("mapping_sha256"))
    )
    if not valid:
        reject("read_publication_invalid_mapping")
    previous = ""
    for entry in mapping["entries"]:
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            reject("read_publication_invalid_mapping")
        source = {
            "endpoint_url": entry["source_endpoint_url"],
            "bucket": entry["source_bucket"],
            "key": entry["source_object_key"],
            "version_id": entry["source_version_id"],
        }
        if not (
            finalizations._sha(entry["source_object_id"])
            and entry["source_object_id"] > previous
            and entry["source_object_id"] == core.digest(source)
            and source["endpoint_url"] == row.source["endpoint"]
            and source["bucket"] in row.source["purpose_buckets"].values()
            and _text(source["key"], 1024)
            and (source["version_id"] is None or _text(source["version_id"], 1024))
            and finalizations._sha(entry["sha256"])
            and type(entry["size_bytes"]) is int
            and 0 <= entry["size_bytes"] < 2**63
            and _text(entry["source_etag"], 1024)
            and type(entry["is_latest"]) is bool
            and _text(entry["target_object_key"], 1024)
            and entry["target_object_key"].startswith(row.target["key_prefix"])
            and _text(entry["target_version_id"], 1024)
            and entry["target_version_id"] != "null"
            and _text(entry["target_etag"], 1024)
            and finalizations._sha(entry["target_content_sha256"])
            and _text(entry["creation_nonce"], 1024)
            and finalizations._uuid(entry["creation_plan_uuid"])
        ):
            reject("read_publication_invalid_mapping")
        previous = entry["source_object_id"]
    if (
        mapping_digest(mapping["entries"]) != mapping["mapping_sha256"]
        or len(mapping["entries"]) != row.evidence["counts"]["copied_objects"]
        or sum(entry["size_bytes"] for entry in mapping["entries"])
        != row.evidence["counts"]["verified_target_bytes"]
    ):
        reject("read_publication_invalid_mapping")
    return mapping["entries"]


async def publish(
    db, actor_id, claim, expected_finalization_revision, token, mapping, native
):
    row, copy, native, current_token, existing = await _prepared(
        db, actor_id, claim, expected_finalization_revision, native
    )
    if token != current_token:
        reject("read_publication_changed")
    entries = _mapping(mapping, row)
    if existing is not None:
        if (
            existing.token != token
            or existing.mapping_sha256 != mapping["mapping_sha256"]
            or existing.route_count != len(entries)
        ):
            reject("read_publication_changed")
        return item(existing)
    publication = Publication(
        id=row.id,
        window_id=row.window_id,
        copy_id=copy.id,
        source_location_id=row.source_location_id,
        target_location_id=row.target_location_id,
        state="active",
        revision=1,
        finalization_revision=row.revision,
        window_revision=row.window_revision,
        source=deepcopy(row.source),
        target=deepcopy(row.target),
        target_revisions=deepcopy(token["target_revisions"]),
        token=deepcopy(token),
        evidence=deepcopy(row.evidence),
        native=native,
        mapping_sha256=mapping["mapping_sha256"],
        route_count=len(entries),
        created_by=actor_id,
    )
    db.add(publication)
    await db.flush()
    for start in range(0, len(entries), 1000):
        batch = [
            {
                "publication_id": publication.id,
                **{_RENAMES.get(key, key): value for key, value in entry.items()},
            }
            for entry in entries[start : start + 1000]
        ]
        await db.execute(insert(Route), batch)
    return item(publication)


async def status(db, actor_id, claim):
    claim = await finalizations._claim(db, actor_id, claim)
    owner = await finalizations._existing(db, claim, lock=False)
    if owner is None:
        reject("finalization_unknown")
    row = await _owner(db, owner.id)
    if row is None:
        reject("read_publication_unknown")
    return item(row)


async def _withdraw_owner(db, actor_id, claim):
    claim = await finalizations._claim(db, actor_id, claim)
    await finalizations._window(db, uuid.UUID(claim["window_id"]))
    owner = await finalizations._existing(db, claim)
    if owner is None:
        reject("finalization_unknown")
    row = await _owner(db, owner.id, lock=True)
    if row is None:
        reject("read_publication_unknown")
    return row


async def _change(db, row, state):
    stamp = await db.scalar(select(func.statement_timestamp()))
    row.state, row.revision, row.updated_at = state, row.revision + 1, stamp
    if state == "withdrawing":
        row.withdrawing_at = stamp
    else:
        row.withdrawn_at = stamp
    await db.flush()


async def begin_withdrawal(db, actor_id, claim, expected_revision):
    row = await _withdraw_owner(db, actor_id, claim)
    _cas(row, expected_revision)
    if row.state == "active":
        await _change(db, row, "withdrawing")
    return item(row)


async def finish_withdrawal(db, actor_id, claim, expected_revision):
    row = await _withdraw_owner(db, actor_id, claim)
    _cas(row, expected_revision)
    if row.state == "withdrawn":
        return item(row)
    if row.state != "withdrawing":
        reject("read_publication_changed")
    if await storage_access.operation_is_open(db, purpose(row.id)):
        reject("read_publication_unsettled")
    await _change(db, row, "withdrawn")
    return item(row)


async def _physical(db, row, *, lock=False, expected_revision=None):
    source = await storage_registry_data.get_location(db, row.source_location_id)
    if (
        source is None
        or not source.enabled
        or source.config_revision != row.source_location_revision
    ):
        reject("read_publication_source_changed")
    identity, revisions, _ = await targets.facts(db, row.target_location_id, lock=lock)
    if identity != row.target_identity:
        reject("read_publication_target_changed")
    gate = await storage_access.gate_facts(
        db, row.target_location_id, lock="share_nowait" if lock else None
    )
    gate_revision = storage_access.validate(
        gate if gate is not None else {"state": "active", "revision": 1},
        expected_revision,
    )
    return identity, revisions, gate_revision


def _route(row, identity, revisions, gate_revision):
    return ReadRoute(
        publication_id=row.publication_id,
        publication_revision=row.publication_revision,
        source_object_id=row.source_object_id,
        source_location_id=row.source_location_id,
        source_endpoint_url=row.source_endpoint_url,
        source_bucket=row.source_bucket,
        source_object_key=row.source_object_key,
        source_version_id=row.source_version_id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        source_etag=row.source_etag,
        is_latest=row.is_latest,
        target_location_id=row.target_location_id,
        target_location_revision=revisions["location"],
        target_connection_id=uuid.UUID(identity["connection_id"]),
        target_connection_revision=revisions["connection"],
        target_endpoint_url=identity["endpoint_url"],
        target_bucket=identity["bucket"],
        target_object_key=row.target_object_key,
        target_version_id=row.target_version_id,
        target_etag=row.target_etag,
        target_content_sha256=row.target_content_sha256,
        creation_nonce=row.creation_nonce,
        creation_plan_uuid=row.creation_plan_uuid,
        maintenance_revision=gate_revision,
    )


@_read_boundary
async def lookup(
    db,
    *,
    source_location_id,
    endpoint_url,
    bucket,
    object_key,
    sha256,
    size_bytes,
    source_version_id=ANY_VERSION,
    source_etag=None,
    lock=False,
):
    match_version = source_version_id is not ANY_VERSION
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.migration_read_route_lookup("
                ":source_id, :endpoint, :bucket, :key, :sha256, :size_bytes, "
                ":match_version, :version_id, :etag, :lock)"
            ),
            {
                "source_id": source_location_id,
                "endpoint": endpoint_url,
                "bucket": bucket,
                "key": object_key,
                "sha256": sha256,
                "size_bytes": size_bytes,
                "match_version": match_version,
                "version_id": source_version_id if match_version else None,
                "etag": source_etag,
                "lock": lock,
            },
        )
    ).one_or_none()
    if row is None:
        return None
    identity, revisions, gate_revision = await _physical(db, row, lock=lock)
    return _route(row, identity, revisions, gate_revision)


async def _rechecked(db, route, *, lock=False):
    row = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.migration_read_route_recheck("
                ":id, :revision, :object_id, :lock)"
            ),
            {
                "id": route.publication_id,
                "revision": route.publication_revision,
                "object_id": route.source_object_id,
                "lock": lock,
            },
        )
    ).one_or_none()
    if row is None:
        reject("read_publication_changed")
    identity, revisions, gate_revision = await _physical(
        db, row, lock=lock, expected_revision=route.maintenance_revision
    )
    if _route(row, identity, revisions, gate_revision) != route:
        reject("read_publication_changed")


@_read_boundary
async def recheck(db, route, *, lock=False):
    await _rechecked(db, route, lock=lock)


@_read_boundary
async def prefix_inventory(db, route, *, prefix, lock=False):
    """Project the sealed source's visible prefix, never a target namespace LIST.

    The complete sealed bucket must have been copied. Retained or rebuilt buckets
    cannot prove absence from their partial read mappings. Historical versions
    remain individually readable but do not count as currently visible keys.
    """
    if not _text(prefix, 1024) or not route.source_object_key.startswith(prefix):
        reject("read_publication_prefix_invalid")
    await _rechecked(db, route, lock=lock)
    rows = await db.execute(
        text(
            "SELECT * FROM yoao_assets_v1.migration_read_prefix_inventory("
            ":id, :revision, :object_id, :prefix)"
        ),
        {
            "id": route.publication_id,
            "revision": route.publication_revision,
            "object_id": route.source_object_id,
            "prefix": prefix,
        },
    )
    result = {}
    for error_code, key, sha256, size in rows:
        if error_code is not None:
            reject(error_code)
        if key in result or len(result) >= 100000:
            reject("read_publication_prefix_incomplete")
        result[key] = {"sha256": sha256, "size_bytes": size}
    await _rechecked(db, route, lock=lock)
    return result
