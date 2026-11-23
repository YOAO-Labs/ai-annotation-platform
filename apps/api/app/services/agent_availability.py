"""Safe, database-only availability for an already authorized Agent source."""

from sqlalchemy import Uuid, and_, case, column, literal, select, table

from app.db.models.dataset_asset import StorageLocation

status_row = table(
    "agent_location_status",
    column("location_id", Uuid),
    column("agent_id", Uuid),
    column("site_id", Uuid),
    column("agent_state"),
    column("agent_reason_code"),
    column("agent_site_name"),
    column("agent_name"),
    column("agent_last_seen"),
    column("agent_measured_at"),
    schema="yoao_assets_storage_v1",
)


def join_status(query, location_id=StorageLocation.id):
    return query.outerjoin(status_row, status_row.c.location_id == location_id)


def reason():
    return status_row.c.agent_reason_code


def projection(visible=literal(True)):
    allowed = and_(visible, status_row.c.location_id.is_not(None))
    return [
        case((allowed, status_row.c[key]), else_=None).label(key)
        for key in (
            "agent_state",
            "agent_reason_code",
            "agent_site_name",
            "agent_name",
            "agent_last_seen",
            "agent_measured_at",
        )
    ]


def from_row(row):
    if row.get("agent_state") is None:
        return None
    return {
        "state": row["agent_state"],
        "reason_code": row["agent_reason_code"],
        "site_name": row["agent_site_name"],
        "agent_name": row["agent_name"],
        "last_seen": row["agent_last_seen"],
        "measured_at": row["agent_measured_at"],
    }


async def for_locations(db, location_ids):
    """Call only after source authorization; one query for the current page."""
    if not location_ids:
        return {}
    rows = await db.execute(
        select(status_row).where(status_row.c.location_id.in_(location_ids))
    )
    return {row["location_id"]: from_row(row) for row in rows.mappings()}
