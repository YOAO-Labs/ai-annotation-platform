"""Publish exact-content grants and qualify their physical read candidates."""

from datetime import datetime, timezone
import uuid

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, StorageLocation
from app.db.models.dataset_reference_replica import DatasetReferenceReplica as Replica
from app.db.models.dataset_source import (
    DatasetReference,
    DatasetSource,
    DatasetSourceRevision,
)
from app.db.models.site_agent import AgentCredential, AgentRegistration, Site
from app.services import agent_availability, dataset_source_access as access
from app.services import dataset_references as references
from app.services import content_snapshot as snapshots
from app.services.site_agents import SiteAgentError, state, validate_current

ORIGINAL_PRIORITY = 100


def active_replica_exists(primary_asset_id, *, actor=None):
    """SQL attempt eligibility only; never a physical read or authority receipt."""
    original, target = aliased(DatasetAsset), aliased(DatasetAsset)
    original_item, target_item = aliased(DatasetItem), aliased(DatasetItem)
    reference, source, location = (
        aliased(DatasetReference),
        aliased(DatasetSource),
        aliased(StorageLocation),
    )
    query = (
        select(1)
        .select_from(Replica)
        .join(original, original.id == Replica.primary_asset_id)
        .join(original_item, original_item.id == original.dataset_item_id)
        .join(target, target.id == Replica.replica_asset_id)
        .join(target_item, target_item.id == target.dataset_item_id)
        .join(reference, reference.dataset_item_id == target_item.id)
        .join(source, source.id == reference.source_id)
        .join(location, location.id == source.location_id)
        .where(
            Replica.primary_asset_id == primary_asset_id,
            Replica.revoked_at.is_(None),
            original_item.dataset_id == target_item.dataset_id,
            original.verification_state == "verified",
            target.verification_state == "verified",
            original.checksum_algorithm == "sha256",
            target.checksum_algorithm == "sha256",
            original.checksum_value == target.checksum_value,
            original.size_bytes == target.size_bytes,
            source.enabled.is_(True),
            source.unregistered_at.is_(None),
            location.enabled.is_(True),
        )
    )
    if actor is not None:
        query = query.where(
            source.id.in_(
                select(DatasetSource.id).where(access.source_scope_clause(actor))
            )
        )
    return query.exists()


def _candidates_query():
    query = (
        select(
            DatasetItem.id.label("item_id"),
            DatasetAsset.id.label("asset_id"),
            DatasetSource.id.label("source_id"),
            DatasetSource.name.label("source_name"),
            StorageLocation.id.label("location_id"),
            StorageLocation.name.label("location_name"),
            StorageLocation.kind.label("location_kind"),
            Site.id.label("site_id"),
            Site.name.label("site_name"),
            AgentRegistration.id.label("agent_id"),
            *agent_availability.projection(),
        )
        .select_from(DatasetItem)
        .join(DatasetAsset, DatasetAsset.dataset_item_id == DatasetItem.id)
        .join(DatasetReference, DatasetReference.dataset_item_id == DatasetItem.id)
        .join(DatasetSource, DatasetSource.id == DatasetReference.source_id)
        .join(StorageLocation, StorageLocation.id == DatasetSource.location_id)
    )
    return agent_availability.join_status(query)


async def _primary(db, item_id, actor):
    context = await references.reference_context(db, item_id, actor)
    if context["accepted_revision_id"] is None:
        raise snapshots.ContentError("reference_replica_requires_accepted_revision")
    item = await db.get(DatasetItem, item_id, populate_existing=True)
    return context, item


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


async def candidates(db, item_id, actor, *, limit=50, offset=0):
    context, item = await _primary(db, item_id, actor)
    query = _candidates_query().where(
        DatasetItem.dataset_id == item.dataset_id,
        DatasetItem.id != item_id,
        DatasetAsset.media_type == "image",
        DatasetAsset.verification_state == "verified",
        DatasetAsset.checksum_algorithm == "sha256",
        DatasetAsset.checksum_value == context["sha256"],
        DatasetAsset.size_bytes == context["size"],
        DatasetSource.enabled.is_(True),
        DatasetSource.unregistered_at.is_(None),
        StorageLocation.enabled.is_(True),
        access.source_scope_clause(actor),
        ~exists().where(
            Replica.primary_asset_id == context["asset_id"],
            Replica.replica_asset_id == DatasetAsset.id,
            Replica.revoked_at.is_(None),
        ),
        exists().where(
            DatasetSourceRevision.source_id == DatasetSource.id,
            DatasetSourceRevision.dataset_item_id == DatasetItem.id,
            DatasetSourceRevision.asset_id == DatasetAsset.id,
            DatasetSourceRevision.ownership_mode == "reference",
            DatasetSourceRevision.content_sha256 == DatasetAsset.checksum_value,
        ),
    )
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(DatasetSource.name, DatasetItem.id).limit(limit).offset(offset)
    )
    return {"items": [_candidate_out(row) for row in result.mappings()], "total": total}


def _links_query(primary_asset_id, actor=None):
    query = (
        _candidates_query()
        .join(Replica, Replica.replica_asset_id == DatasetAsset.id)
        .add_columns(Replica.id, Replica.priority, Replica.created_at)
        .where(
            Replica.primary_asset_id == primary_asset_id, Replica.revoked_at.is_(None)
        )
    )
    return query.where(access.source_scope_clause(actor)) if actor else query


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
    context, _ = await _primary(db, item_id, actor)
    query = _links_query(context["asset_id"], actor)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(Replica.priority, Replica.id).limit(limit).offset(offset)
    )
    items = [_link_out(row, item_id) for row in result.mappings()]
    return {"items": items, "total": total}


async def _write_primary(db, item_id, actor, api_key_id):
    from app.services.dataset_lifecycle import lock_actor, require_writable

    actor, _ = await lock_actor(db, actor, api_key_id)
    context, item = await _primary(db, item_id, actor)
    await require_writable(db, item.dataset_id)
    # Serialize publications of this primary, including concurrent duplicate POSTs.
    await db.scalar(
        select(DatasetAsset.id)
        .where(DatasetAsset.id == context["asset_id"])
        .with_for_update()
    )
    return context, item, actor


async def create(db, item_id, payload, actor, *, api_key_id=None):
    context, item, actor = await _write_primary(db, item_id, actor, api_key_id)
    candidate, other = await _primary(db, payload.replica_item_id, actor)
    if (
        other.id == item.id
        or other.dataset_id != item.dataset_id
        or candidate["sha256"] != context["sha256"]
        or candidate["size"] != context["size"]
    ):
        raise snapshots.ContentError("reference_replica_content_mismatch")
    if await db.scalar(
        select(Replica.id).where(
            Replica.primary_asset_id == context["asset_id"],
            Replica.replica_asset_id == candidate["asset_id"],
            Replica.revoked_at.is_(None),
        )
    ):
        raise snapshots.ContentError("reference_replica_exists")
    link = Replica(
        primary_asset_id=context["asset_id"],
        replica_asset_id=candidate["asset_id"],
        priority=payload.priority,
        created_by=actor.id,
    )
    db.add(link)
    await db.flush()
    return await _out(db, context, link.id, actor)


async def _managed_link(db, item_id, replica_id, actor, api_key_id):
    context, _, actor = await _write_primary(db, item_id, actor, api_key_id)
    link = await db.scalar(
        select(Replica)
        .where(
            Replica.id == replica_id,
            Replica.primary_asset_id == context["asset_id"],
            Replica.revoked_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if link is None:
        raise HTTPException(404, "Reference replica not found")
    candidate = await db.get(DatasetAsset, link.replica_asset_id)
    reference = await db.get(DatasetReference, candidate.dataset_item_id)
    await access.require_source(db, reference.source_id, actor)
    return context, link


async def update(db, item_id, replica_id, payload, actor, *, api_key_id=None):
    context, link = await _managed_link(db, item_id, replica_id, actor, api_key_id)
    link.priority = payload.priority
    await db.flush()
    return await _out(db, context, link.id, actor)


async def revoke(db, item_id, replica_id, actor, *, api_key_id=None):
    _, link = await _managed_link(db, item_id, replica_id, actor, api_key_id)
    link.revoked_at, link.revoked_by = datetime.now(timezone.utc), actor.id
    await db.flush()


async def _out(db, context, replica_id, actor):
    row = (
        (
            await db.execute(
                _links_query(context["asset_id"], actor).where(Replica.id == replica_id)
            )
        )
        .mappings()
        .one()
    )
    return _link_out(row, context["item_id"])


async def route_order(db, context):
    """Only explicit edges, with persisted priority and observed health as hints."""
    result = await db.execute(_links_query(context["asset_id"]))
    rows = list(result.mappings())
    if not rows:
        return [None]
    statuses = await agent_availability.for_locations(db, [context["location_id"]])
    original = statuses.get(context["location_id"])
    choices = [
        (
            ORIGINAL_PRIORITY,
            int(bool(original and original["state"] != "online")),
            "",
            None,
        )
    ]
    choices.extend(
        (
            row["priority"],
            int(row["agent_state"] not in {None, "online"}),
            str(row["id"]),
            row["id"],
        )
        for row in rows
    )
    return [choice[3] for choice in sorted(choices)]


async def assert_agent_readable(db, context):
    """A routing hint never substitutes for current credential/root qualification."""
    if context["location_kind"] != "agent":
        return
    location = await db.get(
        StorageLocation, context["location_id"], populate_existing=True
    )
    agent = await db.get(
        AgentRegistration,
        uuid.UUID(location.config["agent_id"]),
        populate_existing=True,
    )
    credential = (
        await db.get(
            AgentCredential,
            (agent.id, agent.credential_revision),
            populate_existing=True,
        )
        if agent
        else None
    )
    try:
        validate_current(agent, credential)
    except SiteAgentError as exc:
        raise snapshots.ContentError(exc.code, exc.status) from None
    if state(agent) == "offline":
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
    link = await db.get(Replica, replica_id, populate_existing=True)
    if (
        link is None
        or link.revoked_at is not None
        or link.primary_asset_id != context["asset_id"]
    ):
        raise snapshots.ContentError("reference_replica_changed")
    target = await db.get(DatasetAsset, link.replica_asset_id, populate_existing=True)
    if target is None:
        raise snapshots.ContentError("reference_replica_changed")
    if task_id is None:
        state = await references._reference_state(db, target.dataset_item_id, actor)
    else:
        # The caller must recheck the original task grant. A published edge grants
        # this exact content only, not target-task access or source enumeration.
        state = await references._reference_binding_state(db, target.dataset_item_id)
    candidate = references._logical_context(state)
    original = await db.get(DatasetItem, context["item_id"], populate_existing=True)
    if (
        original is None
        or state[6].dataset_id != original.dataset_id
        or candidate["asset_id"] != link.replica_asset_id
        or candidate["sha256"] != context["sha256"]
        or candidate["size"] != context["size"]
        or candidate["accepted_revision_id"] is None
    ):
        raise snapshots.ContentError("reference_replica_content_mismatch")
    physical = await references._physical_reference_context(db, state, node_id=node_id)
    authority = await assert_agent_readable(db, physical)
    return {
        "id": link.id,
        "primary_asset_id": link.primary_asset_id,
        "replica_asset_id": link.replica_asset_id,
        "priority": link.priority,
        "agent_authority": authority,
    }, physical
