"""v0.23.3 ADR-0050 §C.4 · MLBackendRouter tests (off/observe behavior).

P3 acceptance (plan §16): observe does not change the actual instance or result; all
inference paths have would-select evidence. off mode behavior = v0.23.2. These tests
focus on the router's mode semantics; the ledger Lua contract is tested separately
(test_ml_routing_ledger.py) and the full first-party call-chain wiring lands in P3/P4
via the actual routes/workers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.services.ml_routing.contracts import (
    RejectionReason,
    RouterMode,
)
from app.services.ml_routing.router import MLBackendRouter
from app.services.ml_routing.ledger import RoutingLedger
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


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

    fp = capability_fingerprint(caps)
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
async def test_off_mode_returns_legacy_instance_without_lease(
    db_session, super_admin
) -> None:
    """off mode: acquire returns the pool's legacy_instance_id, no lease (behavior = v0.23.2)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, legacy, _ = await _seed_pool_with_two_members(db_session, proj.id)
    await db_session.flush()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection is None
    assert sel.instance_id == legacy.id
    assert sel.lease is None  # off mode never acquires


@pytest.mark.asyncio
async def test_off_mode_respects_project_enablement(db_session, super_admin) -> None:
    """D4: a pool not enabled for the project is rejected even in off mode."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, _, _ = await _seed_pool_with_two_members(db_session, proj.id)
    # Remove the enablement row.
    from sqlalchemy import delete

    await db_session.execute(
        delete(ProjectMLBackendPool).where(ProjectMLBackendPool.pool_id == pool.id)
    )
    await db_session.flush()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection == RejectionReason.POOL_NOT_ENABLED
    assert sel.instance_id is None


@pytest.mark.asyncio
async def test_off_mode_rejects_disabled_pool(db_session, super_admin) -> None:
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, _, _ = await _seed_pool_with_two_members(db_session, proj.id)
    pool.enabled = False
    await db_session.flush()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OFF)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection == RejectionReason.POOL_NOT_ENABLED


@pytest.mark.asyncio
async def test_observe_returns_legacy_but_records_would_select(
    db_session, super_admin
) -> None:
    """observe: actual instance = legacy (unchanged); would_select recorded for diagnostics."""
    import os

    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
    ns = f"ml-router-test:{uuid.uuid4().hex[:8]}"
    ledger = RoutingLedger.from_url(redis_url, namespace=ns)

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, legacy, other = await _seed_pool_with_two_members(db_session, proj.id)
    await db_session.flush()

    try:
        router = MLBackendRouter(db_session, ledger=ledger, mode=RouterMode.OBSERVE)
        sel = await router.acquire(
            pool.id, owner="test", operation="predict", project_id=proj.id
        )
        # Actual instance is still the legacy (off-mode dispatch preserved).
        assert sel.instance_id == legacy.id
        assert sel.lease is None
        # would_select evidence is recorded.
        assert sel.would_select is not None
        assert sel.diagnostics.get("eligible_count") == 2
    finally:
        await ledger.aclose()


@pytest.mark.asyncio
async def test_observe_does_not_break_when_ledger_unavailable(
    db_session, super_admin
) -> None:
    """observe ledger failure must never break actual dispatch (ADR-0050 §14)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, legacy, _ = await _seed_pool_with_two_members(db_session, proj.id)
    await db_session.flush()

    # ledger=None but mode=OBSERVE → would-select is skipped, legacy dispatch still works.
    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.OBSERVE)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.instance_id == legacy.id
    assert sel.lease is None


@pytest.mark.asyncio
async def test_enforce_without_ledger_fails_closed(db_session, super_admin) -> None:
    """enforce mode with no ledger → ROUTER_UNAVAILABLE (fail closed, D6/D17)."""
    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, _, _ = await _seed_pool_with_two_members(db_session, proj.id)
    await db_session.flush()

    router = MLBackendRouter(db_session, ledger=None, mode=RouterMode.ENFORCE)
    sel = await router.acquire(
        pool.id, owner="test", operation="predict", project_id=proj.id
    )
    assert sel.rejection == RejectionReason.ROUTER_UNAVAILABLE


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
    db_session, super_admin
) -> None:
    """A member whose health is stale is not eligible (observe would-select reflects this)."""
    import os

    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
    ns = f"ml-router-test:{uuid.uuid4().hex[:8]}"
    ledger = RoutingLedger.from_url(redis_url, namespace=ns)

    user, _ = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    pool, r1, r2 = await _seed_pool_with_two_members(db_session, proj.id)
    # Make r2's health stale (older than max age).
    from app.config import settings

    stale = datetime.now(UTC) - timedelta(
        seconds=settings.ml_backend_router_health_max_age_seconds + 10
    )
    r2.last_checked_at = stale
    await db_session.flush()

    try:
        router = MLBackendRouter(db_session, ledger=ledger, mode=RouterMode.OBSERVE)
        sel = await router.acquire(
            pool.id, owner="test", operation="predict", project_id=proj.id
        )
        # Only r1 is health-fresh → would_select must be r1.
        assert sel.would_select == r1.id
        assert sel.diagnostics.get("eligible_count") == 1
    finally:
        await ledger.aclose()
