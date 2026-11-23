"""Publish exact-content grants and qualify their physical read candidates."""

import uuid

from fastapi import HTTPException
from sqlalchemy import Boolean, Uuid, cast, func, text

from app.services import content_snapshot as snapshots
from app.services import dataset_references as references
from app.services import site_agents
from app.services.storage_registry_data import get_location


def active_replica_exists(primary_asset_id, *, actor=None):
    """SQL attempt eligibility only; never a physical read or authority receipt."""
    return func.yoao_assets_v1.directory_reference_replica_available(
        cast(primary_asset_id, Uuid),
        cast(actor.id if actor is not None else None, Uuid),
        type_=Boolean,
    )


async def _primary(db, item_id, actor):
    state = await references._reference_state(db, item_id, actor)
    if state.facts.accepted_revision_id is None:
        raise snapshots.ContentError("reference_replica_requires_accepted_revision")
    return state.facts


def _candidate_out(row):
    return {
        key: row[key]
        for key in (
            "item_id",
            "asset_id",
            "source_id",
            "source_name",
            "location_id",
            "location_name",
            "location_kind",
            "site_id",
            "site_name",
            "agent_id",
            "agent_state",
        )
    }


async def _page(
    db, item_id, actor, *, limit, offset=0, candidates=False, replica_id=None
):
    rows = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.directory_reference_replica_page("
                    "CAST(:actor AS uuid), CAST(:item AS uuid), :limit, :offset, "
                    ":candidates, CAST(:replica AS uuid))"
                ),
                {
                    "actor": actor.id,
                    "item": item_id,
                    "limit": limit,
                    "offset": offset,
                    "candidates": candidates,
                    "replica": replica_id,
                },
            )
        )
        .mappings()
        .all()
    )
    out = [
        _candidate_out(row) if candidates else _link_out(row, item_id)
        for row in rows
        if row["item_id"] is not None
    ]
    return {"items": out, "total": rows[0]["total"]}


async def candidates(db, item_id, actor, *, limit=50, offset=0):
    await _primary(db, item_id, actor)
    return await _page(db, item_id, actor, limit=limit, offset=offset, candidates=True)


def _link_out(row, item_id):
    result = _candidate_out(row)
    result.update(
        id=row["id"],
        item_id=item_id,
        replica_item_id=result.pop("item_id"),
        replica_asset_id=result.pop("asset_id"),
        priority=row["priority"],
        created_at=row["created_at"],
    )
    return result


async def list_replicas(db, item_id, actor, *, limit=50, offset=0):
    await _primary(db, item_id, actor)
    return await _page(db, item_id, actor, limit=limit, offset=offset)


async def _write_primary(db, item_id, actor, api_key_id):
    from app.services.dataset_lifecycle import lock_actor, require_writable

    actor, _ = await lock_actor(db, actor, api_key_id)
    primary = await _primary(db, item_id, actor)
    await require_writable(db, primary.dataset_id)
    await db.execute(
        text(
            "SELECT yoao_assets_v1.lock_directory_reference_replica_primary("
            "CAST(:item AS uuid), CAST(:asset AS uuid))"
        ),
        {"item": item_id, "asset": primary.asset_id},
    )
    return primary, actor


async def create(db, item_id, payload, actor, *, api_key_id=None):
    primary, actor = await _write_primary(db, item_id, actor, api_key_id)
    candidate = await _primary(db, payload.replica_item_id, actor)
    if (
        payload.replica_item_id == item_id
        or candidate.dataset_id != primary.dataset_id
        or candidate.sha256 != primary.sha256
        or candidate.size_bytes != primary.size_bytes
    ):
        raise snapshots.ContentError("reference_replica_content_mismatch")
    receipt = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.create_directory_reference_replica("
                "CAST(:actor AS uuid), CAST(:primary AS uuid), CAST(:candidate AS uuid), :priority)"
            ),
            {
                "actor": actor.id,
                "primary": primary.asset_id,
                "candidate": candidate.asset_id,
                "priority": payload.priority,
            },
        )
    ).one()
    if receipt.error_code:
        raise snapshots.ContentError(receipt.error_code)
    return await _out(db, item_id, receipt.id, actor)


async def _change(db, item_id, replica_id, actor, api_key_id, priority):
    primary, actor = await _write_primary(db, item_id, actor, api_key_id)
    error = await db.scalar(
        text(
            "SELECT yoao_assets_v1.change_directory_reference_replica("
            "CAST(:actor AS uuid), CAST(:primary AS uuid), CAST(:replica AS uuid), "
            "CAST(:priority AS integer))"
        ),
        {
            "actor": actor.id,
            "primary": primary.asset_id,
            "replica": replica_id,
            "priority": priority,
        },
    )
    if error == "reference_replica_not_found":
        raise HTTPException(404, "Reference replica not found")
    if error == "reference_replica_source_not_found":
        raise HTTPException(404, "Dataset source not found")
    return actor


async def update(db, item_id, replica_id, payload, actor, *, api_key_id=None):
    actor = await _change(db, item_id, replica_id, actor, api_key_id, payload.priority)
    return await _out(db, item_id, replica_id, actor)


async def revoke(db, item_id, replica_id, actor, *, api_key_id=None):
    await _change(db, item_id, replica_id, actor, api_key_id, None)


async def _out(db, item_id, replica_id, actor):
    items = (await _page(db, item_id, actor, limit=1, replica_id=replica_id))["items"]
    if not items:
        raise HTTPException(404, "Reference replica not found")
    return items[0]


async def route_order(db, context):
    """Only explicit edges, with persisted priority and observed health as hints."""
    rows = await db.scalars(
        text(
            "SELECT * FROM yoao_assets_v1.directory_reference_route_order("
            "CAST(:asset AS uuid), CAST(:location AS uuid))"
        ),
        {"asset": context["asset_id"], "location": context["location_id"]},
    )
    return rows.all() or [None]


async def assert_agent_readable(db, context):
    """A routing hint never substitutes for current credential/root qualification."""
    if context["location_kind"] != "agent":
        return
    location = await get_location(db, context["location_id"])
    agent, credential = await site_agents.get_identity(
        db, uuid.UUID(location.config["agent_id"])
    )
    try:
        site_agents.validate_current(agent, credential)
    except site_agents.SiteAgentError as exc:
        raise snapshots.ContentError(exc.code, exc.status) from None
    if site_agents.state(agent) == "offline":
        raise snapshots.ContentError("agent_offline", 503)
    if agent.draining:
        raise snapshots.ContentError("agent_draining")
    if (location.config["root_handle"], location.config["root_revision"]) not in {
        (root["root_handle"], root["root_revision"]) for root in agent.roots
    }:
        raise snapshots.ContentError("agent_root_revision_changed")
    if agent.reported_limits is None:
        raise snapshots.ContentError("agent_unavailable", 503)
    return {
        "agent_id": agent.id,
        "credential_revision": credential.revision,
        "credential_expires_at": credential.expires_at,
        "root_handle": location.config["root_handle"],
        "root_revision": location.config["root_revision"],
    }


async def source_context(db, context, replica_id, actor, *, task_id=None, node_id=None):
    """Delegate only the already authorized primary's exact bytes to one edge."""
    if db.autoflush:
        await db.flush()
    link = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_replica_edge("
                "CAST(:asset AS uuid), CAST(:replica AS uuid))"
            ),
            {"asset": context["asset_id"], "replica": replica_id},
        )
    ).one_or_none()
    if link is None:
        raise snapshots.ContentError("reference_replica_changed")
    if task_id is None:
        state = await references._reference_state(db, link.replica_item_id, actor)
    else:
        state = await references._reference_binding_state(db, link.replica_item_id)
    candidate = state.facts
    if (
        candidate.dataset_id != link.primary_dataset_id
        or candidate.asset_id != link.replica_asset_id
        or candidate.sha256 != context["sha256"]
        or candidate.size_bytes != context["size"]
        or candidate.accepted_revision_id is None
    ):
        raise snapshots.ContentError("reference_replica_content_mismatch")
    physical = await references._physical_reference_context(db, state, node_id=node_id)
    authority = await assert_agent_readable(db, physical)
    return {
        "id": link.id,
        "primary_asset_id": context["asset_id"],
        "replica_asset_id": link.replica_asset_id,
        "priority": link.priority,
        "agent_authority": authority,
    }, physical
