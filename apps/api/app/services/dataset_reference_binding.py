"""Qualify the immutable binding and physical source of a directory reference."""

from copy import deepcopy
from dataclasses import dataclass
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.services import content_snapshot as snapshots
from app.services import dataset_source_access as access
from app.services import filesystem_storage as fs
from app.services import reference_storage, storage_access
from app.services import storage_location as locations
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.reference_keys import reference_key
from app.services.storage_registry_data import get_location


@dataclass(frozen=True, slots=True)
class _ReferenceState:
    item_id: uuid.UUID
    facts: object
    location: object
    maintenance_revision: int
    source_locator: dict


async def reference_state(db, item_id, actor, *, task_id=None, task_authorizer=None):
    actor_id = access._actor_id(actor)
    if task_id is None:
        await access.require_manager(db, actor_id)
    else:
        from app.services import task_access
        from app.services.original_sources import resolve_task_relation

        relation = await resolve_task_relation(db, task_id)
        if relation.item_id != item_id:
            raise HTTPException(404, "Task not found")
        task = await get_core_row(db, task_row, task_id, populate_existing=True)
        if task is None:
            raise HTTPException(404, "Task not found")
        if task_authorizer is not None:
            await task_authorizer(db, task, actor)
        else:
            await task_access.assert_task_visible(db, task, actor_id)
    state = await reference_binding_state(db, item_id, task=task if task_id else None)
    if task_id is None:
        await access.require_source(db, state.facts.source_id, actor)
    return state


async def reference_binding_state(db, item_id, *, task=None):
    """Validate one immutable binding; callers must authorize its exact content."""
    if db.autoflush:
        await db.flush()
    facts = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.directory_reference_content("
                "CAST(:item AS uuid))"
            ),
            {"item": item_id},
        )
    ).one_or_none()
    if facts is None or facts.verification_state != "verified":
        raise snapshots.ContentError("content_unverified")
    if (
        not facts.bound_run_matches
        or facts.bound_source_revision != facts.bound_run_source_revision
        or facts.bound_location_revision != facts.bound_run_location_revision
        or facts.bound_sha256 != facts.sha256
        or facts.bound_size != facts.size_bytes
    ):
        raise snapshots.ContentError("content_binding_changed")
    if (
        not facts.source_enabled
        or facts.source_unregistered_at is not None
        or facts.source_dataset_id != facts.dataset_id
    ):
        raise snapshots.ContentError("source_unavailable")
    location = await get_location(db, facts.location_id)
    if (
        location is None
        or not location.enabled
        or location.kind not in {"filesystem", "s3", "sftp", "agent"}
    ):
        raise snapshots.ContentError("location_unavailable")
    if not facts.run_matches or not facts.content_receipt:
        raise snapshots.ContentError("content_unverified")
    receipt = facts.content_receipt
    from app.services.source_selection import locator

    source_locator = locator(facts.source_config)
    run_locator = locator(facts.run_config)
    if (
        source_locator != run_locator
        or run_locator != locator(facts.bound_config)
        or facts.origin_anchor != facts.bound_anchor
        or (
            facts.accepted_revision_id is not None
            and facts.accepted_sha256 != receipt["sha256"]
        )
        or facts.checksum_algorithm != "sha256"
        or facts.sha256 != receipt["sha256"]
        or facts.size_bytes != receipt["size_bytes"]
        or facts.file_path != reference_key(item_id, facts.sha256)
        or (task is not None and task.file_path != facts.file_path)
    ):
        raise snapshots.ContentError("content_binding_changed")
    maintenance_revision = await storage_access.check(db, location.id)
    return _ReferenceState(
        item_id, facts, location, maintenance_revision, source_locator
    )


def logical_context(state):
    facts, location = state.facts, state.location

    return deepcopy(
        dict(
            source_id=facts.source_id,
            location_id=location.id,
            location_kind=location.kind,
            item_id=state.item_id,
            asset_id=facts.asset_id,
            sha256=facts.sha256,
            size=facts.size_bytes,
            width=facts.width,
            height=facts.height,
            accepted_revision_id=facts.accepted_revision_id,
            source_revision=facts.source_revision,
            location_revision=location.config_revision,
            maintenance_revision=state.maintenance_revision,
            policy_revision=facts.policy_revision,
            source_policy=facts.source_policy,
            location_policy=location.config.get("cache_policy", "preview"),
            run_id=facts.run_id,
            path_digest=facts.path_digest,
            locator=state.source_locator,
            origin_anchor=facts.origin_anchor,
            content_receipt=facts.content_receipt,
        )
    )


async def physical_reference_context(db, state, *, node_id=None):
    """Qualify a binding already authorized by the original or replica owner."""
    facts, location = state.facts, state.location
    receipt = facts.content_receipt
    node = node_id or locations.api_node_id()
    anchor = await locations.node_anchor(db, location, node)
    if anchor is None:
        raise snapshots.ContentError(
            "worker_read_not_verified" if node_id else "api_read_not_verified"
        )
    same_host = facts.scan_host_id == fs.host_identity()
    expected = (
        receipt["observation"]
        if receipt["node_id"] == node or location.kind in {"s3", "sftp", "agent"}
        else {k: receipt["observation"][k] for k in ("size", "mtime_ns", "ctime_ns")}
    )
    expected_source = (
        expected.get("source_identity")
        if receipt["node_id"] == node or location.kind == "agent"
        else facts.root_identity
        if same_host
        else None
    )
    configured = await reference_storage.configuration(db, location)
    if location.kind in {"s3", "sftp", "agent"} and (
        configured.anchor != facts.origin_anchor or anchor != configured.anchor
    ):
        raise snapshots.ContentError("content_binding_changed")
    return dict(
        **logical_context(state),
        config=configured,
        base=state.source_locator["relative_path"],
        path=facts.relative_path,
        anchor=anchor,
        expected=dict(expected),
        source_identity=expected_source,
    )
