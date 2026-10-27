"""Safe, database-only availability for an already authorized Agent source."""

from sqlalchemy import String, and_, case, cast, func, literal, or_, select

from app.db.models.dataset_asset import StorageLocation
from app.db.models.site_agent import AgentCredential, AgentRegistration, Site
from app.services.site_agents import HEARTBEAT_TTL


def join_status(query):
    return (
        query.outerjoin(
            AgentRegistration,
            and_(
                StorageLocation.kind == "agent",
                cast(AgentRegistration.id, String)
                == StorageLocation.config["agent_id"].astext,
            ),
        )
        .outerjoin(Site, Site.id == AgentRegistration.site_id)
        .outerjoin(
            AgentCredential,
            and_(
                AgentCredential.agent_id == AgentRegistration.id,
                AgentCredential.revision == AgentRegistration.credential_revision,
            ),
        )
    )


def reason():
    bound_root = AgentRegistration.roots.op("@>")(
        func.jsonb_build_array(
            func.jsonb_build_object(
                "root_handle",
                StorageLocation.config["root_handle"],
                "root_revision",
                StorageLocation.config["root_revision"],
            )
        )
    )
    return case(
        (StorageLocation.kind != "agent", None),
        (AgentRegistration.id.is_(None), "agent_unavailable"),
        (AgentRegistration.revoked_at.is_not(None), "agent_revoked"),
        (AgentRegistration.pair_digest.is_not(None), "agent_unpaired"),
        (
            or_(
                AgentCredential.agent_id.is_(None),
                AgentCredential.revoked_at.is_not(None),
            ),
            "agent_unauthorized",
        ),
        (AgentCredential.expires_at <= func.now(), "credential_expired"),
        (
            or_(
                AgentRegistration.last_seen.is_(None),
                AgentRegistration.last_seen <= func.now() - HEARTBEAT_TTL,
            ),
            "agent_offline",
        ),
        (AgentRegistration.draining.is_(True), "agent_draining"),
        (~func.coalesce(bound_root, False), "agent_root_revision_changed"),
        (AgentRegistration.reported_limits.is_(None), "agent_unavailable"),
        else_="agent_online",
    )


def projection(visible=literal(True)):
    why = reason()
    allowed = and_(visible, StorageLocation.kind == "agent")
    state = case(
        (why == "agent_online", "online"),
        (why == "agent_offline", "offline"),
        (why == "agent_revoked", "revoked"),
        (why == "agent_unpaired", "pending"),
        (why == "credential_expired", "expired"),
        (
            why.in_(["agent_draining", "agent_root_revision_changed"]),
            "degraded",
        ),
        else_="unavailable",
    )
    return [
        case((allowed, expression), else_=None).label("agent_" + key)
        for key, expression in (
            ("state", state),
            ("reason_code", why),
            ("site_name", Site.name),
            ("name", AgentRegistration.name),
            ("last_seen", AgentRegistration.last_seen),
            ("measured_at", func.now()),
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
        join_status(
            select(StorageLocation.id, *projection()).select_from(StorageLocation)
        ).where(StorageLocation.id.in_(location_ids), StorageLocation.kind == "agent")
    )
    return {row["id"]: from_row(row) for row in rows.mappings()}
