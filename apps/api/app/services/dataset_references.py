"""Read immutable directory references through their authorized sources."""

import logging

from fastapi import HTTPException

from app.services import content_snapshot as snapshots
from app.services.dataset_reference_binding import (
    logical_context,
    physical_reference_context,
    reference_state,
)
from app.services import dataset_source_access as access
from app.services import filesystem_storage as fs
from app.services import reference_storage
from app.services import storage_fault_events
from app.services import storage_observation_rates as rates
from app.services import storage_location as locations

log = logging.getLogger(__name__)


async def reference_context(
    db, item_id, actor, *, task_id=None, node_id=None, task_authorizer=None
):
    """Current logical authority and content; source reachability is separate."""
    return logical_context(
        await reference_state(
            db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
        )
    )


async def reference_source_context(
    db, item_id, actor, *, task_id=None, node_id=None, task_authorizer=None
):
    """Qualify this reader's physical source access only when it is needed."""
    state = await reference_state(
        db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
    )
    return await physical_reference_context(db, state, node_id=node_id)


async def reference_snapshot(
    db,
    item_id,
    actor,
    *,
    expected_sha256=None,
    task_id=None,
    api_key_id=None,
    scope="tasks:read",
    node_id=None,
    task_authorizer=None,
    on_acquired=None,
    require_range_support=False,
):
    from app.services.dataset_source_access import key_authority
    from app.services import original_cache

    context = await reference_context(
        db,
        item_id,
        actor,
        task_id=task_id,
        node_id=node_id,
        task_authorizer=task_authorizer,
    )
    proof = await key_authority(db, actor, api_key_id, scope)
    if require_range_support and context["location_kind"] != "agent":
        raise HTTPException(416, "Range is not supported for verified snapshots")
    if expected_sha256 is not None and expected_sha256 != context["sha256"]:
        raise snapshots.ContentError("content_mismatch")
    cache_factory = (
        _cache_factory(db)
        if (context["source_policy"] == context["location_policy"] == "on_demand")
        else None
    )
    snapshot = None
    source_context = None
    admission_failed = False

    async def admitted():
        nonlocal admission_failed
        if on_acquired is not None:
            try:
                await on_acquired()
            except BaseException:
                admission_failed = True
                raise
        if source_context is not None:
            from app.services.managed_original_reclaim_fence import (
                protect_observed_read,
            )

            await protect_observed_read(
                db, source_context["expected"], source_context["anchor"]
            )

    if cache_factory:
        from sqlalchemy.exc import SQLAlchemyError

        if db is not None and api_key_id is not None:
            await db.commit()
        try:
            snapshot = await original_cache.read(
                cache_factory,
                context["source_id"],
                context["asset_id"],
                on_acquired=admitted,
            )
        except SQLAlchemyError:
            if admission_failed:
                raise
            log.warning("Original cache lookup deferred asset=%s", context["asset_id"])
    from_source = snapshot is None
    observation = None
    rate_attempt = None
    selected_route = None
    replica_grant = None
    routes = [None]
    from app.services import agent_storage, dataset_reference_replicas as replicas

    async def validate_business():
        if (
            await reference_context(
                db, item_id, actor, task_id=task_id, task_authorizer=task_authorizer
            )
            != context
        ):
            raise snapshots.ContentError("content_binding_changed")
        if await key_authority(db, actor, api_key_id, scope) != proof:
            raise HTTPException(403, detail={"code": "api_key_authority_revoked"})

    async def physical(route):
        if route is not None:
            return await replicas.source_context(
                db, context, route, actor, task_id=task_id, node_id=node_id
            )
        current = await reference_source_context(
            db,
            item_id,
            actor,
            task_id=task_id,
            node_id=node_id,
            task_authorizer=task_authorizer,
        )
        if any(current.get(key) != value for key, value in context.items()):
            raise snapshots.ContentError("content_binding_changed")
        if len(routes) > 1:
            current["agent_authority"] = await replicas.assert_agent_readable(
                db, current
            )
        return None, current

    async def validate():
        await validate_business()
        if source_context is not None:
            if await physical(selected_route) != (replica_grant, source_context):
                raise snapshots.ContentError("content_binding_changed")

    if from_source:
        routes = await replicas.route_order(db, context)
        for selected_route in routes:
            source_context = None
            replica_grant = None
            observation = rate_attempt = None
            try:
                await validate_business()
                replica_grant, source_context = await physical(selected_route)
                actor_id = access._actor_id(actor)
                observation = await storage_fault_events.prepare(
                    db,
                    location_id=source_context["location_id"],
                    source_id=source_context["source_id"],
                    operation="reference_read",
                    actor_id=actor_id,
                    node_id=node_id or locations.api_node_id(),
                    maintenance_revision=source_context["maintenance_revision"],
                    expected_context=source_context,
                    path_digest=source_context["path_digest"],
                    expected=context["sha256"],
                    target=source_context["locator"],
                    task_id=task_id,
                    **{
                        key: source_context[key]
                        for key in ("item_id", "asset_id", "run_id")
                    },
                )
                rate_attempt = rates.begin(
                    source_context["location_id"],
                    source_context["source_id"],
                    "original_read",
                )
                async with storage_fault_events.observe(
                    db,
                    operation="reference_read",
                    observation=observation,
                    rate_attempt=rate_attempt,
                    validate=validate,
                    actor_id=actor_id,
                    task_id=task_id,
                    **{
                        key: source_context[key]
                        for key in (
                            "location_id",
                            "source_id",
                            "item_id",
                            "asset_id",
                            "run_id",
                        )
                    },
                ):
                    if isinstance(source_context["config"], agent_storage.AgentAccess):
                        await db.commit()
                        snapshot = await agent_storage.capture(
                            db,
                            source_context["config"],
                            source_context["base"],
                            source_context["path"],
                            expected_anchor=source_context["anchor"],
                            expected=source_context["expected"],
                            expected_source_identity=source_context["source_identity"],
                            expected_sha256=context["sha256"],
                            expected_revision=source_context["maintenance_revision"],
                            node_id=node_id,
                            on_acquired=admitted,
                            fence={
                                "source_id": str(source_context["source_id"]),
                                "asset_id": str(source_context["asset_id"]),
                                "run_id": str(source_context["run_id"]),
                                "user_id": str(actor_id),
                                **({"task_id": str(task_id)} if task_id else {}),
                            },
                        )
                    elif isinstance(
                        source_context["config"], reference_storage.RegisteredReference
                    ):
                        snapshot = await source_context["config"].capture(
                            db,
                            "original",
                            reference_storage.original_request(
                                source_context, task_id, scope
                            ),
                            context["sha256"],
                            actor_id=actor_id,
                            key_id=api_key_id,
                            key_proof=proof,
                            maintenance_revision=source_context["maintenance_revision"],
                            node_id=node_id or locations.api_node_id(),
                            on_acquired=admitted,
                        )
                    else:
                        snapshot = await snapshots.capture_from_location(
                            db,
                            source_context["location_id"],
                            lambda: fs.content_file(
                                source_context["config"],
                                source_context["base"],
                                source_context["path"],
                                expected_anchor=source_context["anchor"],
                                expected=source_context["expected"],
                                expected_source_identity=source_context[
                                    "source_identity"
                                ],
                            ),
                            context["sha256"],
                            expected_revision=source_context["maintenance_revision"],
                            node_id=node_id,
                            on_acquired=admitted,
                        )
                    if (
                        snapshot.sha256 != context["sha256"]
                        or snapshot.size != context["size"]
                    ):
                        raise snapshots.ContentError("content_mismatch")
                break
            except BaseException as exc:
                if snapshot is not None:
                    try:
                        await agent_storage.reject(db, snapshot)
                    finally:
                        snapshot.close()
                        snapshot = None
                await rates.finish(db, rate_attempt, None)
                if (
                    not isinstance(
                        exc,
                        (
                            snapshots.ContentError,
                            fs.FilesystemStorageError,
                            HTTPException,
                        ),
                    )
                    or admission_failed
                    or getattr(exc, "storage_operation_unsettled", False)
                    or getattr(exc, "code", None) == "snapshot_capacity_exceeded"
                ):
                    raise
                await validate_business()
                if selected_route == routes[-1]:
                    raise
    try:
        await validate()
        if (
            from_source
            and cache_factory
            and source_context["source_policy"]
            == source_context["location_policy"]
            == "on_demand"
        ):
            await _retain_snapshot(original_cache, cache_factory, context, snapshot)
            await validate()
        if from_source:
            await rates.finish(db, rate_attempt, "success")
            await storage_fault_events.recover(db, observation)
        if getattr(snapshot, "_agent_receipt", None) is not None:
            await agent_storage.accept(db, snapshot)
            await db.commit()
        snapshot._range_supported = context["location_kind"] == "agent"
        return snapshot
    except BaseException:
        try:
            await agent_storage.reject(db, snapshot)
        finally:
            snapshot.close()
        raise
    finally:
        await rates.finish(db, rate_attempt, None)


def _cache_factory(db):
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

    return (
        async_sessionmaker(db.bind, expire_on_commit=False)
        if isinstance(db.bind, AsyncEngine)
        else None
    )


async def _retain_snapshot(cache, factory, context, snapshot):
    try:
        for attempt in range(9):
            result = await cache.store(
                factory,
                context["source_id"],
                context["asset_id"],
                snapshot,
                expected_revision=context["maintenance_revision"],
            )
            if result != "quota_full":
                return result
            if attempt == 8:
                return "quota_full"
            identity = await cache.evict_lru(factory, context["location_id"])
            if identity is None or await cache.purge(factory, identity) != "removed":
                return "quota_full"
        return "quota_full"
    except snapshots.ContentError as exc:
        if exc.code.startswith("storage_"):
            raise
        log.warning("Original cache retention deferred asset=%s", context["asset_id"])
        return "fill_failed"
    except Exception:
        log.warning("Original cache retention deferred asset=%s", context["asset_id"])
        return "fill_failed"
