"""Route adapter tests using committed topology and real durable admission."""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.routing_runtime import RoutingRuntime
from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouterMode,
)
from app.services.ml_routing.router import MLBackendRouter
from app.services.ml_routing.ledger import RoutingLedger
from tests.conftest import create_registry_with_pool
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect


@pytest_asyncio.fixture
async def committed_routes(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    receipts_before = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
            "FROM aap_inference_routing_v1.command_receipts"
        )
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        actor = await create_user(
            db, "super_admin", f"route-{uuid.uuid4()}@test.local", "Routing acceptance"
        )
        project = await create_project(db, owner_id=actor.id)
        pool, legacy, other = await _seed_pool_with_two_members(db, project.id)
        await db.commit()
        pool_id = pool.id
        registry_ids = (other.id, legacy.id)
        try:
            yield db, project, pool, legacy, other
        finally:
            await db.rollback()
            try:
                # Every row belongs to this fixture's committed world. Never
                # commit the API request/savepoint fixture to expose topology.
                for registry_id in registry_ids:
                    await _cleanup(
                        owner, {"registry_id": registry_id, "pool_id": pool_id}
                    )
                receipts_after = set(
                    await owner.fetchval(
                        "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) "
                        "FROM aap_inference_routing_v1.command_receipts"
                    )
                )
                await owner.execute(
                    "DELETE FROM aap_inference_routing_v1.command_receipts "
                    "WHERE id=ANY($1::uuid[])",
                    list(receipts_after - receipts_before),
                )
                await db.delete(project)
                await db.flush()
                await db.delete(actor)
                await db.commit()
            finally:
                await owner.close()


@pytest_asyncio.fixture
async def routing_admission(committed_routes, monkeypatch):
    """Serve durable admission over the restricted routing login for this world.

    The committed world is seeded before the owner starts, and the owner reads it
    over its own connections, so admission never depends on savepoint state.
    """
    dsn = os.environ.get("AAP_MODULE_DATA_INFERENCE_ROUTING_URL")
    if not dsn:
        pytest.skip(
            "requires AAP_MODULE_DATA_INFERENCE_ROUTING_URL from the verified "
            "disposable module-data environment"
        )
    runtime = RoutingRuntime(
        dsn,
        health_max_age_seconds=settings.ml_backend_router_health_max_age_seconds,
        lease_ttl_ms=settings.ml_backend_router_lease_ttl_seconds * 1000,
    )
    assert await runtime.readiness()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = uuid.uuid4().hex
    server = PipelineHttpServer(
        manager=None,
        data=None,
        control_token=None,
        host="127.0.0.1",
        port=port,
        routing_manager=runtime.manager,
        routing_control_token=token,
    )
    await server.start()
    monkeypatch.setattr(settings, "inference_routing_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(settings, "inference_routing_token", token)
    monkeypatch.setattr(settings, "inference_routing_token_file", "")
    try:
        yield committed_routes
    finally:
        await server.close()
        await runtime.close()


async def _seed_pool_with_two_members(
    db, project_id: uuid.UUID
) -> tuple[MLBackendServicePool, MLBackendRegistry, MLBackendRegistry]:
    """Seed a pool with two connected, fingerprint-matched active members for routing tests."""
    # Two registries with identical capability → interchangeable.
    caps = {
        "capabilities": {"model_ids": ["sam3"], "supported_trackers": ["sam3_video"]}
    }
    r1 = MLBackendRegistry(
        name="sam3-a",
        url=f"http://sam3-a-{uuid.uuid4().hex[:6]}:9999",
        state="connected",
        is_interactive=True,
        health_meta=caps,
        source="manual",
        last_checked_at=datetime.now(UTC),
    )
    r2 = MLBackendRegistry(
        name="sam3-b",
        url=f"http://sam3-b-{uuid.uuid4().hex[:6]}:9999",
        state="connected",
        is_interactive=True,
        health_meta=caps,
        source="manual",
        last_checked_at=datetime.now(UTC),
    )
    db.add_all([r1, r2])
    await db.flush()
    from aap_inference.pool_capability import capability_fingerprint

    fp = capability_fingerprint(caps["capabilities"])
    pool = MLBackendServicePool(
        name="sam3-pool",
        enabled=True,
        routing_policy="smooth_weighted_round_robin",
        legacy_instance_id=r1.id,
        routing_generation=1,
        capability_fingerprint=fp,
        capability_snapshot=None,
    )
    db.add(pool)
    await db.flush()
    db.add_all(
        [
            MLBackendPoolMember(
                pool_id=pool.id, registry_id=r1.id, traffic_state="active", weight=1
            ),
            MLBackendPoolMember(
                pool_id=pool.id, registry_id=r2.id, traffic_state="active", weight=1
            ),
        ]
    )
    db.add(ProjectMLBackendPool(project_id=project_id, pool_id=pool.id, enabled=True))
    await db.flush()
    return pool, r1, r2


@pytest.mark.asyncio
async def test_off_mode_returns_legacy_instance_with_durable_scope(
    routing_admission,
) -> None:
    db_session, proj, pool, legacy, _ = routing_admission

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection is None
    assert sel.instance_id == legacy.id
    assert sel.lease.scope.mode == "off"
    assert await router.cancel(sel.lease)


@pytest.mark.asyncio
async def test_off_mode_respects_project_enablement(routing_admission) -> None:
    """D4: a pool not enabled for the project is rejected even in off mode."""
    db_session, proj, pool, _, _ = routing_admission
    # Remove the enablement row.
    await db_session.execute(
        delete(ProjectMLBackendPool).where(ProjectMLBackendPool.pool_id == pool.id)
    )
    await db_session.commit()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection == RejectionReason.POOL_NOT_ENABLED
    assert sel.instance_id is None


@pytest.mark.asyncio
async def test_off_mode_rejects_disabled_pool(routing_admission) -> None:
    db_session, proj, pool, _, _ = routing_admission
    pool.enabled = False
    await db_session.commit()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection == RejectionReason.POOL_NOT_ENABLED


@pytest.mark.asyncio
async def test_observe_returns_legacy_but_records_would_select(
    routing_admission,
) -> None:
    """observe: actual instance = legacy (unchanged); would_select recorded for diagnostics."""
    ns = f"ml-router-test:{uuid.uuid4().hex[:8]}"
    ledger = RoutingLedger.from_url(settings.redis_url, namespace=ns)
    db_session, proj, pool, legacy, _ = routing_admission

    try:
        router = MLBackendRouter(db_session, ledger=ledger, mode=RouterMode.OBSERVE)
        sel = await router.acquire(
            pool.id, owner="test", operation="predict", project_id=proj.id
        )
        # Actual instance is still the legacy (off-mode dispatch preserved).
        assert sel.instance_id == legacy.id
        assert sel.lease.scope.mode == "observe"
        # would_select evidence is recorded.
        assert sel.would_select is not None
        assert sel.diagnostics.get("eligible_count") == 2
        assert await router.cancel(sel.lease)
    finally:
        await ledger.aclose()


@pytest.mark.asyncio
async def test_observe_does_not_break_when_ledger_unavailable(
    routing_admission,
) -> None:
    """observe ledger failure must never break actual dispatch (ADR-0050 §14)."""
    db_session, proj, pool, legacy, _ = routing_admission

    # ledger=None but mode=OBSERVE → would-select is skipped, legacy dispatch still works.
    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OBSERVE)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.instance_id == legacy.id
    assert sel.lease.scope.mode == "observe"
    assert await router.cancel(sel.lease)


@pytest.mark.asyncio
async def test_enforce_without_redis_reserves_durable_scope(routing_admission) -> None:
    db_session, proj, pool, _, _ = routing_admission

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.ENFORCE)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection is None
    assert sel.lease.scope.mode == "enforce"
    assert await router.cancel(sel.lease)


@pytest.mark.asyncio
async def test_unavailable_owner_fails_closed_without_redis_fallback(
    committed_routes, monkeypatch
) -> None:
    from aap_inference_contract.routing import RoutingUnavailable

    db_session, proj, pool, _, _ = committed_routes
    route = MLBackendRouter(db_session, mode=RouterMode.ENFORCE)

    async def unavailable(**_kwargs):
        raise RoutingUnavailable()

    monkeypatch.setattr(route.authority, "acquire", unavailable)
    result = await route.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert result.rejection == RejectionReason.ROUTER_UNAVAILABLE
    assert result.lease is None


@pytest.mark.asyncio
async def test_pool_for_registry_resolves_singleton(db_session, super_admin) -> None:
    """Legacy-id resolver: registry → its singleton pool (for recording requested_pool_id)."""
    registry, pool = await create_registry_with_pool(db_session, name="bk")
    await db_session.flush()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    found = await router.pool_for_registry(registry.id)
    assert found is not None
    assert found.id == pool.id
    assert found.legacy_instance_id == registry.id


@pytest.mark.asyncio
async def test_unhealthy_member_excluded_from_would_select(
    routing_admission,
) -> None:
    """A member whose health is stale is not eligible (observe would-select reflects this)."""
    ns = f"ml-router-test:{uuid.uuid4().hex[:8]}"
    ledger = RoutingLedger.from_url(settings.redis_url, namespace=ns)
    db_session, proj, pool, r1, r2 = routing_admission
    # Make r2's health stale (older than max age).
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.ml_backend_router_health_max_age_seconds + 10
    )
    r2.last_checked_at = stale
    await db_session.commit()

    try:
        router = MLBackendRouter(db_session, ledger=ledger, mode=RouterMode.OBSERVE)
        sel = await router.acquire(
            pool.id, owner="test", operation="predict", project_id=proj.id
        )
        # Only r1 is health-fresh → would_select must be r1.
        assert sel.would_select == r1.id
        assert sel.diagnostics.get("eligible_count") == 1
        assert await router.cancel(sel.lease)
    finally:
        await ledger.aclose()
