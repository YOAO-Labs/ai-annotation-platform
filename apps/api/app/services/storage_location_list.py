"""Database-only enrichment of an already-authorized location page."""

from types import SimpleNamespace

from sqlalchemy import DateTime, Integer, Uuid, column, func, select, table, values
from sqlalchemy.dialects.postgresql import JSONB

from app.services import storage_location
from app.services.storage_source_usage import read_usage_page
from app.services.storage_registry_data import connection_origin_row


_PROBE = table(
    "probe_health",
    column("location_id", Uuid),
    column("node_id"),
    column("config_revision", Integer),
    column("observed_at", DateTime(timezone=True)),
    column("result", JSONB),
    schema="yoao_assets_storage_v1",
)
_GATE = table(
    "gate_status",
    column("location_id", Uuid),
    column("state"),
    column("revision", Integer),
    schema="yoao_assets_storage_v1",
)


async def health_page(db, locations, measured_at):
    page = (
        values(column("location_id", Uuid), column("config_revision", Integer))
        .data([(row.id, row.config_revision) for row in locations])
        .cte("health_locations")
    )
    probe = _PROBE.c
    expires_at = probe.observed_at + storage_location.PROBE_TTL
    fresh = (probe.config_revision == page.c.config_revision) & (
        expires_at > measured_at
    )
    passed = func.coalesce(probe.result["ok"].as_boolean(), False)
    window = {"partition_by": probe.location_id}
    ranked = (
        select(
            _PROBE,
            func.count().over(**window).label("observed_node_count"),
            func.count()
            .filter(fresh & passed)
            .over(**window)
            .label("current_success_count"),
            func.count()
            .filter(fresh & ~passed)
            .over(**window)
            .label("current_failure_count"),
            func.count().filter(~fresh).over(**window).label("stale_node_count"),
            func.min(expires_at).filter(fresh).over(**window).label("fresh_until"),
            func.row_number()
            .over(**window, order_by=(probe.observed_at.desc(), probe.node_id))
            .label("rank"),
        )
        .join(page, page.c.location_id == probe.location_id)
        .subquery()
    )
    rows = (await db.execute(select(ranked).where(ranked.c.rank == 1))).mappings()
    result = {
        row.id: {
            "observed_node_count": 0,
            "current_success_count": 0,
            "current_failure_count": 0,
            "stale_node_count": 0,
            "latest": None,
            "measured_at": measured_at,
            "fresh_until": None,
        }
        for row in locations
    }
    by_id = {row.id: row for row in locations}
    for row in rows:
        result[row.location_id].update(
            {
                name: row[name]
                for name in (
                    "observed_node_count",
                    "current_success_count",
                    "current_failure_count",
                    "stale_node_count",
                    "fresh_until",
                )
            },
            latest=storage_location.probe_out(
                SimpleNamespace(**row),
                by_id[row.location_id],
                measured_at=measured_at,
                include_disabled=True,
            ),
        )
    return result


async def list_items(db, locations, actor):
    if not locations:
        return []
    measured_at = await db.scalar(select(func.transaction_timestamp()))
    ids = [row.id for row in locations]
    connection_ids = {row.connection_id for row in locations if row.connection_id}
    connections = {
        row.id: row
        for row in (
            await db.execute(
                select(connection_origin_row).where(
                    connection_origin_row.c.id.in_(connection_ids)
                )
            )
        ).mappings()
    }
    gates = {
        row.location_id: row
        for row in (
            await db.execute(select(_GATE).where(_GATE.c.location_id.in_(ids)))
        ).mappings()
    }
    health = await health_page(db, locations, measured_at)
    usage = await read_usage_page(db, locations, actor)
    result = []
    for location in locations:
        gate = gates.get(location.id)
        maintenance = (
            {"state": gate.state, "revision": gate.revision}
            if gate
            else {"state": "active", "revision": 1}
        )
        result.append(
            {
                **storage_location.location_out(
                    location,
                    actor,
                    connections.get(location.connection_id),
                    maintenance,
                ),
                "health": health[location.id],
                "source_usage": usage[location.id],
            }
        )
    return result
