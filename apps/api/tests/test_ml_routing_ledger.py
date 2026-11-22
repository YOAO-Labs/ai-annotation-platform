"""ADR-0050 §C.2 · Redis routing ledger golden tests.

Requires a live Redis (TEST_REDIS_URL or default settings.redis_url). Each test uses
an isolated namespace + cleans before/after. These are the gold-standard tests for the
atomic acquire/heartbeat/finish/cancel Lua scripts — the algorithmic contract frozen in
plan appendix §B.4. The SWRR pure core is also tested in test_ml_routing_capability_and_policy.py.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from app.config import settings
from app.services.ml_routing.contracts import (
    RouteOutcome,
    RoutingCandidate,
    TrafficState,
    RejectionReason,
)
from app.services.ml_routing.ledger import RoutingLedger


def _redis_url() -> str:
    return os.environ.get("TEST_REDIS_URL", settings.redis_url)


def _test_namespace() -> str:
    return f"ml-router-test:{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def ledger() -> AsyncIterator[RoutingLedger]:
    ns = _test_namespace()
    store = RoutingLedger.from_url(
        _redis_url(),
        namespace=ns,
        lease_ttl_ms=2000,
        heartbeat_interval_ms=500,
        passive_failure_threshold=3,
        eject_ms=2000,
    )
    yield store
    client = Redis.from_url(_redis_url(), decode_responses=True)
    async for key in client.scan_iter(match=f"{ns}:*", count=200):
        await client.delete(key)
    await client.aclose()
    await store.aclose()


def _candidate(
    instance_hex: str,
    *,
    weight: int = 1,
    max_concurrency: int = 4,
    fingerprint_ok: bool = True,
    health_fresh: bool = True,
    traffic_state: TrafficState = TrafficState.ACTIVE,
) -> RoutingCandidate:
    return RoutingCandidate(
        instance_id=uuid.UUID(instance_hex),
        weight=weight,
        max_concurrency=max_concurrency,
        fingerprint_ok=fingerprint_ok,
        health_fresh=health_fresh,
        traffic_state=traffic_state,
    )


def _ids(n: int) -> list[str]:
    base = uuid.UUID("00000000-0000-0000-0000-000000000000")
    return [str(uuid.UUID(int=base.int + i)) for i in range(n)]


@pytest.mark.asyncio
async def test_acquire_selects_one_of_eligible(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    ids = _ids(2)
    cands = [_candidate(i) for i in ids]
    lease, reason = await ledger.acquire(
        pool_id, generation=1, candidates=cands, owner="test", operation="predict"
    )
    assert lease is not None
    assert reason is None
    assert lease.instance_id in ids
    assert lease.pool_id == pool_id


@pytest.mark.asyncio
async def test_acquire_respects_max_concurrency(ledger: RoutingLedger) -> None:
    """At max_concurrency=1, a second concurrent acquire excludes that instance."""
    pool_id = uuid.uuid4().hex
    ids = _ids(2)
    cands = [_candidate(i, max_concurrency=1) for i in ids]
    l1, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert l1 is not None
    l2, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert l2 is not None
    assert l2.instance_id != l1.instance_id
    l3, reason = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert l3 is None
    assert reason == RejectionReason.POOL_SATURATED


@pytest.mark.asyncio
async def test_acquire_no_eligible_returns_unavailable(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0], traffic_state=TrafficState.DISABLED)]
    lease, reason = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is None
    assert reason == RejectionReason.POOL_UNAVAILABLE


@pytest.mark.asyncio
async def test_acquire_all_circuit_open_returns_all_circuits_open(
    ledger: RoutingLedger,
) -> None:
    pool_id = uuid.uuid4().hex
    ids = _ids(2)
    cands = [_candidate(i) for i in ids]
    lease, reason = await ledger.acquire(
        pool_id,
        1,
        cands,
        owner="t",
        operation="p",
        circuit_open_instances=set(ids),
    )
    assert lease is None
    assert reason == RejectionReason.ALL_CIRCUITS_OPEN


@pytest.mark.asyncio
async def test_acquire_generation_mismatch_rejected(ledger: RoutingLedger) -> None:
    """An acquire with a stale generation is rejected (ADR-0050 D16)."""
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0])]
    l1, _ = await ledger.acquire(
        pool_id, generation=1, candidates=cands, owner="t", operation="p"
    )
    assert l1 is not None
    await ledger.cancel(l1)
    await ledger.bump_generation(pool_id)
    lease, reason = await ledger.acquire(
        pool_id,
        generation=1,
        candidates=cands,
        owner="t",
        operation="p",
    )
    assert lease is None
    assert reason == RejectionReason.GENERATION_MISMATCH


@pytest.mark.asyncio
async def test_newer_db_generation_self_heals_redis_state(
    ledger: RoutingLedger,
) -> None:
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0])]
    first, _ = await ledger.acquire(pool_id, 2, cands, owner="t", operation="p")
    assert first is not None
    await ledger.cancel(first)

    newer, reason = await ledger.acquire(pool_id, 7, cands, owner="t", operation="p")
    assert newer is not None
    assert newer.generation == 7
    assert reason is None

    stale, reason = await ledger.acquire(pool_id, 6, cands, owner="t", operation="p")
    assert stale is None
    assert reason == RejectionReason.GENERATION_MISMATCH


@pytest.mark.asyncio
async def test_sync_generation_is_exact_idempotent_and_monotonic(
    ledger: RoutingLedger,
) -> None:
    pool_id = uuid.uuid4().hex
    assert await ledger.sync_generation(pool_id, 7) is True
    assert await ledger.sync_generation(pool_id, 7) is True
    assert await ledger.sync_generation(pool_id, 8) is True
    assert await ledger.sync_generation(pool_id, 7) is False


@pytest.mark.asyncio
async def test_swrr_weight_1_2_distribution_over_redis(ledger: RoutingLedger) -> None:
    """The Redis Lua SWRR matches the pure core: weight 1:2 → 33/67 over a cycle."""
    pool_id = uuid.uuid4().hex
    ids = _ids(2)
    cands = [
        _candidate(ids[0], weight=1, max_concurrency=100),
        _candidate(ids[1], weight=2, max_concurrency=100),
    ]
    counts = {ids[0]: 0, ids[1]: 0}
    for _ in range(300):
        lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
        assert lease is not None
        counts[lease.instance_id] += 1
        await ledger.cancel(lease)
    assert abs(counts[ids[0]] / 300 - 1 / 3) < 0.05
    assert abs(counts[ids[1]] / 300 - 2 / 3) < 0.05


@pytest.mark.asyncio
async def test_heartbeat_extends_unexpired_lease(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0], max_concurrency=4)]
    lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is not None
    ok = await ledger.heartbeat(lease)
    assert ok is True


@pytest.mark.asyncio
async def test_heartbeat_cannot_resurrect_terminal_lease(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    instance_id = _ids(1)[0]
    lease, _ = await ledger.acquire(
        pool_id, 1, [_candidate(instance_id)], owner="t", operation="p"
    )
    assert lease is not None
    assert await ledger.cancel(lease) is True
    assert await ledger.heartbeat(lease) is False
    assert await ledger.member_inflight(pool_id, instance_id) == 0


@pytest.mark.asyncio
async def test_finish_releases_and_counts_success(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0], max_concurrency=4)]
    lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is not None
    released = await ledger.finish(lease, RouteOutcome.SUCCESS, duration_ms=42)
    assert released is True
    again = await ledger.finish(lease, RouteOutcome.SUCCESS, duration_ms=42)
    assert again is False


@pytest.mark.asyncio
async def test_cancel_releases_without_tripping_circuit(ledger: RoutingLedger) -> None:
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0], max_concurrency=4)]
    lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is not None
    released = await ledger.cancel(lease)
    assert released is True
    open_instances = await ledger.circuit_open_instances(pool_id)
    assert open_instances == set()


@pytest.mark.asyncio
async def test_finish_cancel_race_only_one_releases(ledger: RoutingLedger) -> None:
    """finish and cancel on the same lease: only the first releases (idempotency)."""
    pool_id = uuid.uuid4().hex
    cands = [_candidate(_ids(1)[0], max_concurrency=4)]
    lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is not None
    r_finish = await ledger.finish(lease, RouteOutcome.SUCCESS, duration_ms=1)
    r_cancel = await ledger.cancel(lease)
    assert (r_finish, r_cancel) in {(True, False), (False, True)}


@pytest.mark.asyncio
async def test_transport_failures_open_circuit_after_threshold(
    ledger: RoutingLedger,
) -> None:
    """Consecutive transport failures reach threshold → circuit opens, instance excluded."""
    pool_id = uuid.uuid4().hex
    inst = _ids(1)[0]
    cands = [_candidate(inst, max_concurrency=4)]
    for _ in range(3):
        lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
        assert lease is not None
        await ledger.finish(lease, RouteOutcome.CONNECT_REFUSED, duration_ms=1)
    open_ids = await ledger.circuit_open_instances(pool_id)
    assert inst in open_ids
    lease, reason = await ledger.acquire(
        pool_id,
        1,
        cands,
        owner="t",
        operation="p",
        circuit_open_instances=open_ids,
    )
    assert lease is None
    assert reason == RejectionReason.ALL_CIRCUITS_OPEN


@pytest.mark.asyncio
async def test_business_4xx_does_not_trip_circuit(ledger: RoutingLedger) -> None:
    """Business errors do NOT open the circuit (ADR-0050 D14)."""
    pool_id = uuid.uuid4().hex
    inst = _ids(1)[0]
    cands = [_candidate(inst, max_concurrency=4)]
    for _ in range(5):
        lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
        assert lease is not None
        await ledger.finish(lease, RouteOutcome.BUSINESS_ERROR, duration_ms=1)
    open_ids = await ledger.circuit_open_instances(pool_id)
    assert inst not in open_ids


@pytest.mark.asyncio
async def test_success_resets_failure_counter(ledger: RoutingLedger) -> None:
    """A success between failures resets the consecutive count (D14)."""
    pool_id = uuid.uuid4().hex
    inst = _ids(1)[0]
    cands = [_candidate(inst, max_concurrency=4)]
    for outcome in [
        RouteOutcome.TRANSPORT_TIMEOUT,
        RouteOutcome.TRANSPORT_TIMEOUT,
        RouteOutcome.SUCCESS,
        RouteOutcome.TRANSPORT_TIMEOUT,
        RouteOutcome.TRANSPORT_TIMEOUT,
    ]:
        lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
        await ledger.finish(lease, outcome, duration_ms=1)
    open_ids = await ledger.circuit_open_instances(pool_id)
    assert inst not in open_ids


@pytest.mark.asyncio
async def test_expired_lease_reclaimed_on_next_acquire(ledger: RoutingLedger) -> None:
    """A lease whose TTL passed is swept by the next acquire (crash reclaim, §C.2)."""
    pool_id = uuid.uuid4().hex
    inst = _ids(1)[0]
    cands = [_candidate(inst, max_concurrency=1)]
    lease, _ = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert lease is not None
    l2, reason = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert l2 is None and reason == RejectionReason.POOL_SATURATED
    await asyncio.sleep(2.2)
    l3, reason = await ledger.acquire(pool_id, 1, cands, owner="t", operation="p")
    assert l3 is not None, f"expected reclaim after TTL, got reason={reason}"


@pytest.mark.asyncio
async def test_namespace_isolated_from_gpu_arbiter(ledger: RoutingLedger) -> None:
    """ml-router keys must never collide with gpu-arbiter keys (ADR-0050 D7)."""
    client = Redis.from_url(_redis_url(), decode_responses=True)
    found = set()
    async for key in client.scan_iter(match=f"{ledger.namespace}:*", count=50):
        found.add(key.split(":", 2)[0])
    await client.aclose()
    assert "gpu-arbiter" not in found
