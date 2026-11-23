"""Transaction-bound registered location, connector origin and binding facts."""

from dataclasses import dataclass
from datetime import datetime
import json
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Integer,
    String,
    Uuid,
    column,
    func,
    or_,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

_SCHEMA = "yoao_assets_storage_v1"

location_row = table(
    "location_metadata",
    column("id", Uuid),
    column("name", String),
    column("kind", String),
    column("config_revision", Integer),
    column("config", JSONB),
    column("connection_id", Uuid),
    column("enabled", Boolean),
    schema=_SCHEMA,
)
connection_origin_row = table(
    "connector_origins",
    column("id", Uuid),
    column("name", String),
    column("kind", String),
    column("revision", Integer),
    column("scope", String),
    column("project_id", Uuid),
    column("created_by", Uuid),
    column("config", JSONB),
    column("secret_set", Boolean),
    schema=_SCHEMA,
)
binding_row = table(
    "purpose_bindings",
    column("purpose", String),
    column("location_id", Uuid),
    column("bucket", String),
    column("generation", Integer),
    schema=_SCHEMA,
)

_LOCATION = text(
    "SELECT * FROM yoao_assets_storage_v1.location_facts(:id,:lock,:actor,:role,:strict)"
)
_CONNECTION = text("SELECT * FROM yoao_assets_storage_v1.connector_origin(:id,:lock)")
_BINDING = text("SELECT * FROM yoao_assets_storage_v1.purpose_binding(:purpose,:lock)")
_PROBES = text("SELECT * FROM yoao_assets_storage_v1.location_probe_facts(:id,:node)")
_CREATE = text(
    "SELECT * FROM yoao_assets_storage_v1.create_read_location(:id,:name,:kind,CAST(:config AS jsonb))"
)
_EDIT = text(
    "SELECT * FROM yoao_assets_storage_v1.edit_location_policy"
    "(:id,:revision,:name,:enabled,CAST(:config AS jsonb))"
)
_ENSURE = text(
    "SELECT yoao_assets_storage_v1.ensure_embedded_targets(CAST(:config AS jsonb),CAST(:buckets AS jsonb))"
)
_SWITCH = text(
    "SELECT yoao_assets_storage_v1.switch_purpose_binding(:purpose,:generation,:location,:bucket)"
)
_LOCK_POSITIONS = text(
    "SELECT yoao_assets_storage_v1.lock_binding_locations(:current,:target)"
)
_RECORD_PROBE = text(
    "SELECT yoao_assets_storage_v1.record_location_probe"
    "(:id,:node,:revision,:observed_at,CAST(:result AS jsonb))"
)


@dataclass(frozen=True, slots=True)
class LocationFacts:
    id: UUID
    name: str
    kind: str
    config_revision: int
    config: dict
    connection_id: UUID | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class ConnectionOrigin:
    id: UUID
    name: str
    kind: str
    revision: int
    scope: str
    project_id: UUID | None
    created_by: UUID | None
    config: dict
    secret_set: bool


@dataclass(frozen=True, slots=True)
class BindingFacts:
    purpose: str
    location_id: UUID
    bucket: str
    generation: int


@dataclass(frozen=True, slots=True)
class ProbeFacts:
    location_id: UUID
    node_id: str
    config_revision: int
    observed_at: datetime
    result: dict


async def get_location(db, location_id, *, lock=None, actor=None):
    if getattr(db, "autoflush", False):
        await db.flush()
    result = await db.execute(
        _LOCATION,
        {
            "id": location_id,
            "lock": lock,
            "actor": actor.id if actor is not None else None,
            "role": actor.role if actor is not None else None,
            "strict": actor is not None,
        },
    )
    row = result.mappings().one_or_none()
    return LocationFacts(**row) if row is not None else None


async def get_connection(db, connection_id, *, lock=None):
    if getattr(db, "autoflush", False):
        await db.flush()
    result = await db.execute(_CONNECTION, {"id": connection_id, "lock": lock})
    row = result.mappings().one_or_none()
    return ConnectionOrigin(**row) if row is not None else None


async def get_binding(db, purpose, *, lock=None):
    if getattr(db, "autoflush", False):
        await db.flush()
    result = await db.execute(_BINDING, {"purpose": purpose, "lock": lock})
    row = result.mappings().one_or_none()
    return BindingFacts(**row) if row is not None else None


async def bindings(db):
    if getattr(db, "autoflush", False):
        await db.flush()
    return [
        BindingFacts(**row)
        for row in (await db.execute(select(binding_row))).mappings()
    ]


async def get_probe(db, location_id, node_id):
    if getattr(db, "autoflush", False):
        await db.flush()
    row = (
        (await db.execute(_PROBES, {"id": location_id, "node": node_id}))
        .mappings()
        .one_or_none()
    )
    return ProbeFacts(**row) if row is not None else None


async def probes(db, location_id):
    rows = await db.execute(_PROBES, {"id": location_id, "node": None})
    return [ProbeFacts(**row) for row in rows.mappings()]


async def record_probe(db, location, node_id, result, observed_at):
    await db.execute(
        _RECORD_PROBE,
        {
            "id": location.id,
            "node": node_id,
            "revision": location.config_revision,
            "observed_at": observed_at,
            "result": json.dumps(result),
        },
    )


async def create_location(db, location_id, name, kind, config):
    result = await db.execute(
        _CREATE,
        {"id": location_id, "name": name, "kind": kind, "config": json.dumps(config)},
    )
    return LocationFacts(**result.mappings().one())


async def edit_location(db, location, *, name, enabled, config):
    result = await db.execute(
        _EDIT,
        {
            "id": location.id,
            "revision": location.config_revision,
            "name": name,
            "enabled": enabled,
            "config": json.dumps(config),
        },
    )
    row = result.mappings().one_or_none()
    return LocationFacts(**row) if row is not None else None


async def ensure_embedded_targets(db, config, buckets):
    return await db.scalar(
        _ENSURE, {"config": json.dumps(config), "buckets": json.dumps(buckets)}
    )


async def list_binding_candidates(db, *, limit, offset):
    from app.services.dataset_assets import LEGACY_LOCATION_ID

    loc, origin = location_row.c, connection_origin_row.c
    display_name = func.coalesce(origin.name, loc.name)
    candidates = location_row.outerjoin(
        connection_origin_row, origin.id == loc.connection_id
    )
    eligible = (
        loc.enabled,
        loc.kind == "s3",
        or_(
            loc.id == LEGACY_LOCATION_ID,
            (origin.kind == "s3") & (origin.scope == "global"),
        ),
    )
    total = await db.scalar(
        select(func.count()).select_from(candidates).where(*eligible)
    )
    rows = await db.execute(
        select(loc.id, display_name.label("display_name"), loc.config_revision)
        .select_from(candidates)
        .where(*eligible)
        .order_by(display_name, loc.id)
        .limit(limit)
        .offset(offset)
    )
    return rows.mappings().all(), total


async def switch_binding(db, purpose, *, expected_generation, location_id, bucket):
    return await db.scalar(
        _SWITCH,
        {
            "purpose": purpose,
            "generation": expected_generation,
            "location": location_id,
            "bucket": bucket,
        },
    )


async def lock_binding_locations(db, current_id, target_id):
    return await db.scalar(
        _LOCK_POSITIONS, {"current": current_id, "target": target_id}
    )


async def binding_candidates_page(db, *, limit, offset):
    rows, total = await list_binding_candidates(db, limit=limit, offset=offset)
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
