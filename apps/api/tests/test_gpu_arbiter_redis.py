from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
import hashlib
import json
import os
import time
import uuid

import pytest
from redis.asyncio import Redis
from redis.cluster import key_slot

from app.config import settings
from aap_inference.gpu.ledger import (
    GPUAllocation,
    GPUAllocationState,
    GPUArbiterStore,
    GPUArbiterStoreError,
    GPUBackendDomainMember,
    GPUBackendMembershipState,
    GPUReconcileLeaseCleanup,
    GPURequestLeaseState,
    gpu_arbiter_keys,
    normalize_gpu_backend_max_concurrency,
)
from aap_inference.gpu.ledger import store as gpu_arbiter_store_module
from aap_inference.gpu.ledger.types import _LEDGER_REVISION_REBASE_THRESHOLD


_TEST_BACKEND_DOMAIN = (
    "backend-a",
    "backend-b",
    "backend-c",
    "backend-missing",
)
_TEST_BACKEND_MEMBERSHIPS = tuple(
    GPUBackendDomainMember(
        backend_id=backend_id,
        membership_epoch=1,
        state="active",
    )
    for backend_id in _TEST_BACKEND_DOMAIN
)


def _memberships(
    *backend_ids: str,
    epoch: int = 1,
    state: GPUBackendMembershipState = "active",
) -> tuple[GPUBackendDomainMember, ...]:
    return tuple(
        GPUBackendDomainMember(
            backend_id=backend_id,
            membership_epoch=epoch,
            state=state,
        )
        for backend_id in backend_ids
    )


def _future_reconcile_deadline_ms(window_ms: int = 120_000) -> int:
    return int(time.time() * 1000) + window_ms


def _redis_url() -> str:
    return os.environ.get("TEST_REDIS_URL", settings.redis_url)


async def _clean_namespace(redis_url: str, namespace: str) -> None:
    client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry_on_timeout=False,
    )
    try:
        async with asyncio.timeout(5):
            batch: list[str] = []
            async for key in client.scan_iter(match=f"{namespace}:*"):
                batch.append(key)
                if len(batch) >= 100:
                    await client.unlink(*batch)
                    batch.clear()
            if batch:
                await client.unlink(*batch)
    finally:
        async with asyncio.timeout(2):
            await client.aclose()


@pytest.fixture
async def redis_stores() -> AsyncIterator[tuple[GPUArbiterStore, GPUArbiterStore]]:
    redis_url = _redis_url()
    namespace = f"gpu-arbiter-test:{uuid.uuid4().hex}"
    first = GPUArbiterStore.from_url(redis_url, namespace=namespace)
    second = GPUArbiterStore.from_url(redis_url, namespace=namespace)
    try:
        assert await first.ping() is True
        assert await second.ping() is True
        yield first, second
    finally:
        close_results = await asyncio.gather(
            first.aclose(), second.aclose(), return_exceptions=True
        )
        await _clean_namespace(redis_url, namespace)
        for result in close_results:
            if isinstance(result, BaseException):
                raise result


def _admission_kwargs(
    backend_id: str,
    lease_id: str,
    *,
    budget_mb: int = 60,
    generation: str = "1",
    max_concurrency: int = 4,
    owner_id: str = "owner-a",
    eviction_priority: int = 0,
) -> dict:
    return {
        "backend_id": backend_id,
        "membership_epoch": 1,
        "budget_mb": budget_mb,
        "generation": generation,
        "eviction_priority": eviction_priority,
        "evictable": False,
        "max_concurrency": max_concurrency,
        "lease_id": lease_id,
        "owner_id": owner_id,
        "operation": "predict",
        "heartbeat_ttl_ms": 5_000,
        "hard_ttl_ms": 30_000,
    }


async def _admit_resident(
    store: GPUArbiterStore,
    resource_id: str,
    *,
    backend_id: str = "backend-a",
    lease_id: str = "lease-a",
    owner_id: str = "owner-a",
    generation: str = "1",
    budget_mb: int = 60,
    max_concurrency: int = 4,
    eviction_priority: int = 0,
    resident_cooldown_ms: int = 1,
    wait_for_cooldown: bool = True,
) -> None:
    assert (
        await store.admit(
            resource_id,
            **_admission_kwargs(
                backend_id,
                lease_id,
                owner_id=owner_id,
                generation=generation,
                budget_mb=budget_mb,
                max_concurrency=max_concurrency,
                eviction_priority=eviction_priority,
            ),
        )
    ).admitted
    assert (
        await store.transition_allocation(
            resource_id,
            backend_id=backend_id,
            expected_generation=generation,
            target_state=GPUAllocationState.LOADING,
            request_lease_id=lease_id,
            request_owner_id=owner_id,
        )
    ).status == "transitioned"
    assert (
        await store.finalize_cold_allocation(
            resource_id,
            backend_id=backend_id,
            expected_generation=generation,
            request_lease_id=lease_id,
            request_owner_id=owner_id,
            target_state=GPUAllocationState.RESIDENT,
            target_evictable=True,
            resident_cooldown_ms=resident_cooldown_ms,
        )
    ).status == "transitioned"
    if wait_for_cooldown:
        await asyncio.sleep((resident_cooldown_ms / 1000) + 0.001)


async def _bootstrap_empty_card(
    store: GPUArbiterStore,
    resource_id: str,
    allocatable_mb: int,
    *,
    memberships: tuple[GPUBackendDomainMember, ...] = _TEST_BACKEND_MEMBERSHIPS,
):
    result = await store.reconcile_card(
        resource_id,
        allocatable_mb,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=memberships,
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id=f"test-bootstrap-{uuid.uuid4().hex}",
    )
    assert result.status == "reconciled"
    assert result.ready is True
    return result


def _reconcile_allocation(
    *,
    backend_id: str = "backend-a",
    state: GPUAllocationState = GPUAllocationState.UNKNOWN,
    generation: str | None = "1",
    budget_mb: int = 60,
    last_used_at_ms: int = 1,
    not_evict_before_ms: int | None = None,
) -> GPUAllocation:
    if not_evict_before_ms is None:
        cooldown_deadline_ms = 1 if state is GPUAllocationState.RESIDENT else 0
    else:
        cooldown_deadline_ms = not_evict_before_ms
    return GPUAllocation(
        backend_id=backend_id,
        state=state,
        budget_mb=budget_mb,
        generation=generation,
        eviction_priority=0,
        evictable=False,
        max_concurrency=4,
        reservation_lease_id=None,
        reservation_owner_id=None,
        last_used_at_ms=last_used_at_ms,
        not_evict_before_ms=cooldown_deadline_ms,
    )


def test_resource_keys_share_one_brace_safe_cluster_slot() -> None:
    keys = gpu_arbiter_keys(
        "node-{tenant-a}/index:0", namespace="gpu-arbiter-test:key-codec"
    )
    same_resource = {
        keys.card,
        keys.allocations,
        keys.queue,
        keys.transition,
        keys.backend_queue("backend-a"),
        keys.leases("backend-a"),
    }
    assert len({key_slot(key.encode()) for key in same_resource}) == 1
    assert "node-{tenant-a}" not in keys.card

    other = gpu_arbiter_keys(
        "node-{tenant-b}/index:0", namespace="gpu-arbiter-test:key-codec"
    )
    assert key_slot(other.card.encode()) != key_slot(keys.card.encode())


@pytest.mark.asyncio
async def test_reconcile_bootstrap_is_atomic_and_retry_idempotent(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    allocation = _reconcile_allocation()
    kwargs = {
        "expected_ledger_revision": None,
        "expected_ledger_incarnation": None,
        "backend_memberships": _memberships("backend-a"),
        "allocations": (allocation,),
        "lease_cleanup": None,
        "ready": True,
        "reconcile_deadline_ms": _future_reconcile_deadline_ms(),
        "repair_id": "bootstrap-1",
    }

    created = await first.reconcile_card(resource_id, 100, **kwargs)
    assert created.status == "reconciled"
    assert created.ready is True
    assert created.committed_mb == 60
    assert created.idempotent is False

    snapshot = await first.snapshot(resource_id)
    assert snapshot.ready is True
    assert snapshot.committed_mb == 60
    assert snapshot.allocations == (allocation,)

    retried = await first.reconcile_card(resource_id, 100, **kwargs)
    assert retried.status == "reconciled"
    assert retried.idempotent is True
    assert retried.ledger_revision == created.ledger_revision
    assert (await first.snapshot(resource_id)).reconcile_deadline_ms == (
        snapshot.reconcile_deadline_ms
    )


@pytest.mark.asyncio
async def test_null_generation_unknown_is_counted_and_blocks_new_work(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-null-generation/index:0"
    allocation = _reconcile_allocation(generation=None)
    reconcile_deadline_ms = _future_reconcile_deadline_ms()

    created = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(allocation,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=reconcile_deadline_ms,
        repair_id="null-generation-bootstrap",
    )
    assert created.status == "reconciled"
    assert created.committed_mb == allocation.budget_mb

    snapshot = await first.snapshot(resource_id)
    assert snapshot.ready is True
    assert snapshot.allocations == (allocation,)
    assert snapshot.allocations[0].generation is None
    assert snapshot.allocations[0].evictable is False

    retried = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(allocation,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=reconcile_deadline_ms,
        repair_id="null-generation-bootstrap",
    )
    assert retried.status == "reconciled"
    assert retried.idempotent is True
    assert retried.ledger_revision == snapshot.ledger_revision

    admission = await first.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-null-generation"),
    )
    assert (admission.status, admission.reason) == (
        "not_ready",
        "allocation_unknown",
    )
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="backend-ticket-null-generation",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "not_ready"
    assert (
        await first.enqueue_card(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="card-ticket-null-generation",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "not_ready"

    transitioned = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="1",
        target_state=GPUAllocationState.UNLOADED,
    )
    assert transitioned.status == "stale_generation"
    assert transitioned.generation is None
    owner = await first.acquire_transition_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="transition-owner",
        generation="1",
        operation="evict",
        ttl_ms=30_000,
    )
    assert owner.status == "invalid_transition"
    assert owner.generation is None
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == snapshot.ledger_revision
    assert after.allocations == (allocation,)
    assert after.leases == ()
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="backend-ticket-null-generation",
            card_queue=False,
        )
    ).status == "missing"
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="card-ticket-null-generation",
            card_queue=True,
        )
    ).status == "missing"


@pytest.mark.parametrize(
    ("allocation", "message"),
    (
        (
            replace(
                _reconcile_allocation(generation=None),
                state=GPUAllocationState.RESIDENT,
            ),
            "null generation is only valid for unknown allocations",
        ),
        (
            replace(_reconcile_allocation(generation=None), evictable=True),
            "null-generation unknown allocation cannot be evictable",
        ),
    ),
)
@pytest.mark.asyncio
async def test_null_generation_allocation_invariants_are_rejected_before_redis(
    redis_stores,
    allocation: GPUAllocation,
    message: str,
) -> None:
    first, _ = redis_stores
    with pytest.raises(ValueError, match=message):
        await first.reconcile_card(
            "node-invalid-null-generation/index:0",
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=_memberships("backend-a"),
            allocations=(allocation,),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="invalid-null-generation",
        )


@pytest.mark.asyncio
async def test_resident_without_cooldown_is_rejected_before_redis(
    redis_stores,
) -> None:
    first, _ = redis_stores
    with pytest.raises(ValueError, match="cooldown deadline must be positive"):
        await first.reconcile_card(
            "node-invalid-resident-cooldown/index:0",
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=_memberships("backend-a"),
            allocations=(
                _reconcile_allocation(
                    state=GPUAllocationState.RESIDENT,
                    not_evict_before_ms=0,
                ),
            ),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="invalid-resident-cooldown",
        )


@pytest.mark.asyncio
async def test_reconcile_never_regresses_known_generation_to_null(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-generation-regression/index:0"
    known = _reconcile_allocation(generation="7")
    await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(known,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="known-generation-bootstrap",
    )
    before = await first.snapshot(resource_id)

    rejected = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_memberships("backend-a"),
        allocations=(replace(known, generation=None),),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="known-to-null-repair",
    )
    assert (rejected.status, rejected.reason) == (
        "stale_generation",
        "generation_regression",
    )
    assert (await first.snapshot(resource_id)).allocations == (known,)


@pytest.mark.asyncio
async def test_trusted_reconcile_can_replace_null_with_known_generation(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-null-to-known-generation/index:0"
    unknown = _reconcile_allocation(generation=None)
    await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(unknown,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="null-generation-bootstrap",
    )
    before = await first.snapshot(resource_id)
    known = replace(unknown, generation="9", last_used_at_ms=2)

    reconciled = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_memberships("backend-a"),
        allocations=(known,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="null-to-known-repair",
    )
    assert reconciled.status == "reconciled"
    assert (await first.snapshot(resource_id)).allocations == (known,)


@pytest.mark.asyncio
async def test_null_generation_unknown_only_consumes_its_own_card_budget(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-null-generation-sibling/index:0"
    await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a", "backend-b"),
        allocations=(_reconcile_allocation(generation=None),),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="null-generation-sibling-bootstrap",
    )

    admitted = await first.admit(
        resource_id,
        **_admission_kwargs(
            "backend-b",
            "lease-backend-b",
            budget_mb=40,
        ),
    )
    assert admitted.status == "admitted"
    assert admitted.committed_mb == 100
    snapshot = await first.snapshot(resource_id)
    assert {item.backend_id for item in snapshot.allocations} == {
        "backend-a",
        "backend-b",
    }


@pytest.mark.asyncio
async def test_normal_repair_exact_retry_is_read_only_and_idempotent(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-normal-repair/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    before = await first.snapshot(resource_id)
    kwargs = {
        "expected_ledger_revision": before.ledger_revision,
        "expected_ledger_incarnation": before.ledger_incarnation,
        "backend_memberships": _TEST_BACKEND_MEMBERSHIPS,
        "allocations": before.allocations,
        "lease_cleanup": None,
        "ready": True,
        "reconcile_deadline_ms": _future_reconcile_deadline_ms(),
        "repair_id": "normal-repair-1",
    }

    created = await first.reconcile_card(resource_id, 100, **kwargs)
    assert created.status == "reconciled"
    assert created.idempotent is False
    committed = await first.snapshot(resource_id)

    retried = await first.reconcile_card(resource_id, 100, **kwargs)
    assert retried.status == "reconciled"
    assert retried.idempotent is True
    assert retried.ledger_revision == created.ledger_revision
    assert retried.ledger_incarnation == created.ledger_incarnation
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == committed.ledger_revision
    assert after.reconcile_deadline_ms == committed.reconcile_deadline_ms


@pytest.mark.asyncio
async def test_repair_id_fingerprint_includes_membership_epoch(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-repair-membership-fingerprint/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    before = await first.snapshot(resource_id)
    kwargs = {
        "expected_ledger_revision": before.ledger_revision,
        "expected_ledger_incarnation": before.ledger_incarnation,
        "allocations": before.allocations,
        "lease_cleanup": None,
        "ready": True,
        "reconcile_deadline_ms": _future_reconcile_deadline_ms(),
        "repair_id": "membership-sensitive-repair",
    }
    created = await first.reconcile_card(
        resource_id,
        100,
        backend_memberships=_memberships("backend-a", epoch=1),
        **kwargs,
    )
    assert created.status == "reconciled"

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_after_create = await raw.hgetall(keys.card)
        conflict = await first.reconcile_card(
            resource_id,
            100,
            backend_memberships=_memberships("backend-a", epoch=2),
            **kwargs,
        )
        assert (conflict.status, conflict.reason, conflict.idempotent) == (
            "stale_revision",
            "ledger_changed",
            False,
        )
        assert await raw.hgetall(keys.card) == card_after_create
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_membership_domain_is_canonical_and_fingerprint_covers_epoch_and_state(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-membership-canonical/index:0"
    large_epoch = 9_007_199_254_740_993
    memberships = (
        GPUBackendDomainMember("backend-b", 4, "pending"),
        GPUBackendDomainMember("backend-a", large_epoch, "active"),
    )
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_before = await raw.hgetall(keys.card)
        expected_membership_raw = json.dumps(
            [
                {
                    "backend_id": "backend-a",
                    "membership_epoch": str(large_epoch),
                    "state": "active",
                },
                {
                    "backend_id": "backend-b",
                    "membership_epoch": "4",
                    "state": "pending",
                },
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        assert card_before["backend_domain"] == '["backend-a","backend-b"]'
        assert card_before["membership_domain"] == expected_membership_raw
        assert card_before["active_backend_domain"] == '["backend-a"]'
        assert (
            card_before["membership_domain_fingerprint"]
            == hashlib.sha256(expected_membership_raw.encode()).hexdigest()
        )
        snapshot = await first.snapshot(resource_id)
        assert snapshot.backend_memberships == tuple(
            sorted(memberships, key=lambda item: item.backend_id)
        )

        await first.mark_card_not_ready(
            resource_id,
            100,
            reason="membership_change",
        )
        before_evolution = await first.snapshot(resource_id)
        evolved = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before_evolution.ledger_revision,
            expected_ledger_incarnation=before_evolution.ledger_incarnation,
            backend_memberships=(
                GPUBackendDomainMember("backend-a", large_epoch + 1, "retiring"),
                GPUBackendDomainMember("backend-b", 4, "pending"),
            ),
            evolution_id="canonical-state-change",
        )
        assert evolved.status == "evolved"
        card_after = await raw.hgetall(keys.card)
        assert (
            card_after["backend_domain_fingerprint"]
            == card_before["backend_domain_fingerprint"]
        )
        assert (
            card_after["membership_domain_fingerprint"]
            != card_before["membership_domain_fingerprint"]
        )
        assert (
            card_after["active_backend_domain_fingerprint"]
            != card_before["active_backend_domain_fingerprint"]
        )
        assert (await first.snapshot(resource_id)).active_backend_ids == ()
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_only_exact_active_membership_epoch_can_start_new_work(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-membership-admission/index:0"
    memberships = (
        GPUBackendDomainMember("backend-a", 7, "active"),
        GPUBackendDomainMember("backend-b", 3, "pending"),
        GPUBackendDomainMember("backend-c", 2, "retiring"),
    )
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    before = await first.snapshot(resource_id)

    wrong_epoch = await first.admit(
        resource_id,
        **{
            **_admission_kwargs("backend-a", "wrong-epoch"),
            "membership_epoch": 6,
        },
    )
    assert (wrong_epoch.status, wrong_epoch.reason) == (
        "config_mismatch",
        "membership_epoch_changed",
    )
    for backend_id, membership_epoch in (("backend-b", 3), ("backend-c", 2)):
        rejected = await first.admit(
            resource_id,
            **{
                **_admission_kwargs(backend_id, f"inactive-{backend_id}"),
                "membership_epoch": membership_epoch,
            },
        )
        assert (rejected.status, rejected.reason) == (
            "config_mismatch",
            "backend_not_active",
        )
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-b",
            membership_epoch=3,
            ticket_id="pending-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "config_mismatch"
    assert (
        await first.enqueue_card(
            resource_id,
            backend_id="backend-a",
            membership_epoch=6,
            ticket_id="wrong-card-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "config_mismatch"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=6,
            owner_id="wrong-owner",
            generation="1",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "config_mismatch"
    rejected_snapshot = await first.snapshot(resource_id)
    assert rejected_snapshot.ledger_revision == before.ledger_revision
    assert rejected_snapshot.allocations == ()
    assert rejected_snapshot.leases == ()

    backend_ticket = await first.enqueue_backend(
        resource_id,
        backend_id="backend-a",
        membership_epoch=7,
        ticket_id="active-backend-ticket",
        owner_id="owner-a",
        ttl_ms=30_000,
    )
    card_ticket = await first.enqueue_card(
        resource_id,
        backend_id="backend-a",
        membership_epoch=7,
        ticket_id="active-card-ticket",
        owner_id="owner-a",
        ttl_ms=30_000,
    )
    assert backend_ticket.status == card_ticket.status == "queued"
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        assert (
            json.loads(await raw.lindex(keys.backend_queue("backend-a"), 0))[
                "membership_epoch"
            ]
            == "7"
        )
        assert json.loads(await raw.lindex(keys.queue, 0))["membership_epoch"] == "7"

        admitted = await first.admit(
            resource_id,
            backend_ticket_id="active-backend-ticket",
            card_ticket_id="active-card-ticket",
            **{
                **_admission_kwargs("backend-a", "exact-active-epoch"),
                "membership_epoch": 7,
            },
        )
        assert admitted.status == "admitted"
        assert await raw.llen(keys.backend_queue("backend-a")) == 0
        assert await raw.llen(keys.queue) == 0
        card_queue_count, backend_queue_counts = await raw.hmget(
            keys.card,
            "card_queue_count",
            "backend_queue_counts",
        )
        assert card_queue_count == "0"
        assert json.loads(backend_queue_counts) == {
            "backend-a": 0,
            "backend-b": 0,
            "backend-c": 0,
        }
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_retiring_members_remain_in_all_domain_until_cleanup_converges(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-retiring-cleanup/index:0"
    initial_memberships = _memberships("backend-a", "backend-c")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=initial_memberships,
    )
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        budget_mb=30,
    )
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-c",
        lease_id="lease-c",
        owner_id="owner-c",
        budget_mb=30,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-c",
            lease_id="lease-c",
            owner_id="owner-c",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="backend-ticket-a",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "queued"
    assert (
        await first.enqueue_card(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="card-ticket-a",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "queued"

    ready_snapshot = await first.snapshot(resource_id)
    target_with_pending = (
        GPUBackendDomainMember("backend-a", 1, "active"),
        GPUBackendDomainMember("backend-b", 1, "pending"),
        GPUBackendDomainMember("backend-c", 1, "active"),
    )
    refused = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=ready_snapshot.ledger_revision,
        expected_ledger_incarnation=ready_snapshot.ledger_incarnation,
        backend_memberships=target_with_pending,
        evolution_id="must-fail-while-ready",
    )
    assert (refused.status, refused.reason) == (
        "not_ready",
        "domain_evolution_requires_not_ready",
    )
    assert (await first.snapshot(resource_id)).ledger_revision == (
        ready_snapshot.ledger_revision
    )

    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before_expand = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        children_before = (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-a"), 0, -1),
            await raw.lrange(keys.queue, 0, -1),
            await raw.get(keys.transition),
        )
        expanded = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before_expand.ledger_revision,
            expected_ledger_incarnation=before_expand.ledger_incarnation,
            backend_memberships=target_with_pending,
            evolution_id="add-pending-b",
        )
        assert expanded.status == "evolved"
        target_retiring = (
            GPUBackendDomainMember("backend-a", 2, "retiring"),
            GPUBackendDomainMember("backend-b", 1, "active"),
            GPUBackendDomainMember("backend-c", 2, "retiring"),
        )
        retired = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=expanded.ledger_revision,
            expected_ledger_incarnation=expanded.ledger_incarnation,
            backend_memberships=target_retiring,
            evolution_id="activate-b-retire-a-c",
        )
        assert retired.status == "evolved"
        assert (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-a"), 0, -1),
            await raw.lrange(keys.queue, 0, -1),
            await raw.get(keys.transition),
        ) == children_before

        inactive_admission = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "retiring-new-work"),
        )
        assert inactive_admission.status == "not_ready"
        blocked_reconcile_snapshot = await first.snapshot(resource_id)
        blocked_reconcile = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=blocked_reconcile_snapshot.ledger_revision,
            expected_ledger_incarnation=blocked_reconcile_snapshot.ledger_incarnation,
            backend_memberships=target_retiring,
            allocations=blocked_reconcile_snapshot.allocations,
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="blocked-by-retiring-work",
        )
        assert blocked_reconcile.status == "busy"

        assert (
            await first.sweep_expired_leases(
                resource_id,
                backend_id="backend-a",
            )
        ) == (0, 1)
        assert (
            await first.heartbeat_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
                heartbeat_ttl_ms=5_000,
            )
        ).status == "heartbeated"
        assert (
            await first.release_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
            )
        ).status == "released"
        backend_position = await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="backend-ticket-a",
            card_queue=False,
        )
        card_position = await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="card-ticket-a",
            card_queue=True,
        )
        assert (backend_position.status, backend_position.position) == ("queued", 1)
        assert (card_position.status, card_position.position) == ("queued", 1)
        assert (
            await first.cancel_queue_ticket(
                resource_id,
                backend_id="backend-a",
                ticket_id="backend-ticket-a",
                owner_id="owner-a",
                card_queue=False,
            )
        ).status == "cancelled"
        assert (
            await first.cancel_queue_ticket(
                resource_id,
                backend_id="backend-a",
                ticket_id="card-ticket-a",
                owner_id="owner-a",
                card_queue=True,
            )
        ).status == "cancelled"

        assert (
            await first.acquire_transition_owner(
                resource_id,
                backend_id="backend-c",
                membership_epoch=2,
                owner_id="transition-c",
                generation="2",
                operation="evict",
                ttl_ms=30_000,
            )
        ).status == "acquired"
        assert (
            await first.heartbeat_transition_owner(
                resource_id,
                backend_id="backend-c",
                owner_id="transition-c",
                generation="2",
                operation="evict",
                ttl_ms=30_000,
            )
        ).status == "renewed"
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-c",
                expected_generation="1",
                target_state=GPUAllocationState.DRAINING,
                next_generation="2",
                transition_owner_id="transition-c",
                transition_operation="evict",
            )
        ).status == "transitioned"
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-c",
                expected_generation="2",
                target_state=GPUAllocationState.UNLOADING,
                transition_owner_id="transition-c",
                transition_operation="evict",
            )
        ).status == "transitioned"
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-c",
                expected_generation="2",
                target_state=GPUAllocationState.UNLOADED,
                transition_owner_id="transition-c",
                transition_operation="evict",
            )
        ).status == "transitioned"
        assert (
            await first.release_transition_owner(
                resource_id,
                backend_id="backend-c",
                owner_id="transition-c",
                generation="2",
                operation="evict",
            )
        ).status == "released"

        cleanup_snapshot = await first.snapshot(resource_id)
        target_allocations = tuple(
            replace(allocation, state=GPUAllocationState.UNLOADED)
            if allocation.backend_id == "backend-a"
            else allocation
            for allocation in cleanup_snapshot.allocations
        )
        reconciled = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=cleanup_snapshot.ledger_revision,
            expected_ledger_incarnation=cleanup_snapshot.ledger_incarnation,
            backend_memberships=target_retiring,
            allocations=target_allocations,
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="retiring-cleanup-complete",
        )
        assert reconciled.status == "reconciled"
        final = await first.snapshot(resource_id)
        assert final.ready is True
        assert final.backend_ids == ("backend-a", "backend-b", "backend-c")
        assert final.active_backend_ids == ("backend-b",)
        assert final.leases == ()
    finally:
        await raw.aclose()


@pytest.mark.parametrize("child_kind", ("lease", "queue", "card_queue"))
@pytest.mark.asyncio
async def test_domain_evolution_rejects_nonempty_new_backend_children_atomically(
    redis_stores,
    child_kind: str,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-new-child-{child_kind}/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        if child_kind == "lease":
            child_key = keys.leases("backend-b")
        elif child_kind == "queue":
            child_key = keys.backend_queue("backend-b")
        else:
            child_key = keys.queue
        if child_kind == "lease":
            await raw.hset(child_key, "orphan", "{}")
        elif child_kind == "card_queue":
            await raw.rpush(
                child_key,
                json.dumps(
                    {
                        "ticket_id": "orphan-card-ticket",
                        "backend_id": "backend-b",
                        "owner_id": "owner-b",
                        "kind": "card",
                        "membership_epoch": "1",
                        "enqueued_at_ms": 1,
                        "expires_at_ms": _future_reconcile_deadline_ms(),
                    },
                    separators=(",", ":"),
                ),
            )
            await raw.hset(keys.card, "card_queue_count", "1")
        else:
            await raw.rpush(child_key, "{}")
        card_before = await raw.hgetall(keys.card)
        rejected = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=(
                GPUBackendDomainMember("backend-a", 1, "active"),
                GPUBackendDomainMember("backend-b", 1, "pending"),
            ),
            evolution_id=f"reject-{child_kind}",
        )
        assert (rejected.status, rejected.reason) == (
            "partial_state",
            "new_backend_children_present",
        )
        assert await raw.hgetall(keys.card) == card_before
        assert await raw.exists(child_key) == 1
    finally:
        await raw.aclose()


@pytest.mark.parametrize(
    ("corrupt_kind", "expected_reason"),
    (
        ("card_queue", "queue_domain_exceeded"),
        ("backend_queue", "queue_domain_exceeded"),
        ("aggregate_queue", "queue_domain_exceeded"),
        ("lease", "lease_domain_exceeded"),
    ),
)
@pytest.mark.asyncio
async def test_domain_evolution_rejects_oversized_child_domains_before_scanning(
    redis_stores,
    corrupt_kind: str,
    expected_reason: str,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-oversized-evolution-{corrupt_kind}/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        if corrupt_kind == "card_queue":
            await raw.rpush(keys.queue, *("{}" for _ in range(10_001)))
            await raw.hset(keys.card, "card_queue_count", "10001")
        elif corrupt_kind == "backend_queue":
            await raw.rpush(
                keys.backend_queue("backend-a"),
                *("{}" for _ in range(10_001)),
            )
            await raw.hset(
                keys.card,
                "backend_queue_counts",
                '{"backend-a":10001}',
            )
        elif corrupt_kind == "aggregate_queue":
            await raw.rpush(keys.queue, *("{}" for _ in range(9_999)))
            await raw.rpush(keys.backend_queue("backend-a"), "{}", "{}")
            await raw.hset(
                keys.card,
                mapping={
                    "card_queue_count": "9999",
                    "backend_queue_counts": '{"backend-a":2}',
                },
            )
        else:
            await raw.hset(
                keys.leases("backend-a"),
                mapping={f"lease-{index}": "{}" for index in range(10_001)},
            )
            await raw.hset(keys.card, "lease_counts", '{"backend-a":10001}')
        card_before = await raw.hgetall(keys.card)
        children_before = (
            await raw.llen(keys.queue),
            await raw.llen(keys.backend_queue("backend-a")),
            await raw.hlen(keys.leases("backend-a")),
        )
        with pytest.raises(GPUArbiterStoreError, match="GPU ledger decode failed"):
            await first.snapshot(resource_id)
        rejected = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=(
                GPUBackendDomainMember("backend-a", 1, "active"),
                GPUBackendDomainMember("backend-b", 1, "pending"),
            ),
            evolution_id=f"oversized-{corrupt_kind}",
        )
        assert (rejected.status, rejected.reason) == (
            "ledger_corrupt",
            expected_reason,
        )
        assert await raw.hgetall(keys.card) == card_before
        assert (
            await raw.llen(keys.queue),
            await raw.llen(keys.backend_queue("backend-a")),
            await raw.hlen(keys.leases("backend-a")),
        ) == children_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_domain_evolution_rejects_all_domain_shrink_without_mutation(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-domain-shrink/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a", "backend-b"),
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_before = await raw.hgetall(keys.card)
        rejected = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            evolution_id="shrink-forbidden",
        )
        assert (rejected.status, rejected.reason) == (
            "config_mismatch",
            "backend_domain_shrink_forbidden",
        )
        assert await raw.hgetall(keys.card) == card_before
    finally:
        await raw.aclose()


@pytest.mark.parametrize(
    ("current_memberships", "target_memberships", "expected_reason"),
    (
        (
            (GPUBackendDomainMember("backend-a", 2, "active"),),
            (GPUBackendDomainMember("backend-a", 1, "active"),),
            "membership_transition_invalid",
        ),
        (
            (GPUBackendDomainMember("backend-a", 1, "active"),),
            (GPUBackendDomainMember("backend-a", 3, "retiring"),),
            "membership_transition_invalid",
        ),
        (
            (GPUBackendDomainMember("backend-a", 1, "active"),),
            (GPUBackendDomainMember("backend-a", 1, "pending"),),
            "membership_transition_invalid",
        ),
        (
            (GPUBackendDomainMember("backend-a", 1, "active"),),
            (GPUBackendDomainMember("backend-a", 1, "retiring"),),
            "membership_transition_invalid",
        ),
        (
            (
                GPUBackendDomainMember("backend-a", 1, "active"),
                GPUBackendDomainMember("backend-b", 2, "retiring"),
            ),
            (
                GPUBackendDomainMember("backend-a", 1, "active"),
                GPUBackendDomainMember("backend-b", 2, "active"),
            ),
            "membership_transition_invalid",
        ),
        (
            (GPUBackendDomainMember("backend-a", 1, "active"),),
            (
                GPUBackendDomainMember("backend-a", 1, "active"),
                GPUBackendDomainMember("backend-b", 2, "pending"),
            ),
            "new_membership_must_start_pending",
        ),
    ),
    ids=(
        "epoch-rollback",
        "epoch-jump",
        "active-to-pending",
        "retiring-without-epoch-bump",
        "retiring-resurrection",
        "new-pending-wrong-epoch",
    ),
)
@pytest.mark.asyncio
async def test_domain_evolution_rejects_invalid_membership_transitions_atomically(
    redis_stores,
    current_memberships: tuple[GPUBackendDomainMember, ...],
    target_memberships: tuple[GPUBackendDomainMember, ...],
    expected_reason: str,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-invalid-transition-{uuid.uuid4().hex}/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=current_memberships,
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    backend_ids = sorted(
        {member.backend_id for member in (*current_memberships, *target_memberships)}
    )
    raw = Redis.from_url(_redis_url(), decode_responses=True)

    async def resource_state() -> tuple:
        backend_children = []
        for backend_id in backend_ids:
            backend_children.append(
                (
                    backend_id,
                    await raw.hgetall(keys.leases(backend_id)),
                    await raw.lrange(keys.backend_queue(backend_id), 0, -1),
                )
            )
        return (
            await raw.hgetall(keys.card),
            await raw.hgetall(keys.allocations),
            await raw.lrange(keys.queue, 0, -1),
            await raw.get(keys.transition),
            tuple(backend_children),
        )

    try:
        state_before = await resource_state()
        rejected = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=target_memberships,
            evolution_id=f"reject-{expected_reason}-{uuid.uuid4().hex}",
        )
        assert (rejected.status, rejected.reason) == (
            "config_mismatch",
            expected_reason,
        )
        assert await resource_state() == state_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_domain_evolution_exact_retry_is_read_only_after_response_loss(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-evolution-retry/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before = await first.snapshot(resource_id)
    target = (GPUBackendDomainMember("backend-a", 2, "retiring"),)
    created = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=target,
        evolution_id="response-loss-evolution",
    )
    assert created.status == "evolved"
    assert created.ledger_revision == before.ledger_revision + 1
    assert created.idempotent is False
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_after_create = await raw.hgetall(keys.card)
        retried = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=target,
            evolution_id="response-loss-evolution",
        )
        assert retried.status == "evolved"
        assert retried.idempotent is True
        assert retried.ledger_revision == created.ledger_revision
        assert await raw.hgetall(keys.card) == card_after_create

        await raw.hset(keys.card, "ledger_incarnation", "replacement-incarnation")
        incarnation_fenced = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=target,
            evolution_id="response-loss-evolution",
        )
        assert (incarnation_fenced.status, incarnation_fenced.reason) == (
            "stale_revision",
            "ledger_incarnation_changed",
        )
        await raw.hset(keys.card, "ledger_incarnation", created.ledger_incarnation)
        await raw.hset(keys.card, "ledger_version", "1")
        schema_fenced = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=target,
            evolution_id="response-loss-evolution",
        )
        assert (schema_fenced.status, schema_fenced.reason) == (
            "ledger_corrupt",
            "legacy_schema_requires_proof_reset",
        )
        await raw.hset(keys.card, "ledger_version", "3")
        assert await raw.hgetall(keys.card) == card_after_create

        conflict = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=(GPUBackendDomainMember("backend-a", 3, "retiring"),),
            evolution_id="response-loss-evolution",
        )
        assert (conflict.status, conflict.reason) == (
            "config_mismatch",
            "evolution_id_conflict",
        )
        assert await raw.hgetall(keys.card) == card_after_create

        unchanged = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=created.ledger_revision,
            expected_ledger_incarnation=created.ledger_incarnation,
            backend_memberships=target,
            evolution_id="already-current-domain",
        )
        assert unchanged.status == "unchanged"
        assert unchanged.idempotent is True
        assert unchanged.ledger_revision == created.ledger_revision
        assert await raw.hgetall(keys.card) == card_after_create

        progressed = await first.mark_card_not_ready(
            resource_id,
            100,
            reason="later_observation",
        )
        assert progressed.ledger_revision == created.ledger_revision + 1
        card_after_progress = await raw.hgetall(keys.card)
        fenced_retry = await first.evolve_backend_domains(
            resource_id,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=target,
            evolution_id="response-loss-evolution",
        )
        assert (fenced_retry.status, fenced_retry.reason) == (
            "stale_revision",
            "ledger_changed_after_evolution",
        )
        assert await raw.hgetall(keys.card) == card_after_progress
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_domain_evolution_is_isolated_by_full_resource_id(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_a = "node-shared/index:0"
    resource_b = "node-shared/index:1"
    for store, resource_id in ((first, resource_a), (second, resource_b)):
        await _bootstrap_empty_card(
            store,
            resource_id,
            100,
            memberships=_memberships("backend-a"),
        )
        await store.mark_card_not_ready(
            resource_id,
            100,
            reason="membership_change",
        )
    before_a = await first.snapshot(resource_a)
    before_b = await second.snapshot(resource_b)
    target = (
        GPUBackendDomainMember("backend-a", 1, "active"),
        GPUBackendDomainMember("backend-b", 1, "pending"),
    )
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(first.keys(resource_a).leases("backend-b"), "orphan", "{}")
        failed_a = await first.evolve_backend_domains(
            resource_a,
            expected_ledger_revision=before_a.ledger_revision,
            expected_ledger_incarnation=before_a.ledger_incarnation,
            backend_memberships=target,
            evolution_id="same-evolution-id",
        )
        evolved_b = await second.evolve_backend_domains(
            resource_b,
            expected_ledger_revision=before_b.ledger_revision,
            expected_ledger_incarnation=before_b.ledger_incarnation,
            backend_memberships=target,
            evolution_id="same-evolution-id",
        )
        assert failed_a.status == "partial_state"
        assert evolved_b.status == "evolved"
        after_a = await first.snapshot(resource_a)
        after_b = await second.snapshot(resource_b)
        assert after_a.ledger_revision == before_a.ledger_revision
        assert after_a.backend_ids == ("backend-a",)
        assert after_b.ledger_revision == before_b.ledger_revision + 1
        assert after_b.backend_ids == ("backend-a", "backend-b")
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_domain_evolution_fences_revision_and_incarnation(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-evolution-cas/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    stale = await first.snapshot(resource_id)
    await first.mark_card_not_ready(resource_id, 100, reason="newer_observation")
    current = await first.snapshot(resource_id)
    target = (
        GPUBackendDomainMember("backend-a", 1, "active"),
        GPUBackendDomainMember("backend-b", 1, "pending"),
    )
    stale_revision = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=stale.ledger_revision,
        expected_ledger_incarnation=stale.ledger_incarnation,
        backend_memberships=target,
        evolution_id="stale-revision",
    )
    assert (stale_revision.status, stale_revision.reason) == (
        "stale_revision",
        "ledger_revision_changed",
    )
    skipped_pending = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=current.ledger_revision,
        expected_ledger_incarnation=current.ledger_incarnation,
        backend_memberships=_memberships("backend-a", "backend-b"),
        evolution_id="new-active-without-pending",
    )
    assert (skipped_pending.status, skipped_pending.reason) == (
        "config_mismatch",
        "new_membership_must_start_pending",
    )
    stale_incarnation = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=current.ledger_revision,
        expected_ledger_incarnation="stale-incarnation",
        backend_memberships=target,
        evolution_id="stale-incarnation",
    )
    assert (stale_incarnation.status, stale_incarnation.reason) == (
        "stale_revision",
        "ledger_incarnation_changed",
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == current.ledger_revision
    assert after.backend_ids == ("backend-a",)


@pytest.mark.asyncio
async def test_legacy_ledger_v1_is_latched_not_ready_without_in_place_upgrade(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-legacy-v1/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.card, "ledger_version", "1")
        await raw.hdel(
            keys.card,
            "membership_domain",
            "membership_domain_fingerprint",
            "active_backend_domain",
            "active_backend_domain_fingerprint",
        )
        revision_before = int(await raw.hget(keys.card, "ledger_revision"))
        latched = await first.mark_card_not_ready(
            resource_id,
            100,
            reason="operator_fail_close",
        )
        assert (latched.status, latched.reason) == (
            "ledger_corrupt",
            "legacy_schema_requires_proof_reset",
        )
        assert latched.ledger_revision == revision_before + 1
        assert await raw.hget(keys.card, "ledger_version") == "1"
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
        assert await raw.hmget(
            keys.card,
            "membership_domain",
            "membership_domain_fingerprint",
            "active_backend_domain",
            "active_backend_domain_fingerprint",
        ) == [None, None, None, None]

        rejected_reconcile = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=latched.ledger_revision,
            expected_ledger_incarnation=latched.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            allocations=(),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="legacy-must-not-upgrade",
        )
        assert rejected_reconcile.status == "ledger_corrupt"
        assert await raw.hget(keys.card, "ledger_version") == "1"
        assert await raw.hget(keys.card, "membership_domain") is None
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_legacy_ledger_v2_is_latched_not_ready_without_in_place_upgrade(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-legacy-v2/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.card, "ledger_version", "2")
        card_before = await raw.hgetall(keys.card)
        latched = await first.mark_card_not_ready(
            resource_id,
            100,
            reason="operator_fail_close",
        )

        assert (latched.status, latched.reason) == (
            "ledger_corrupt",
            "legacy_schema_requires_proof_reset",
        )
        assert await raw.hget(keys.card, "ledger_version") == "2"
        card_after = await raw.hgetall(keys.card)
        assert card_after["bootstrap_state"] == "not_ready"
        assert card_after["reconcile_deadline_ms"] == "0"
        assert card_after["not_ready_reason"] == "legacy_schema_requires_proof_reset"
        assert set(card_after) == set(card_before)

        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=latched.ledger_revision,
            expected_ledger_incarnation=latched.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            reset_id="migrate-v2-to-v3",
        )
        assert prepared.status == "prepared"
        assert await raw.hget(keys.card, "ledger_version") == "3"
        committed = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="migrate-v2-to-v3",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            allocations=(),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=hashlib.sha256(b"migrate-v2-to-v3").hexdigest(),
        )
        assert (committed.status, committed.ledger_revision) == ("reconciled", 2)
        assert await raw.hget(keys.card, "ledger_version") == "3"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_legacy_v2_prepared_proof_reset_resumes_without_in_place_upgrade(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-legacy-v2-prepared/index:0"
    memberships = _memberships("backend-a")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    before = await first.snapshot(resource_id)
    prepared = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=memberships,
        reset_id="resume-v2-prepared",
    )
    assert prepared.status == "prepared"

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.card, "ledger_version", "2")
        card_before_resume = await raw.hgetall(keys.card)

        context = await second.prepared_proof_reset(resource_id)
        assert context is not None
        assert (
            context.reset_id,
            context.ledger_revision,
            context.ledger_incarnation,
        ) == (
            "resume-v2-prepared",
            prepared.ledger_revision,
            prepared.ledger_incarnation,
        )
        replayed = await second.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="resume-v2-prepared",
        )
        assert (replayed.status, replayed.idempotent) == ("prepared", True)
        assert await raw.hgetall(keys.card) == card_before_resume

        cooldown_deadline_ms = context.prepared_at_ms + 30_000
        committed = await second.commit_proof_reset(
            resource_id,
            100,
            reset_id=context.reset_id,
            expected_reset_revision=context.ledger_revision,
            expected_reset_incarnation=context.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(
                _reconcile_allocation(
                    state=GPUAllocationState.RESIDENT,
                    not_evict_before_ms=cooldown_deadline_ms,
                ),
            ),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=hashlib.sha256(b"resume-v2-prepared").hexdigest(),
        )

        assert (committed.status, committed.ledger_revision) == ("reconciled", 2)
        assert await raw.hget(keys.card, "ledger_version") == "3"
        snapshot = await first.snapshot(resource_id)
        assert snapshot.allocations[0].not_evict_before_ms == cooldown_deadline_ms
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_incomplete_v3_domains_are_not_filled_by_mark_not_ready(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-incomplete-v2/index:0"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hdel(keys.card, "membership_domain")
        revision_before = int(await raw.hget(keys.card, "ledger_revision"))
        rejected = await first.mark_card_not_ready(
            resource_id,
            100,
            reason="operator_fail_close",
        )
        assert (rejected.status, rejected.reason) == (
            "ledger_corrupt",
            "ledger_schema_incomplete",
        )
        assert rejected.ledger_revision == revision_before + 1
        assert await raw.hget(keys.card, "ledger_version") == "3"
        assert await raw.hget(keys.card, "membership_domain") is None
        assert await raw.hget(keys.card, "active_backend_domain") == '["backend-a"]'
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_full_flush_revision_aba_is_fenced_by_incarnation(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-full-flush-aba/index:0"
    allocation = _reconcile_allocation()
    first_bootstrap = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(allocation,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="old-bootstrap",
    )
    old_snapshot = await first.snapshot(resource_id)
    old_target = replace(old_snapshot.allocations[0], state=GPUAllocationState.UNLOADED)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.delete(
            keys.card,
            keys.allocations,
            keys.queue,
            keys.transition,
            *(
                key
                for backend_id in _TEST_BACKEND_DOMAIN
                for key in (
                    keys.leases(backend_id),
                    keys.backend_queue(backend_id),
                )
            ),
        )
        replacement = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=(allocation,),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="new-bootstrap",
        )
        assert replacement.ledger_revision == first_bootstrap.ledger_revision
        assert replacement.ledger_incarnation != first_bootstrap.ledger_incarnation

        stale = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=old_snapshot.ledger_revision,
            expected_ledger_incarnation=old_snapshot.ledger_incarnation,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=(old_target,),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="delayed-old-repair",
        )
        assert stale.status == "stale_revision"
        assert stale.reason == "ledger_incarnation_changed"
        current = await first.snapshot(resource_id)
        assert current.ledger_incarnation == replacement.ledger_incarnation
        assert current.committed_mb == 60
        assert current.allocations[0].state is GPUAllocationState.UNKNOWN
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_partial_flush_state_cannot_be_bootstrapped_over(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.leases("backend-a"), "orphan", "{}")
        result = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=_memberships("backend-a"),
            allocations=(_reconcile_allocation(),),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="partial-flush",
        )
        assert result.status == "partial_state"
        assert await raw.exists(keys.card) == 0
        assert await raw.hget(keys.leases("backend-a"), "orphan") == "{}"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_recovers_card_missing_partial_children(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-proof-reset-partial/index:0"
    memberships = _memberships("backend-a")
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.leases("backend-a"), "orphan", "not-json")

        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="partial-reset",
        )
        assert (prepared.status, prepared.ready, prepared.ledger_revision) == (
            "prepared",
            False,
            1,
        )
        assert await raw.hget(keys.leases("backend-a"), "orphan") == "not-json"
        with pytest.raises(
            GPUArbiterStoreError, match="gpu_arbiter_proof_reset_in_progress"
        ):
            await first.snapshot(resource_id)
        context = await second.prepared_proof_reset(resource_id)
        assert context is not None
        assert (
            context.reset_id,
            context.ledger_revision,
            context.ledger_incarnation,
            context.backend_memberships,
        ) == (
            "partial-reset",
            prepared.ledger_revision,
            prepared.ledger_incarnation,
            memberships,
        )

        committed = await second.commit_proof_reset(
            resource_id,
            context.allocatable_mb,
            reset_id=context.reset_id,
            expected_reset_revision=context.ledger_revision,
            expected_reset_incarnation=context.ledger_incarnation,
            backend_memberships=context.backend_memberships,
            allocations=(
                _reconcile_allocation(
                    generation=None,
                    state=GPUAllocationState.UNKNOWN,
                ),
            ),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=hashlib.sha256(b"partial-proof").hexdigest(),
        )
        assert (
            committed.status,
            committed.reason,
            committed.ready,
            committed.ledger_revision,
        ) == (
            "not_ready",
            "proof_incomplete",
            False,
            2,
        )
        assert committed.ledger_incarnation == prepared.ledger_incarnation
        assert await raw.exists(keys.leases("backend-a")) == 0
        snapshot = await first.snapshot(resource_id)
        assert snapshot.committed_mb == 60
        assert snapshot.ready is False
        assert snapshot.allocations[0].generation is None
        assert snapshot.allocations[0].state is GPUAllocationState.UNKNOWN
        assert await first.key_ttls(resource_id, backend_id="backend-a") == (-1, -2)
        assert await second.prepared_proof_reset(resource_id) is None
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_retries_are_read_only_and_fence_new_work(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-retry/index:0"
    memberships = _memberships("backend-a")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    original = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=original.ledger_revision,
            expected_ledger_incarnation=original.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="retry-reset",
        )
        prepared_card = await raw.hgetall(keys.card)
        assert prepared_card["ledger_version"] == "3"
        assert prepared_card["ledger_revision"] == "1"
        retry = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=original.ledger_revision,
            expected_ledger_incarnation=original.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="retry-reset",
        )
        assert (retry.status, retry.idempotent) == ("prepared", True)
        assert await raw.hgetall(keys.card) == prepared_card

        conflict = await first.begin_proof_reset(
            resource_id,
            101,
            expected_ledger_revision=original.ledger_revision,
            expected_ledger_incarnation=original.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="retry-reset",
        )
        assert (conflict.status, conflict.reason) == (
            "config_mismatch",
            "proof_reset_id_conflict",
        )
        assert await raw.hgetall(keys.card) == prepared_card

        allocation = replace(
            _reconcile_allocation(
                state=GPUAllocationState.RESIDENT,
                not_evict_before_ms=1234,
            ),
            evictable=True,
        )
        deadline = _future_reconcile_deadline_ms()
        proof_fingerprint = hashlib.sha256(b"retry-proof").hexdigest()
        await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="retry-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=deadline,
            proof_fingerprint=proof_fingerprint,
        )
        committed_card = await raw.hgetall(keys.card)
        assert committed_card["ledger_version"] == "3"
        assert committed_card["ledger_revision"] == "2"
        commit_retry = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="retry-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=deadline,
            proof_fingerprint=proof_fingerprint,
        )
        assert (commit_retry.status, commit_retry.idempotent) == (
            "reconciled",
            True,
        )
        assert await raw.hgetall(keys.card) == committed_card
        assert (await first.snapshot(resource_id)).allocations[
            0
        ].not_evict_before_ms == 1234
        begin_retry = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=original.ledger_revision,
            expected_ledger_incarnation=original.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="retry-reset",
        )
        assert (begin_retry.status, begin_retry.reason) == (
            "stale_revision",
            "proof_reset_already_committed",
        )
        assert await raw.hgetall(keys.card) == committed_card
        commit_conflict = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="retry-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(replace(allocation, budget_mb=61),),
            ready=True,
            evidence_deadline_ms=deadline,
            proof_fingerprint=proof_fingerprint,
        )
        assert (commit_conflict.status, commit_conflict.reason) == (
            "config_mismatch",
            "proof_reset_id_conflict",
        )
        assert await raw.hgetall(keys.card) == committed_card

        allocation_raw = await raw.hget(keys.allocations, "backend-a")
        assert allocation_raw is not None
        await raw.hdel(keys.allocations, "backend-a")
        corrupt_retry = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="retry-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=deadline,
            proof_fingerprint=proof_fingerprint,
        )
        assert (corrupt_retry.status, corrupt_retry.reason) == (
            "ledger_corrupt",
            "proof_reset_committed_state_invalid",
        )
        assert await raw.hgetall(keys.card) == committed_card
        await raw.hset(keys.allocations, "backend-a", allocation_raw)

        admitted = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "post-reset-lease"),
        )
        assert admitted.admitted
        stale_retry = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="retry-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=deadline,
            proof_fingerprint=proof_fingerprint,
        )
        assert (stale_retry.status, stale_retry.reason) == (
            "stale_revision",
            "ledger_changed_after_proof_reset",
        )
        assert await raw.hlen(keys.leases("backend-a")) == 1
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_retry_recomputes_expired_ready_deadline(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-expired-retry/index:0"
    memberships = _memberships("backend-a")
    prepared = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=memberships,
        reset_id="expiring-reset",
    )
    deadline = _future_reconcile_deadline_ms(1_000)
    proof_fingerprint = hashlib.sha256(b"expiring-proof").hexdigest()
    committed = await first.commit_proof_reset(
        resource_id,
        100,
        reset_id="expiring-reset",
        expected_reset_revision=prepared.ledger_revision,
        expected_reset_incarnation=prepared.ledger_incarnation,
        backend_memberships=memberships,
        allocations=(),
        ready=True,
        evidence_deadline_ms=deadline,
        proof_fingerprint=proof_fingerprint,
    )
    assert committed.ready is True
    await asyncio.sleep(1.1)
    retry = await first.commit_proof_reset(
        resource_id,
        100,
        reset_id="expiring-reset",
        expected_reset_revision=prepared.ledger_revision,
        expected_reset_incarnation=prepared.ledger_incarnation,
        backend_memberships=memberships,
        allocations=(),
        ready=True,
        evidence_deadline_ms=deadline,
        proof_fingerprint=proof_fingerprint,
    )
    assert (retry.status, retry.reason, retry.ready, retry.idempotent) == (
        "not_ready",
        "reconcile_expired",
        False,
        True,
    )


@pytest.mark.asyncio
async def test_prepared_proof_reset_blocks_every_mutation_family(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-freeze/index:0"
    memberships = _memberships("backend-a")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    await _admit_resident(first, resource_id)
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="existing-ticket",
            owner_id="queue-owner",
            ttl_ms=30_000,
        )
    ).status == "queued"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    before = await first.snapshot(resource_id)
    prepared = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=memberships,
        reset_id="freeze-reset",
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_before = await raw.hgetall(keys.card)
        children_before = (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-a"), 0, -1),
            await raw.get(keys.transition),
        )
        assert (
            await first.mark_card_not_ready(
                resource_id,
                100,
                reason="operator-fail-close",
            )
        ).reason == "proof_reset_in_progress"
        with pytest.raises(
            GPUArbiterStoreError, match="gpu_arbiter_proof_reset_in_progress"
        ):
            await first.configure_card(resource_id, 100, ready=False)
        assert (
            await first.reconcile_card(
                resource_id,
                100,
                expected_ledger_revision=prepared.ledger_revision,
                expected_ledger_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                allocations=(),
                lease_cleanup=None,
                ready=True,
                reconcile_deadline_ms=_future_reconcile_deadline_ms(),
                repair_id="blocked-repair",
            )
        ).reason == "proof_reset_in_progress"
        assert (
            await first.evolve_backend_domains(
                resource_id,
                expected_ledger_revision=prepared.ledger_revision,
                expected_ledger_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                evolution_id="blocked-evolution",
            )
        ).reason == "proof_reset_in_progress"
        assert (
            await first.admit(
                resource_id,
                **_admission_kwargs("backend-a", "blocked-lease"),
            )
        ).status == "not_ready"
        assert (
            await first.heartbeat_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
                heartbeat_ttl_ms=5_000,
            )
        ).status == "not_ready"
        assert (
            await first.release_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
            )
        ).status == "not_ready"
        with pytest.raises(
            GPUArbiterStoreError, match="gpu_arbiter_proof_reset_in_progress"
        ):
            await first.sweep_expired_leases(
                resource_id,
                backend_id="backend-a",
            )
        assert (
            await first.queue_position(
                resource_id,
                backend_id="backend-a",
                ticket_id="existing-ticket",
                card_queue=False,
            )
        ).status == "not_ready"
        assert (
            await first.cancel_queue_ticket(
                resource_id,
                backend_id="backend-a",
                ticket_id="existing-ticket",
                owner_id="queue-owner",
                card_queue=False,
            )
        ).status == "not_ready"
        assert (
            await first.heartbeat_transition_owner(
                resource_id,
                backend_id="backend-a",
                owner_id="transition-owner",
                generation="2",
                operation="evict",
                ttl_ms=5_000,
            )
        ).status == "not_ready"
        assert (
            await first.release_transition_owner(
                resource_id,
                backend_id="backend-a",
                owner_id="transition-owner",
                generation="2",
                operation="evict",
            )
        ).status == "not_ready"
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-a",
                expected_generation="1",
                target_state=GPUAllocationState.DRAINING,
                transition_owner_id="transition-owner",
                transition_operation="evict",
            )
        ).status == "not_ready"
        with pytest.raises(
            GPUArbiterStoreError, match="gpu_arbiter_proof_reset_in_progress"
        ):
            await first.snapshot(resource_id)

        assert await raw.hgetall(keys.card) == card_before
        assert (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-a"), 0, -1),
            await raw.get(keys.transition),
        ) == children_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_validates_cas_domain_and_evidence_deadline(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-validation/index:0"
    memberships = _memberships("backend-a", "backend-b")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    snapshot = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_before = await raw.hgetall(keys.card)
        missing_cas = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="missing-cas-reset",
        )
        assert (missing_cas.status, missing_cas.reason) == (
            "stale_revision",
            "proof_reset_cas_required",
        )
        assert await raw.hgetall(keys.card) == card_before
        stale = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=snapshot.ledger_revision + 1,
            expected_ledger_incarnation=snapshot.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="stale-reset",
        )
        assert stale.status == "stale_revision"
        assert await raw.hgetall(keys.card) == card_before

        incomplete_domain = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=snapshot.ledger_revision,
            expected_ledger_incarnation=snapshot.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            reset_id="shrunk-reset",
        )
        assert (incomplete_domain.status, incomplete_domain.reason) == (
            "config_mismatch",
            "stored_backend_domain_exceeds_closed_domain",
        )
        assert await raw.hgetall(keys.card) == card_before

        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=snapshot.ledger_revision,
            expected_ledger_incarnation=snapshot.ledger_incarnation,
            backend_memberships=memberships,
            reset_id="deadline-reset",
        )
        prepared_card = await raw.hgetall(keys.card)
        allocation = _reconcile_allocation()
        proof_fingerprint = hashlib.sha256(b"deadline-proof").hexdigest()
        await raw.hdel(keys.card, "proof_reset_prepared_at_ms")
        with pytest.raises(
            GPUArbiterStoreError, match="proof reset context decode failed"
        ):
            await first.prepared_proof_reset(resource_id)
        corrupt_marker = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="deadline-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=proof_fingerprint,
        )
        assert (corrupt_marker.status, corrupt_marker.reason) == (
            "ledger_corrupt",
            "proof_reset_marker_invalid",
        )
        await raw.hset(
            keys.card,
            "proof_reset_prepared_at_ms",
            prepared_card["proof_reset_prepared_at_ms"],
        )
        wrong_context = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="deadline-reset",
            expected_reset_revision=prepared.ledger_revision + 1,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=proof_fingerprint,
        )
        assert (wrong_context.status, wrong_context.reason) == (
            "stale_revision",
            "proof_reset_context_changed",
        )
        assert await raw.hgetall(keys.card) == prepared_card
        changed_domain = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="deadline-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=_memberships("backend-a", "backend-b", epoch=2),
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=proof_fingerprint,
        )
        assert (changed_domain.status, changed_domain.reason) == (
            "config_mismatch",
            "proof_reset_domain_changed",
        )
        assert await raw.hgetall(keys.card) == prepared_card
        with pytest.raises(ValueError, match="unknown allocations cannot be evictable"):
            await first.commit_proof_reset(
                resource_id,
                100,
                reset_id="deadline-reset",
                expected_reset_revision=prepared.ledger_revision,
                expected_reset_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                allocations=(replace(allocation, evictable=True),),
                ready=True,
                evidence_deadline_ms=_future_reconcile_deadline_ms(),
                proof_fingerprint=proof_fingerprint,
            )
        with pytest.raises(ValueError, match="null generation"):
            await first.commit_proof_reset(
                resource_id,
                100,
                reset_id="deadline-reset",
                expected_reset_revision=prepared.ledger_revision,
                expected_reset_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                allocations=(
                    replace(
                        allocation,
                        state=GPUAllocationState.RESIDENT,
                        generation=None,
                    ),
                ),
                ready=True,
                evidence_deadline_ms=_future_reconcile_deadline_ms(),
                proof_fingerprint=proof_fingerprint,
            )
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            await first.commit_proof_reset(
                resource_id,
                100,
                reset_id="deadline-reset",
                expected_reset_revision=prepared.ledger_revision,
                expected_reset_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                allocations=(allocation,),
                ready=True,
                evidence_deadline_ms=_future_reconcile_deadline_ms(),
                proof_fingerprint=proof_fingerprint.upper(),
            )
        with pytest.raises(ValueError, match="requires an active backend"):
            await first.commit_proof_reset(
                resource_id,
                100,
                reset_id="deadline-reset",
                expected_reset_revision=prepared.ledger_revision,
                expected_reset_incarnation=prepared.ledger_incarnation,
                backend_memberships=_memberships(
                    "backend-a",
                    "backend-b",
                    state="retiring",
                ),
                allocations=(),
                ready=True,
                evidence_deadline_ms=_future_reconcile_deadline_ms(),
                proof_fingerprint=proof_fingerprint,
            )
        with pytest.raises(ValueError, match="must be zero when ready is false"):
            await first.commit_proof_reset(
                resource_id,
                100,
                reset_id="deadline-reset",
                expected_reset_revision=prepared.ledger_revision,
                expected_reset_incarnation=prepared.ledger_incarnation,
                backend_memberships=memberships,
                allocations=(allocation,),
                ready=False,
                evidence_deadline_ms=_future_reconcile_deadline_ms(),
                proof_fingerprint=proof_fingerprint,
            )
        expired = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="deadline-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=1,
            proof_fingerprint=proof_fingerprint,
        )
        assert (expired.status, expired.reason) == (
            "not_ready",
            "proof_evidence_expired",
        )
        too_far = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="deadline-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(allocation,),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(400_000),
            proof_fingerprint=proof_fingerprint,
        )
        assert (too_far.status, too_far.reason) == (
            "not_ready",
            "proof_evidence_expired",
        )
        assert await raw.hgetall(keys.card) == prepared_card
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_can_repair_corrupt_core_without_cas_and_is_per_card(
    redis_stores,
) -> None:
    first, _ = redis_stores
    memberships = _memberships("backend-a")
    damaged_resource = "node-proof-reset-corrupt/index:0"
    healthy_resource = "node-proof-reset-corrupt/index:1"
    await _bootstrap_empty_card(
        first,
        damaged_resource,
        100,
        memberships=memberships,
    )
    await _bootstrap_empty_card(
        first,
        healthy_resource,
        100,
        memberships=memberships,
    )
    keys = first.keys(damaged_resource)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        old_incarnation = await raw.hget(keys.card, "ledger_incarnation")
        await raw.hdel(keys.card, "ledger_revision")
        await raw.hset(keys.card, "resource_id", "different-resource/index:0")
        wrong_identity = await first.begin_proof_reset(
            damaged_resource,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="wrong-identity-reset",
        )
        assert (wrong_identity.status, wrong_identity.reason) == (
            "ledger_corrupt",
            "resource_identity_mismatch",
        )
        await raw.hdel(keys.card, "resource_id")
        missing_identity = await first.begin_proof_reset(
            damaged_resource,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="missing-identity-reset",
        )
        assert (missing_identity.status, missing_identity.reason) == (
            "ledger_corrupt",
            "resource_identity_missing",
        )
        await raw.hset(keys.card, "resource_id", damaged_resource)
        prepared = await first.begin_proof_reset(
            damaged_resource,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="corrupt-reset",
        )
        assert prepared.status == "prepared"
        assert prepared.ledger_incarnation != old_incarnation
        assert (
            await first.admit(
                healthy_resource,
                **_admission_kwargs("backend-a", "healthy-lease"),
            )
        ).admitted

        committed = await first.commit_proof_reset(
            damaged_resource,
            100,
            reset_id="corrupt-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(),
            ready=False,
            evidence_deadline_ms=0,
            proof_fingerprint=hashlib.sha256(b"corrupt-proof").hexdigest(),
        )
        assert (committed.status, committed.reason, committed.ready) == (
            "not_ready",
            "proof_incomplete",
            False,
        )
        healthy = await first.snapshot(healthy_resource)
        assert healthy.ready is True
        assert len(healthy.leases) == 1
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_cleans_closed_domain_and_latches_overcommit(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-overcommit/index:0"
    memberships = (
        GPUBackendDomainMember("backend-a", 1, "active"),
        GPUBackendDomainMember("backend-b", 2, "retiring"),
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.set(keys.allocations, "wrong-type")
        await raw.rpush(keys.queue, "old-card-ticket")
        await raw.set(keys.transition, "old-transition")
        await raw.set(keys.leases("backend-a"), "wrong-type")
        await raw.rpush(keys.backend_queue("backend-a"), "old-a-ticket")
        await raw.hset(keys.leases("backend-b"), "old-b-lease", "invalid")
        await raw.rpush(keys.backend_queue("backend-b"), "old-b-ticket")

        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="overcommit-reset",
        )
        assert prepared.status == "prepared"
        assert await raw.get(keys.allocations) == "wrong-type"

        committed = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="overcommit-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(
                _reconcile_allocation(
                    backend_id="backend-a",
                    state=GPUAllocationState.RESIDENT,
                    budget_mb=70,
                ),
                _reconcile_allocation(
                    backend_id="backend-b",
                    state=GPUAllocationState.UNKNOWN,
                    generation=None,
                    budget_mb=40,
                ),
            ),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=hashlib.sha256(b"overcommit-proof").hexdigest(),
        )
        assert (committed.status, committed.reason, committed.committed_mb) == (
            "not_ready",
            "committed_exceeds_allocatable",
            110,
        )
        assert committed.ready is False
        assert await raw.exists(keys.queue, keys.transition) == 0
        assert (
            await raw.exists(
                keys.leases("backend-a"),
                keys.backend_queue("backend-a"),
                keys.leases("backend-b"),
                keys.backend_queue("backend-b"),
            )
            == 0
        )
        snapshot = await first.snapshot(resource_id)
        assert snapshot.backend_ids == ("backend-a", "backend-b")
        assert snapshot.active_backend_ids == ("backend-a",)
        assert snapshot.committed_mb == 110
        assert snapshot.ready is False
        assert await raw.ttl(keys.card) == -1
        assert await raw.ttl(keys.allocations) == -1
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_proof_reset_incarnation_fences_full_flush_revision_aba(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-proof-reset-aba/index:0"
    memberships = _memberships("backend-a")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=memberships,
    )
    assert (
        await first.mark_card_not_ready(
            resource_id,
            100,
            reason="force-revision-two",
        )
    ).ledger_revision == 2
    old = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.delete(
            keys.card,
            keys.allocations,
            keys.queue,
            keys.transition,
            keys.leases("backend-a"),
            keys.backend_queue("backend-a"),
        )
        prepared = await first.begin_proof_reset(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=memberships,
            reset_id="aba-reset",
        )
        committed = await first.commit_proof_reset(
            resource_id,
            100,
            reset_id="aba-reset",
            expected_reset_revision=prepared.ledger_revision,
            expected_reset_incarnation=prepared.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(),
            ready=True,
            evidence_deadline_ms=_future_reconcile_deadline_ms(),
            proof_fingerprint=hashlib.sha256(b"aba-proof").hexdigest(),
        )
        assert committed.ledger_revision == old.ledger_revision == 2
        assert committed.ledger_incarnation != old.ledger_incarnation

        stale = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=old.ledger_revision,
            expected_ledger_incarnation=old.ledger_incarnation,
            backend_memberships=memberships,
            allocations=(),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="old-context-after-reset",
        )
        assert (stale.status, stale.reason) == (
            "stale_revision",
            "ledger_incarnation_changed",
        )
    finally:
        await raw.aclose()


@pytest.mark.parametrize(
    "corruption",
    (
        "allocations",
        "leases",
        "backend_queue",
        "transition",
        "deadline",
        "revision",
        "revision_zero",
    ),
)
@pytest.mark.parametrize("entry", ("admit", "enqueue", "transition_owner"))
@pytest.mark.asyncio
async def test_ready_card_integrity_loss_blocks_every_new_work_entry(
    redis_stores,
    corruption: str,
    entry: str,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-{corruption}-{entry}/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    keys = first.keys(resource_id)

    if corruption == "backend_queue":
        assert (
            await first.enqueue_backend(
                resource_id,
                backend_id="backend-b",
                membership_epoch=1,
                ticket_id="existing-ticket",
                owner_id="existing-owner",
                ttl_ms=30_000,
            )
        ).status == "queued"
    elif corruption == "transition":
        assert (
            await first.acquire_transition_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="existing-transition-owner",
                generation="2",
                operation="evict",
                ttl_ms=30_000,
            )
        ).status == "acquired"

    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        if corruption == "allocations":
            await raw.delete(keys.allocations)
        elif corruption == "leases":
            await raw.delete(keys.leases("backend-a"))
        elif corruption == "backend_queue":
            await raw.delete(keys.backend_queue("backend-b"))
        elif corruption == "transition":
            await raw.delete(keys.transition)
        elif corruption == "deadline":
            await raw.hset(
                keys.card,
                "reconcile_deadline_ms",
                str(9_007_199_254_740_992),
            )
        elif corruption == "revision":
            await raw.hdel(keys.card, "ledger_revision")
        else:
            await raw.hset(keys.card, "ledger_revision", "0")

        children_before = (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.queue, 0, -1),
            await raw.lrange(keys.backend_queue("backend-b"), 0, -1),
            await raw.get(keys.transition),
        )
        if entry == "admit":
            result = await first.admit(
                resource_id,
                **_admission_kwargs(
                    "backend-b",
                    "new-lease",
                    budget_mb=40,
                    owner_id="new-owner",
                ),
            )
        elif entry == "enqueue":
            result = await first.enqueue_card(
                resource_id,
                backend_id="backend-b",
                membership_epoch=1,
                ticket_id="new-ticket",
                owner_id="new-owner",
                ttl_ms=30_000,
            )
        else:
            result = await first.acquire_transition_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="new-transition-owner",
                generation="2",
                operation="evict",
                ttl_ms=30_000,
            )

        assert result.status == "ledger_corrupt"
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
        assert (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.queue, 0, -1),
            await raw.lrange(keys.backend_queue("backend-b"), 0, -1),
            await raw.get(keys.transition),
        ) == children_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_missing_revision_blocks_cleanup_and_state_mutations(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-missing-revision-cleanup/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-b",
            membership_epoch=1,
            ticket_id="existing-ticket",
            owner_id="existing-owner",
            ttl_ms=30_000,
        )
    ).status == "queued"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="existing-transition-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "acquired"

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hdel(keys.card, "ledger_revision")
        children_before = (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-b"), 0, -1),
            await raw.get(keys.transition),
        )

        assert (
            await first.heartbeat_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
                heartbeat_ttl_ms=5_000,
            )
        ).status == "not_ready"
        assert await first.sweep_expired_leases(
            resource_id, backend_id="backend-a"
        ) == (0, 0)
        assert (
            await first.queue_position(
                resource_id,
                backend_id="backend-b",
                ticket_id="existing-ticket",
                card_queue=False,
            )
        ).status == "ledger_corrupt"
        assert (
            await first.heartbeat_transition_owner(
                resource_id,
                backend_id="backend-a",
                owner_id="existing-transition-owner",
                generation="2",
                operation="evict",
                ttl_ms=5_000,
            )
        ).status == "ledger_corrupt"
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-a",
                expected_generation="1",
                target_state=GPUAllocationState.DRAINING,
                next_generation="2",
                transition_owner_id="existing-transition-owner",
                transition_operation="evict",
            )
        ).status == "ledger_corrupt"
        assert (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
            await raw.lrange(keys.backend_queue("backend-b"), 0, -1),
            await raw.get(keys.transition),
        ) == children_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_expired_idempotent_repair_cannot_reopen_readiness(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-expired-repair/index:0"
    kwargs = {
        "expected_ledger_revision": None,
        "expected_ledger_incarnation": None,
        "backend_memberships": _TEST_BACKEND_MEMBERSHIPS,
        "allocations": (),
        "lease_cleanup": None,
        "ready": True,
        "reconcile_deadline_ms": _future_reconcile_deadline_ms(100),
        "repair_id": "expiring-repair",
    }
    created = await first.reconcile_card(resource_id, 100, **kwargs)
    assert created.status == "reconciled"
    snapshot = await first.snapshot(resource_id)

    await asyncio.sleep(0.12)
    retried = await first.reconcile_card(resource_id, 100, **kwargs)

    assert retried.status == "not_ready"
    assert retried.ready is False
    assert retried.idempotent is True
    assert retried.ledger_revision == created.ledger_revision
    expired = await first.snapshot(resource_id)
    assert expired.ready is False
    assert expired.reconcile_deadline_ms == snapshot.reconcile_deadline_ms


@pytest.mark.asyncio
async def test_ready_renewal_cas_preserves_live_ledger_and_fences_races(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-renewal/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first, resource_id, wait_for_cooldown=False, resident_cooldown_ms=30_000
    )
    before_admission = await first.snapshot(resource_id)
    assert (
        await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "live-lease"),
        )
    ).admitted
    proposed = int(time.time() * 1000) + 240_000
    assert not await first.renew_ready(before_admission, evidence_deadline_ms=proposed)
    stale = await first.snapshot(resource_id)
    assert (
        await second.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="waiting",
            owner_id="waiter",
            ttl_ms=30_000,
        )
    ).status == "queued"
    assert not await first.renew_ready(stale, evidence_deadline_ms=proposed)
    current = await first.snapshot(resource_id)
    assert current.allocations[0].evictable
    assert current.leases and current.backend_queue_count == 1
    assert await first.renew_ready(current, evidence_deadline_ms=proposed)
    renewed = await first.snapshot(resource_id)
    assert renewed.reconcile_deadline_ms == proposed
    assert renewed.ledger_revision == current.ledger_revision + 1
    assert renewed.ledger_incarnation == current.ledger_incarnation
    assert renewed.allocations == current.allocations
    assert renewed.leases == current.leases
    assert renewed.backend_queue_count == current.backend_queue_count
    assert renewed.card_queue_count == current.card_queue_count
    assert renewed.transition_present == current.transition_present
    assert not await second.renew_ready(current, evidence_deadline_ms=proposed + 1)
    assert not await first.renew_ready(
        replace(renewed, ledger_incarnation="different"),
        evidence_deadline_ms=proposed + 1,
    )
    assert not await first.renew_ready(
        replace(renewed, reconcile_deadline_ms=renewed.reconcile_deadline_ms - 1),
        evidence_deadline_ms=proposed + 1,
    )
    assert not await first.renew_ready(
        renewed,
        evidence_deadline_ms=int(time.time() * 1000) + 301_000,
    )

    card = first.keys(resource_id).card
    domain = await first._redis.hget(card, "membership_domain")
    await first._redis.hset(card, "membership_domain", "[]")
    assert not await first.renew_ready(renewed, evidence_deadline_ms=proposed + 1)
    await first._redis.hset(card, "membership_domain", domain)
    await first._redis.hset(card, "reconcile_deadline_ms", int(time.time() * 1000) - 1)
    assert not await first.renew_ready(renewed, evidence_deadline_ms=proposed + 1)
    expired = await first.snapshot(resource_id)
    assert not expired.ready
    assert not await first.renew_ready(expired, evidence_deadline_ms=proposed + 1)


@pytest.mark.asyncio
async def test_expired_reconcile_deadline_stops_new_work_and_queueing(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    result = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(10),
        repair_id="short-readiness",
    )
    assert result.ready is True
    await asyncio.sleep(0.02)

    assert (await first.snapshot(resource_id)).ready is False
    admission = await first.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-after-expiry"),
    )
    assert admission.status == "not_ready"
    assert admission.reason == "reconcile_expired"
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="ticket-after-expiry",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "not_ready"


@pytest.mark.asyncio
async def test_admission_extends_reconcile_deadline_past_token_horizon(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-admission-renewal/index:0"
    await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(1_000),
        repair_id="admission-renewal",
    )

    admission = await first.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-renews-card"),
        reconcile_deadline_grace_ms=60_000,
    )

    assert admission.admitted is True
    assert admission.hard_deadline_ms is not None
    snapshot = await first.snapshot(resource_id)
    assert snapshot.ready is True
    assert snapshot.reconcile_deadline_ms == admission.hard_deadline_ms + 60_000


@pytest.mark.asyncio
async def test_admission_rejects_excessive_reconcile_deadline_grace(
    redis_stores,
) -> None:
    first, _ = redis_stores

    with pytest.raises(ValueError, match="must be at most 300000"):
        await first.admit(
            "node-admission-renewal/index:0",
            **_admission_kwargs("backend-a", "lease-invalid-grace"),
            reconcile_deadline_grace_ms=300_001,
        )


@pytest.mark.parametrize("card_queue", (False, True))
@pytest.mark.asyncio
async def test_expired_queue_ticket_does_not_block_reconcile(
    redis_stores,
    card_queue: bool,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-expired-{'card' if card_queue else 'backend'}-queue/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    enqueue = first.enqueue_card if card_queue else first.enqueue_backend
    assert (
        await enqueue(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="expired-ticket",
            owner_id="owner-a",
            ttl_ms=10,
        )
    ).status == "queued"
    await asyncio.sleep(0.03)
    before = await first.snapshot(resource_id)

    repaired = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=before.allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id=f"clear-expired-{'card' if card_queue else 'backend'}-queue",
    )
    assert repaired.status == "reconciled"
    assert repaired.ready is True
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="expired-ticket",
            card_queue=card_queue,
        )
    ).status == "missing"


@pytest.mark.asyncio
async def test_busy_reconcile_reports_expired_card_as_not_ready(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-expired-busy/index:0"
    bootstrap = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(10),
        repair_id="short-busy-readiness",
    )
    assert bootstrap.ready is True
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="live-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "queued"
    await asyncio.sleep(0.02)
    before = await first.snapshot(resource_id)

    busy = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=before.allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="busy-after-expiry",
    )
    assert busy.status == "busy"
    assert busy.ready is False


@pytest.mark.asyncio
async def test_stale_incarnation_new_work_context_does_not_latch_replacement(
    redis_stores,
    monkeypatch,
) -> None:
    first, _ = redis_stores
    resource_id = "node-stale-context/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    old_context = await first._ledger_domain(first.keys(resource_id))
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.delete(
            keys.card,
            keys.allocations,
            keys.queue,
            keys.transition,
            *(
                key
                for backend_id in _TEST_BACKEND_DOMAIN
                for key in (
                    keys.leases(backend_id),
                    keys.backend_queue(backend_id),
                )
            ),
        )
        replacement = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=None,
            expected_ledger_incarnation=None,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=(),
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="replacement-bootstrap",
        )

        async def stale_context(_keys):
            return old_context

        monkeypatch.setattr(first, "_ledger_domain", stale_context)
        rejected = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "stale-context-lease"),
        )
        assert rejected.status == "not_ready"
        assert await raw.hget(keys.card, "bootstrap_state") == "ready"
        assert await raw.hget(keys.card, "ledger_incarnation") == (
            replacement.ledger_incarnation
        )
        assert await raw.hlen(keys.allocations) == 0
        assert await raw.hlen(keys.leases("backend-a")) == 0
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_stale_membership_context_does_not_latch_reconciled_card(
    redis_stores,
    monkeypatch,
) -> None:
    first, _ = redis_stores
    resource_id = "node-stale-membership-context/index:0"
    initial_memberships = _memberships("backend-a", "backend-b")
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=initial_memberships,
    )
    keys = first.keys(resource_id)
    old_context = await first._ledger_domain(keys)
    await first.mark_card_not_ready(resource_id, 100, reason="membership_change")
    before_evolution = await first.snapshot(resource_id)
    target_memberships = (
        GPUBackendDomainMember("backend-a", 2, "retiring"),
        GPUBackendDomainMember("backend-b", 1, "active"),
    )
    evolved = await first.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=before_evolution.ledger_revision,
        expected_ledger_incarnation=before_evolution.ledger_incarnation,
        backend_memberships=target_memberships,
        evolution_id="stale-context-evolution",
    )
    assert evolved.status == "evolved"
    reconciled = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=evolved.ledger_revision,
        expected_ledger_incarnation=evolved.ledger_incarnation,
        backend_memberships=target_memberships,
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="stale-context-reconcile",
    )
    assert reconciled.status == "reconciled"

    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        card_before = await raw.hgetall(keys.card)

        async def stale_context(_keys):
            return old_context

        monkeypatch.setattr(first, "_ledger_domain", stale_context)
        rejected = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "stale-membership-lease"),
        )
        assert (rejected.status, rejected.reason) == (
            "not_ready",
            "membership_domain_changed",
        )
        assert await raw.hgetall(keys.card) == card_before
        assert card_before["bootstrap_state"] == "ready"
        assert await raw.hlen(keys.allocations) == 0
        assert await raw.hlen(keys.leases("backend-a")) == 0
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_reconcile_revision_cas_cannot_overwrite_a_new_admission(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    stale_snapshot = await first.snapshot(resource_id)
    assert (
        await second.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-a"),
        )
    ).admitted

    stale_repair = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=stale_snapshot.ledger_revision,
        expected_ledger_incarnation=stale_snapshot.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=stale_snapshot.allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="stale-repair",
    )
    assert stale_repair.status == "stale_revision"
    current = await second.snapshot(resource_id)
    assert len(current.allocations) == 1
    assert current.allocations[0].state is GPUAllocationState.RESERVING
    assert len(current.leases) == 1


@pytest.mark.asyncio
async def test_reconcile_only_purges_stale_lease_after_hard_deadline(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        lease_raw = await raw.hget(keys.leases("backend-a"), "lease-a")
        lease = json.loads(lease_raw)
        lease["state"] = "stale"
        await raw.hset(
            keys.leases("backend-a"),
            "lease-a",
            json.dumps(lease),
        )
        before = await first.snapshot(resource_id)
        target = replace(
            before.allocations[0],
            state=GPUAllocationState.UNLOADED,
            reservation_lease_id=None,
            reservation_owner_id=None,
        )
        redis_time = await raw.time()
        now_ms = (int(redis_time[0]) * 1000) + (int(redis_time[1]) // 1000)
        cleanup = {
            "backend-a": GPUReconcileLeaseCleanup(
                observed_idle_at_ms=now_ms,
                lease_ids=("lease-a",),
            )
        }

        rejected = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=(target,),
            lease_cleanup=cleanup,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="lease-too-young",
        )
        assert rejected.status == "active_leases"
        assert len((await first.snapshot(resource_id)).leases) == 1

        lease["heartbeat_deadline_ms"] = lease["created_at_ms"]
        lease["hard_deadline_ms"] = lease["created_at_ms"]
        await raw.hset(
            keys.leases("backend-a"),
            "lease-a",
            json.dumps(lease),
        )
        redis_time = await raw.time()
        observed_idle_at_ms = (int(redis_time[0]) * 1000) + (int(redis_time[1]) // 1000)
        repaired = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=(target,),
            lease_cleanup={
                "backend-a": GPUReconcileLeaseCleanup(
                    observed_idle_at_ms=observed_idle_at_ms,
                    lease_ids=("lease-a",),
                )
            },
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="lease-expired",
        )
        assert repaired.status == "reconciled"
        assert repaired.purged_leases == 1
        assert repaired.committed_mb == 0
        snapshot = await first.snapshot(resource_id)
        assert snapshot.allocations[0].state is GPUAllocationState.UNLOADED
        assert snapshot.leases == ()
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_missing_or_not_ready_card_fails_closed(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"

    missing = await first.admit(
        resource_id, **_admission_kwargs("backend-a", "lease-missing")
    )
    assert missing.status == "not_ready"

    with pytest.raises(ValueError, match="reconcile_card"):
        await first.configure_card(resource_id, 100, ready=True)

    await first.configure_card(resource_id, 100, ready=False)
    rebuilding = await first.admit(
        resource_id, **_admission_kwargs("backend-a", "lease-rebuilding")
    )
    assert rebuilding.status == "not_ready"


@pytest.mark.parametrize(
    ("state", "expected_reason"),
    [
        (None, "resident_allocation_required"),
        (GPUAllocationState.UNKNOWN, "allocation_unknown"),
        (GPUAllocationState.UNLOADED, "resident_allocation_required"),
        (GPUAllocationState.CPU_FALLBACK, "resident_allocation_required"),
    ],
)
@pytest.mark.asyncio
async def test_require_resident_rejects_nonresident_without_side_effects(
    redis_stores,
    state: GPUAllocationState | None,
    expected_reason: str,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-resident-only/{state.value if state else 'missing'}"
    allocations = (
        () if state is None else (_reconcile_allocation(state=state, generation="1"),)
    )
    reconciled = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id=f"resident-only-{state.value if state else 'missing'}",
    )
    assert reconciled.status == "reconciled"
    before = await first.snapshot(resource_id)
    kwargs = _admission_kwargs(
        "backend-a",
        "resident-only-lease",
        generation=(
            "2"
            if state in {GPUAllocationState.UNLOADED, GPUAllocationState.CPU_FALLBACK}
            else "1"
        ),
    )
    kwargs["evictable"] = False

    rejected = await first.admit(
        resource_id,
        require_resident=True,
        **kwargs,
    )

    assert (rejected.status, rejected.reason) == (
        "not_ready",
        expected_reason,
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.committed_mb == before.committed_mb
    assert after.allocations == before.allocations
    assert after.leases == ()


@pytest.mark.parametrize(
    "state",
    [GPUAllocationState.RESERVING, GPUAllocationState.LOADING],
)
@pytest.mark.asyncio
async def test_require_resident_rejects_cold_allocation_and_idempotent_lease(
    redis_stores,
    state: GPUAllocationState,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-resident-only/{state.value}"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    cold_kwargs = _admission_kwargs("backend-a", "cold-lease")
    assert (await first.admit(resource_id, **cold_kwargs)).admitted
    if state is GPUAllocationState.LOADING:
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-a",
                expected_generation="1",
                target_state=GPUAllocationState.LOADING,
                request_lease_id="cold-lease",
                request_owner_id="owner-a",
            )
        ).status == "transitioned"
    before = await first.snapshot(resource_id)

    new_lease = await first.admit(
        resource_id,
        require_resident=True,
        **_admission_kwargs("backend-a", "resident-only-lease"),
    )
    idempotent_lease = await first.admit(
        resource_id,
        require_resident=True,
        **cold_kwargs,
    )

    assert (new_lease.status, new_lease.reason) == (
        "not_ready",
        "resident_allocation_required",
    )
    assert (idempotent_lease.status, idempotent_lease.reason) == (
        "not_ready",
        "resident_allocation_required",
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.allocations == before.allocations
    assert after.leases == before.leases


@pytest.mark.parametrize(
    "state",
    [GPUAllocationState.RESERVING, GPUAllocationState.LOADING],
)
@pytest.mark.asyncio
async def test_cold_allocation_only_admits_its_reservation_owner(
    redis_stores,
    state: GPUAllocationState,
) -> None:
    first, second = redis_stores
    resource_id = f"node-cold-owner/{state.value}"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    reservation = _admission_kwargs("backend-a", "cold-lease")
    admitted = await first.admit(resource_id, **reservation)
    assert admitted.admitted
    if state is GPUAllocationState.LOADING:
        transitioned = await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="cold-lease",
            request_owner_id="owner-a",
        )
        assert transitioned.status == "transitioned"
    before = await first.snapshot(resource_id)

    retry = await first.admit(resource_id, **reservation)
    competitor = await second.admit(
        resource_id,
        **_admission_kwargs(
            "backend-a",
            "competitor-lease",
            owner_id="owner-b",
        ),
    )

    assert retry.admitted is True
    assert retry.idempotent is True
    assert (competitor.status, competitor.reason) == (
        "not_ready",
        "cold_allocation_in_progress",
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.allocations == before.allocations
    assert after.leases == before.leases


@pytest.mark.asyncio
async def test_cold_admission_owner_serializes_generation_and_reservation(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-cold-owner/prepare"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a", "backend-b"),
    )

    acquired = await first.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="cold-owner-a",
        generation="1",
        ttl_ms=30_000,
    )
    blocked = await second.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-b",
        membership_epoch=1,
        owner_id="cold-owner-b",
        generation="1",
        ttl_ms=30_000,
    )
    assert acquired.status == "acquired"
    assert blocked.status == "busy"
    renewed = await first.revalidate_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="cold-owner-a",
        generation="1",
        ttl_ms=30_000,
    )
    assert renewed.status == "renewed"

    unowned = await second.admit(
        resource_id,
        **_admission_kwargs("backend-a", "unowned-lease", owner_id="other-owner"),
    )
    unowned_other_backend = await second.admit(
        resource_id,
        **_admission_kwargs(
            "backend-b",
            "unowned-other-lease",
            owner_id="other-owner",
        ),
    )
    wrong_owner = await second.admit(
        resource_id,
        require_cold_owner=True,
        **_admission_kwargs("backend-a", "wrong-lease", owner_id="other-owner"),
    )
    assert (unowned.status, unowned.reason) == (
        "transition_in_progress",
        "transition_owner_active",
    )
    assert (wrong_owner.status, wrong_owner.reason) == (
        "transition_in_progress",
        "transition_owner_active",
    )
    assert (unowned_other_backend.status, unowned_other_backend.reason) == (
        "transition_in_progress",
        "cold_admission_owner_active",
    )

    reservation = _admission_kwargs(
        "backend-a",
        "cold-lease",
        owner_id="cold-owner-a",
    )
    admitted = await first.admit(
        resource_id,
        require_cold_owner=True,
        **reservation,
    )
    retry = await second.admit(
        resource_id,
        require_cold_owner=True,
        **reservation,
    )
    assert admitted.admitted is True
    assert admitted.allocation_state is GPUAllocationState.RESERVING
    assert retry.admitted is True
    assert retry.idempotent is True

    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="cold-lease",
            request_owner_id="cold-owner-a",
        )
    ).status == "transitioned"
    assert (
        await first.finalize_cold_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            request_lease_id="cold-lease",
            request_owner_id="cold-owner-a",
            target_state=GPUAllocationState.RESIDENT,
            target_evictable=True,
            resident_cooldown_ms=1,
        )
    ).status == "transitioned"
    other_intent = await second.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-b",
        membership_epoch=1,
        owner_id="cold-owner-b",
        generation="1",
        ttl_ms=30_000,
    )
    assert other_intent.status == "acquired"
    response_loss_retry = await first.admit(
        resource_id,
        require_cold_owner=True,
        **reservation,
    )
    assert response_loss_retry.admitted is True
    assert response_loss_retry.idempotent is True
    assert (
        await second.release_cold_admission_owner(
            resource_id,
            backend_id="backend-b",
            owner_id="cold-owner-b",
            generation="1",
        )
    ).status == "released"

    released = await first.release_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        owner_id="cold-owner-a",
        generation="1",
    )
    assert released.status == "missing"
    stale_target = await second.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="cold-owner-next",
        generation="2",
        ttl_ms=30_000,
    )
    assert stale_target.status == "invalid_transition"
    snapshot = await first.snapshot(resource_id)
    assert snapshot.transition_present is False
    assert snapshot.allocations[0].state is GPUAllocationState.RESIDENT
    assert snapshot.allocations[0].reservation_owner_id is None
    assert tuple(item.lease_id for item in snapshot.leases) == ("cold-lease",)


@pytest.mark.asyncio
async def test_cold_admission_owner_requires_ready_fresh_active_card(
    redis_stores,
) -> None:
    first, _ = redis_stores
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        for suffix, fields in (
            ("not-ready", {"bootstrap_state": "not_ready"}),
            ("expired", {"reconcile_deadline_ms": "1"}),
        ):
            resource_id = f"node-cold-owner/{suffix}"
            await _bootstrap_empty_card(
                first,
                resource_id,
                100,
                memberships=_memberships("backend-a"),
            )
            await raw.hset(first.keys(resource_id).card, mapping=fields)
            result = await first.acquire_cold_admission_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="cold-owner",
                generation="1",
                ttl_ms=30_000,
            )
            assert result.status == "not_ready"
            assert (await first.snapshot(resource_id)).transition_present is False

        for membership_state in ("pending", "retiring"):
            resource_id = f"node-cold-owner/{membership_state}"
            await _bootstrap_empty_card(
                first,
                resource_id,
                100,
                memberships=(
                    *_memberships("backend-a", state=membership_state),
                    *_memberships("backend-b"),
                ),
            )
            result = await first.acquire_cold_admission_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="cold-owner",
                generation="1",
                ttl_ms=30_000,
            )
            assert result.status == "config_mismatch"
            assert (await first.snapshot(resource_id)).transition_present is False

        resource_id = "node-cold-owner/revalidate-not-ready"
        await _bootstrap_empty_card(
            first,
            resource_id,
            100,
            memberships=_memberships("backend-a"),
        )
        assert (
            await first.acquire_cold_admission_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="cold-owner",
                generation="1",
                ttl_ms=30_000,
            )
        ).status == "acquired"
        keys = first.keys(resource_id)
        transition_before = await raw.get(keys.transition)
        await raw.hset(keys.card, "bootstrap_state", "not_ready")
        revision_before = await raw.hget(keys.card, "ledger_revision")
        not_ready = await first.revalidate_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner",
            generation="1",
            ttl_ms=30_000,
        )
        assert not_ready.status == "not_ready"
        assert await raw.get(keys.transition) == transition_before
        assert await raw.hget(keys.card, "ledger_revision") == revision_before

        resource_id = "node-cold-owner/revalidate-epoch"
        await _bootstrap_empty_card(
            first,
            resource_id,
            100,
            memberships=_memberships("backend-a"),
        )
        assert (
            await first.acquire_cold_admission_owner(
                resource_id,
                backend_id="backend-a",
                membership_epoch=1,
                owner_id="cold-owner",
                generation="1",
                ttl_ms=30_000,
            )
        ).status == "acquired"
        keys = first.keys(resource_id)
        transition_before = await raw.get(keys.transition)
        revision_before = await raw.hget(keys.card, "ledger_revision")
        changed = await first.revalidate_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=2,
            owner_id="cold-owner",
            generation="1",
            ttl_ms=30_000,
        )
        assert changed.status == "config_mismatch"
        assert await raw.get(keys.transition) == transition_before
        assert await raw.hget(keys.card, "ledger_revision") == revision_before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_cold_admission_owner_does_not_block_other_resident_backend(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-cold-owner/resident-fast-path"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a", "backend-b"),
    )
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="resident-lease-a",
        owner_id="resident-owner-a",
        budget_mb=40,
    )
    assert (
        await first.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner",
            generation="1",
            ttl_ms=30_000,
        )
    ).status == "acquired"

    resident = await second.admit(
        resource_id,
        require_resident=True,
        **_admission_kwargs(
            "backend-b",
            "resident-lease-b",
            budget_mb=40,
            owner_id="resident-owner-b",
        ),
    )
    assert resident.admitted is True
    assert resident.allocation_state is GPUAllocationState.RESIDENT


@pytest.mark.asyncio
async def test_failed_cold_admission_keeps_owner_until_exact_release(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-cold-owner/capacity-failure"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a", "backend-b"),
    )
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="resident-lease",
        owner_id="resident-owner",
        budget_mb=80,
    )
    assert (
        await first.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner-a",
            generation="1",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    failed = await first.admit(
        resource_id,
        require_cold_owner=True,
        **_admission_kwargs(
            "backend-a",
            "cold-lease",
            budget_mb=30,
            owner_id="cold-owner-a",
        ),
    )
    assert failed.status == "capacity_unavailable"
    assert (
        await second.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner-b",
            generation="1",
            ttl_ms=30_000,
        )
    ).status == "busy"
    assert (
        await first.release_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="cold-owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await second.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner-b",
            generation="2",
            ttl_ms=30_000,
        )
    ).status == "acquired"


@pytest.mark.asyncio
async def test_cold_admission_owners_are_isolated_by_full_resource_id(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_a = "node-cold-multi/index:0"
    resource_b = "node-cold-multi/index:1"
    for store, resource_id in ((first, resource_a), (second, resource_b)):
        await _bootstrap_empty_card(
            store,
            resource_id,
            100,
            memberships=_memberships("backend-a"),
        )

    owner_a, owner_b = await asyncio.gather(
        first.acquire_cold_admission_owner(
            resource_a,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="same-owner",
            generation="1",
            ttl_ms=30_000,
        ),
        second.acquire_cold_admission_owner(
            resource_b,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="same-owner",
            generation="1",
            ttl_ms=30_000,
        ),
    )

    assert owner_a.status == owner_b.status == "acquired"
    assert (await first.snapshot(resource_a)).transition_present is True
    assert (await second.snapshot(resource_b)).transition_present is True


@pytest.mark.asyncio
async def test_expired_cold_admission_owner_cannot_reserve_after_takeover(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-cold-owner/takeover"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    assert (
        await first.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="expired-owner",
            generation="1",
            ttl_ms=30,
        )
    ).status == "acquired"

    deadline = asyncio.get_running_loop().time() + 1
    while True:
        takeover = await second.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="current-owner",
            generation="2",
            ttl_ms=30_000,
        )
        if takeover.status == "acquired":
            break
        assert takeover.status == "busy"
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("cold admission owner did not expire")
        await asyncio.sleep(0.005)

    stale = await first.admit(
        resource_id,
        require_cold_owner=True,
        **_admission_kwargs(
            "backend-a",
            "stale-lease",
            owner_id="expired-owner",
        ),
    )
    stale_release = await first.release_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        owner_id="expired-owner",
        generation="1",
    )
    current = await second.admit(
        resource_id,
        require_cold_owner=True,
        **_admission_kwargs(
            "backend-a",
            "current-lease",
            owner_id="current-owner",
            generation="2",
        ),
    )
    assert (stale.status, stale.reason) == (
        "transition_in_progress",
        "transition_owner_active",
    )
    assert stale_release.status == "owner_mismatch"
    assert current.admitted is True
    snapshot = await first.snapshot(resource_id)
    assert tuple(item.lease_id for item in snapshot.leases) == ("current-lease",)


@pytest.mark.asyncio
async def test_expired_cold_admission_owner_cannot_be_revalidated_or_recreated(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-cold-owner/revalidate-expired"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    assert (
        await first.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="expired-owner",
            generation="1",
            ttl_ms=30,
        )
    ).status == "acquired"

    await asyncio.sleep(0.05)
    for _ in range(2):
        missing = await first.revalidate_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="expired-owner",
            generation="1",
            ttl_ms=30_000,
        )
        assert missing.status == "missing"

    assert (
        await second.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="takeover-owner",
            generation="2",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    assert (
        await second.release_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="takeover-owner",
            generation="2",
        )
    ).status == "released"
    stale = await first.revalidate_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="expired-owner",
        generation="1",
        ttl_ms=30_000,
    )
    assert stale.status == "missing"
    assert (await first.snapshot(resource_id)).transition_present is False


@pytest.mark.parametrize(
    ("target_state", "target_evictable", "committed_mb"),
    (
        (GPUAllocationState.RESIDENT, True, 60),
        (GPUAllocationState.UNKNOWN, False, 60),
        (GPUAllocationState.UNLOADED, False, 0),
        (GPUAllocationState.CPU_FALLBACK, False, 0),
    ),
)
@pytest.mark.asyncio
async def test_finalize_cold_allocation_is_owned_and_idempotent(
    redis_stores,
    target_state: GPUAllocationState,
    target_evictable: bool,
    committed_mb: int,
) -> None:
    first, second = redis_stores
    resource_id = f"node-cold-finalize/{target_state.value}"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    assert (
        await first.acquire_cold_admission_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="cold-owner",
            generation="1",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    kwargs = _admission_kwargs(
        "backend-a",
        "cold-lease",
        owner_id="cold-owner",
    )
    kwargs["evictable"] = False
    assert (
        await first.admit(
            resource_id,
            require_cold_owner=True,
            **kwargs,
        )
    ).admitted
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="cold-lease",
            request_owner_id="cold-owner",
        )
    ).status == "transitioned"

    wrong_owner = await second.finalize_cold_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="1",
        request_lease_id="cold-lease",
        request_owner_id="other-owner",
        target_state=target_state,
        target_evictable=target_evictable,
        resident_cooldown_ms=(
            30_000 if target_state is GPUAllocationState.RESIDENT else 0
        ),
    )
    assert wrong_owner.status == "owner_mismatch"
    before_finalize = await first.snapshot(resource_id)
    finalized = await first.finalize_cold_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="1",
        request_lease_id="cold-lease",
        request_owner_id="cold-owner",
        target_state=target_state,
        target_evictable=target_evictable,
        resident_cooldown_ms=(
            30_000 if target_state is GPUAllocationState.RESIDENT else 0
        ),
    )
    assert finalized.status == "transitioned"
    assert finalized.idempotent is False
    assert finalized.committed_mb == committed_mb
    terminal_snapshot = await first.snapshot(resource_id)
    terminal_cooldown = terminal_snapshot.allocations[0].not_evict_before_ms
    if target_state is GPUAllocationState.RESIDENT:
        assert (
            before_finalize.observed_at_ms + 30_000
            <= terminal_cooldown
            <= terminal_snapshot.observed_at_ms + 30_000
        )
        assert (
            await first.admit(
                resource_id,
                require_resident=True,
                **_admission_kwargs(
                    "backend-a",
                    "resident-lease",
                    owner_id="resident-owner",
                ),
            )
        ).admitted
        assert (await first.snapshot(resource_id)).allocations[
            0
        ].not_evict_before_ms == terminal_cooldown
    else:
        assert terminal_cooldown == 0
    revision = (await first.snapshot(resource_id)).ledger_revision

    retried = await second.finalize_cold_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="1",
        request_lease_id="cold-lease",
        request_owner_id="cold-owner",
        target_state=target_state,
        target_evictable=target_evictable,
        resident_cooldown_ms=(
            30_000 if target_state is GPUAllocationState.RESIDENT else 0
        ),
    )
    assert retried.status == "transitioned"
    assert retried.idempotent is True
    assert retried.committed_mb == committed_mb
    snapshot = await first.snapshot(resource_id)
    assert snapshot.ledger_revision == revision
    assert snapshot.allocations[0].state is target_state
    assert snapshot.allocations[0].evictable is target_evictable
    assert snapshot.allocations[0].not_evict_before_ms == terminal_cooldown
    expected_leases = (
        ("cold-lease", "resident-lease")
        if target_state is GPUAllocationState.RESIDENT
        else ("cold-lease",)
    )
    assert tuple(item.lease_id for item in snapshot.leases) == expected_leases


@pytest.mark.parametrize(
    "state",
    (GPUAllocationState.UNLOADED, GPUAllocationState.CPU_FALLBACK),
)
@pytest.mark.asyncio
async def test_cold_admission_owner_requires_new_generation_for_empty_tombstone(
    redis_stores,
    state: GPUAllocationState,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-cold-owner/{state.value}"
    reconciled = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(_reconcile_allocation(state=state, generation="1"),),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id=f"cold-owner-{state.value}",
    )
    assert reconciled.status == "reconciled"

    stale = await first.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="cold-owner",
        generation="1",
        ttl_ms=30_000,
    )
    current = await first.acquire_cold_admission_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="cold-owner",
        generation="2",
        ttl_ms=30_000,
    )
    assert stale.status == "stale_generation"
    assert current.status == "acquired"

    kwargs = _admission_kwargs(
        "backend-a",
        "cold-lease",
        owner_id="cold-owner",
        generation="2",
    )
    kwargs["evictable"] = False
    admitted = await first.admit(
        resource_id,
        require_cold_owner=True,
        **kwargs,
    )
    assert admitted.admitted is True
    assert admitted.allocation_state is GPUAllocationState.RESERVING


@pytest.mark.asyncio
async def test_require_resident_admits_only_resident_and_enforces_concurrency(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-resident-only/resident"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await _admit_resident(
        first,
        resource_id,
        lease_id="initial-lease",
        max_concurrency=1,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="initial-lease",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    kwargs = _admission_kwargs(
        "backend-a",
        "resident-lease",
        max_concurrency=1,
    )

    saturated_kwargs = _admission_kwargs(
        "backend-a",
        "saturated-lease",
        max_concurrency=1,
        owner_id="owner-b",
    )
    results = await asyncio.gather(
        first.admit(
            resource_id,
            require_resident=True,
            **kwargs,
        ),
        second.admit(
            resource_id,
            require_resident=True,
            **saturated_kwargs,
        ),
    )

    assert sorted(result.status for result in results) == [
        "admitted",
        "concurrency_saturated",
    ]
    admitted = next(result for result in results if result.admitted)
    saturated = next(
        result for result in results if result.status == "concurrency_saturated"
    )
    assert admitted.admitted is True
    assert admitted.allocation_state is GPUAllocationState.RESIDENT
    assert (saturated.status, saturated.reason) == (
        "concurrency_saturated",
        "max_concurrency_reached",
    )
    snapshot = await first.snapshot(resource_id)
    assert len(snapshot.allocations) == 1
    assert snapshot.allocations[0].state is GPUAllocationState.RESIDENT
    assert snapshot.allocations[0].generation == "1"
    assert len(snapshot.leases) == 1
    assert snapshot.committed_mb == 60


@pytest.mark.parametrize(
    "state",
    [GPUAllocationState.DRAINING, GPUAllocationState.UNLOADING],
)
@pytest.mark.asyncio
async def test_require_resident_rejects_transition_race(
    redis_stores,
    state: GPUAllocationState,
) -> None:
    first, _ = redis_stores
    resource_id = f"node-resident-only/{state.value}"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await _admit_resident(first, resource_id, lease_id="initial-lease")
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="initial-lease",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    owner = await first.acquire_transition_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="drain-owner",
        generation="2",
        operation="drain",
        ttl_ms=30_000,
        require_idle=True,
    )
    assert owner.status == "acquired"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.DRAINING,
            next_generation="2",
            transition_owner_id="drain-owner",
            transition_operation="drain",
        )
    ).status == "transitioned"
    if state is GPUAllocationState.UNLOADING:
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-a",
                expected_generation="2",
                target_state=GPUAllocationState.UNLOADING,
                transition_owner_id="drain-owner",
                transition_operation="drain",
            )
        ).status == "transitioned"
    before = await first.snapshot(resource_id)

    rejected = await first.admit(
        resource_id,
        require_resident=True,
        **_admission_kwargs(
            "backend-a",
            "resident-only-lease",
            generation="2",
        ),
    )

    assert (rejected.status, rejected.reason) == (
        "transition_in_progress",
        "transition_owner_active",
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.allocations == before.allocations
    assert after.leases == ()


@pytest.mark.asyncio
async def test_require_resident_and_idle_transition_owner_are_atomic(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-resident-only/atomic-transition"
    await _bootstrap_empty_card(
        first,
        resource_id,
        100,
        memberships=_memberships("backend-a"),
    )
    await _admit_resident(first, resource_id, lease_id="initial-lease")
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="initial-lease",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"

    admission, transition = await asyncio.gather(
        first.admit(
            resource_id,
            require_resident=True,
            **_admission_kwargs("backend-a", "race-lease"),
        ),
        second.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="drain-owner",
            generation="2",
            operation="drain",
            ttl_ms=30_000,
            require_idle=True,
        ),
    )

    assert (admission.status, transition.status) in {
        ("admitted", "active_leases"),
        ("transition_in_progress", "acquired"),
    }
    snapshot = await first.snapshot(resource_id)
    assert len(snapshot.allocations) == 1
    assert snapshot.allocations[0].state is GPUAllocationState.RESIDENT
    assert all(
        allocation.state is not GPUAllocationState.RESERVING
        for allocation in snapshot.allocations
    )


@pytest.mark.asyncio
async def test_two_independent_clients_cannot_oversell_capacity(redis_stores) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)

    first_result, second_result = await asyncio.gather(
        first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a")),
        second.admit(
            resource_id,
            **_admission_kwargs("backend-b", "lease-b", owner_id="owner-b"),
        ),
    )

    assert sorted([first_result.status, second_result.status]) == [
        "admitted",
        "capacity_unavailable",
    ]
    snapshot = await first.snapshot(resource_id)
    assert snapshot.committed_mb == 60
    assert len(snapshot.allocations) == 1
    assert len(snapshot.leases) == 1
    assert snapshot.committed_mb <= snapshot.allocatable_mb


@pytest.mark.asyncio
async def test_exact_capacity_is_allowed_and_cached_counter_cannot_oversell(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-a", budget_mb=61),
        )
    ).admitted

    exact = await second.admit(
        resource_id,
        **_admission_kwargs("backend-b", "lease-b", budget_mb=39, owner_id="owner-b"),
    )
    assert exact.admitted
    one_mb_over = await first.admit(
        resource_id,
        **_admission_kwargs("backend-c", "lease-c", budget_mb=1, owner_id="owner-c"),
    )
    assert one_mb_over.status == "capacity_unavailable"
    assert one_mb_over.committed_mb == 100
    assert (await first.snapshot(resource_id)).committed_mb == 100

    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(first.keys(resource_id).card, "committed_mb", "0")
    finally:
        await raw.aclose()
    drifted = await second.admit(
        resource_id,
        **_admission_kwargs(
            "backend-c", "lease-drift", budget_mb=1, owner_id="owner-c"
        ),
    )
    assert drifted.status == "ledger_corrupt"
    assert drifted.reason == "allocation_cache_drift"


@pytest.mark.asyncio
async def test_allocation_and_global_backend_lease_are_atomic(redis_stores) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        lease_id="lease-0",
        owner_id="owner-0",
        max_concurrency=2,
    )

    request_indices = range(1, 8)
    results = await asyncio.gather(
        *(
            (first if index % 2 == 0 else second).admit(
                resource_id,
                **_admission_kwargs(
                    "backend-a",
                    f"lease-{index}",
                    max_concurrency=2,
                    owner_id=f"owner-{index}",
                ),
            )
            for index in request_indices
        )
    )

    assert sum(result.admitted for result in results) == 1
    assert sum(result.status == "concurrency_saturated" for result in results) == 6
    snapshot = await first.snapshot(resource_id)
    assert snapshot.committed_mb == 60
    assert len(snapshot.allocations) == 1
    assert len(snapshot.leases) == 2

    lease_index = next(
        index
        for index, result in zip(request_indices, results, strict=True)
        if result.admitted
    )
    repeated = await second.admit(
        resource_id,
        **_admission_kwargs(
            "backend-a",
            f"lease-{lease_index}",
            max_concurrency=2,
            owner_id=f"owner-{lease_index}",
        ),
    )
    assert repeated.admitted is True
    assert repeated.idempotent is True
    assert repeated.lease_count == 2


@pytest.mark.asyncio
async def test_backend_fifo_prevents_new_requests_bypassing_waiters(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        lease_id="lease-initial",
        max_concurrency=1,
    )

    ticket_one = await first.enqueue_backend(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="ticket-1",
        owner_id="waiter-1",
        ttl_ms=30_000,
    )
    ticket_two = await second.enqueue_backend(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="ticket-2",
        owner_id="waiter-2",
        ttl_ms=30_000,
    )
    assert (ticket_one.position, ticket_two.position) == (1, 2)
    card_ticket = await first.enqueue_card(
        resource_id,
        backend_id="backend-b",
        membership_epoch=1,
        ticket_id="card-ticket",
        owner_id="card-waiter",
        ttl_ms=30_000,
    )
    assert card_ticket.position == 1

    released = await first.release_lease(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-initial",
        owner_id="owner-a",
        generation="1",
    )
    assert released.status == "released"

    bypass = await second.admit(
        resource_id,
        require_resident=True,
        **_admission_kwargs(
            "backend-a", "lease-bypass", max_concurrency=1, owner_id="bypass"
        ),
    )
    assert bypass.status == "concurrency_queued"

    first_waiter = await second.admit(
        resource_id,
        require_resident=True,
        backend_ticket_id="ticket-1",
        **_admission_kwargs(
            "backend-a", "lease-waiter-1", max_concurrency=1, owner_id="waiter-1"
        ),
    )
    assert first_waiter.admitted
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-b",
            ticket_id="card-ticket",
            card_queue=True,
        )
    ).position == 1
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="ticket-2",
            card_queue=False,
        )
    ).position == 1
    assert (
        await first.cancel_queue_ticket(
            resource_id,
            backend_id="backend-a",
            ticket_id="ticket-2",
            owner_id="waiter-2",
            card_queue=False,
        )
    ).status == "cancelled"
    assert (
        await first.cancel_queue_ticket(
            resource_id,
            backend_id="backend-b",
            ticket_id="card-ticket",
            owner_id="card-waiter",
            card_queue=True,
        )
    ).status == "cancelled"


@pytest.mark.asyncio
async def test_missing_or_expired_ticket_cannot_bypass_empty_fifo(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-ticket-presence/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-initial",
        owner_id="owner-initial",
        budget_mb=60,
        max_concurrency=1,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-initial",
            owner_id="owner-initial",
            generation="1",
        )
    ).status == "released"

    missing_backend = await first.admit(
        resource_id,
        backend_ticket_id="missing-backend-ticket",
        require_resident=True,
        **_admission_kwargs(
            "backend-a",
            "lease-missing-ticket",
            owner_id="owner-missing-ticket",
            budget_mb=60,
            max_concurrency=1,
        ),
    )
    assert (missing_backend.status, missing_backend.reason) == (
        "concurrency_queued",
        "backend_fifo_wait",
    )

    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="expired-backend-ticket",
            owner_id="owner-expired-ticket",
            ttl_ms=1,
        )
    ).status == "queued"
    await asyncio.sleep(0.005)
    expired_backend = await first.admit(
        resource_id,
        backend_ticket_id="expired-backend-ticket",
        require_resident=True,
        **_admission_kwargs(
            "backend-a",
            "lease-expired-ticket",
            owner_id="owner-expired-ticket",
            budget_mb=60,
            max_concurrency=1,
        ),
    )
    assert (expired_backend.status, expired_backend.reason) == (
        "concurrency_queued",
        "backend_fifo_wait",
    )

    missing_card = await first.admit(
        resource_id,
        card_ticket_id="missing-card-ticket",
        **_admission_kwargs(
            "backend-b",
            "lease-missing-card-ticket",
            owner_id="owner-missing-card-ticket",
            budget_mb=40,
        ),
    )
    assert (missing_card.status, missing_card.reason) == (
        "card_queued",
        "card_fifo_wait",
    )
    assert len((await first.snapshot(resource_id)).leases) == 0


@pytest.mark.asyncio
async def test_queue_ticket_and_transition_owner_require_exact_owner(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await first.enqueue_backend(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="ticket-a",
        owner_id="queue-owner",
        ttl_ms=30_000,
    )
    wrong_queue_owner = await second.cancel_queue_ticket(
        resource_id,
        backend_id="backend-a",
        ticket_id="ticket-a",
        owner_id="other-owner",
        card_queue=False,
    )
    assert wrong_queue_owner.status == "owner_mismatch"
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="ticket-a",
            card_queue=False,
        )
    ).position == 1
    backend_queue_snapshot = await first.snapshot(resource_id)
    assert backend_queue_snapshot.card_queue == ()
    assert [
        (
            ticket.ticket_id,
            ticket.backend_id,
            ticket.owner_id,
            ticket.kind,
            ticket.membership_epoch,
        )
        for ticket in backend_queue_snapshot.backend_queues
    ] == [("ticket-a", "backend-a", "queue-owner", "backend", 1)]
    assert (
        await first.cancel_queue_ticket(
            resource_id,
            backend_id="backend-a",
            ticket_id="ticket-a",
            owner_id="queue-owner",
            card_queue=False,
        )
    ).status == "cancelled"

    await first.enqueue_card(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="card-ticket",
        owner_id="queue-owner",
        ttl_ms=30_000,
    )
    wrong_card_backend = await second.cancel_queue_ticket(
        resource_id,
        backend_id="backend-b",
        ticket_id="card-ticket",
        owner_id="queue-owner",
        card_queue=True,
    )
    assert wrong_card_backend.status == "ticket_conflict"
    assert (
        await second.queue_position(
            resource_id,
            backend_id="backend-b",
            ticket_id="card-ticket",
            card_queue=True,
        )
    ).status == "ticket_conflict"
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="card-ticket",
            card_queue=True,
        )
    ).position == 1
    card_queue_snapshot = await first.snapshot(resource_id)
    assert card_queue_snapshot.backend_queues == ()
    assert [
        (
            ticket.ticket_id,
            ticket.backend_id,
            ticket.owner_id,
            ticket.kind,
            ticket.membership_epoch,
        )
        for ticket in card_queue_snapshot.card_queue
    ] == [("card-ticket", "backend-a", "queue-owner", "card", 1)]
    assert (
        await first.cancel_queue_ticket(
            resource_id,
            backend_id="backend-a",
            ticket_id="card-ticket",
            owner_id="queue-owner",
            card_queue=True,
        )
    ).status == "cancelled"

    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        budget_mb=50,
    )
    await _admit_resident(
        second,
        resource_id,
        backend_id="backend-b",
        lease_id="lease-b",
        owner_id="owner-b",
        budget_mb=50,
    )
    for store, backend_id, lease_id, owner_id in (
        (first, "backend-a", "lease-a", "owner-a"),
        (second, "backend-b", "lease-b", "owner-b"),
    ):
        assert (
            await store.release_lease(
                resource_id,
                backend_id=backend_id,
                lease_id=lease_id,
                owner_id=owner_id,
                generation="1",
            )
        ).status == "released"

    acquired = await first.acquire_transition_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="transition-owner",
        generation="2",
        operation="drain",
        ttl_ms=30,
    )
    assert acquired.status == "acquired"
    assert acquired.idempotent is False
    repeated = await second.acquire_transition_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="transition-owner",
        generation="2",
        operation="drain",
        ttl_ms=30,
    )
    assert repeated.status == "acquired"
    assert repeated.idempotent is True
    blocked = await second.acquire_transition_owner(
        resource_id,
        backend_id="backend-b",
        membership_epoch=1,
        owner_id="other-owner",
        generation="1",
        operation="drain",
        ttl_ms=30,
    )
    assert blocked.status == "busy"
    wrong_release = await second.release_transition_owner(
        resource_id,
        backend_id="backend-a",
        owner_id="other-owner",
        generation="2",
        operation="drain",
    )
    assert wrong_release.status == "owner_mismatch"

    deadline = asyncio.get_running_loop().time() + 1
    while True:
        takeover = await second.acquire_transition_owner(
            resource_id,
            backend_id="backend-b",
            membership_epoch=1,
            owner_id="other-owner",
            generation="3",
            operation="drain",
            ttl_ms=30,
        )
        if takeover.status == "acquired":
            break
        assert takeover.status == "busy"
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("transition owner did not expire")
        await asyncio.sleep(0.005)
    assert takeover.status == "acquired"
    late_release = await first.release_transition_owner(
        resource_id,
        backend_id="backend-a",
        owner_id="transition-owner",
        generation="2",
        operation="drain",
    )
    assert late_release.status == "owner_mismatch"


@pytest.mark.asyncio
async def test_snapshot_rejects_queue_ticket_from_future_membership_epoch(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-corrupt-queue-epoch/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="future-epoch-ticket",
            owner_id="queue-owner",
            ttl_ms=30_000,
        )
    ).status == "queued"
    queue_key = first.keys(resource_id).backend_queue("backend-a")
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        ticket = json.loads(await raw.lindex(queue_key, 0))
        ticket["membership_epoch"] = "2"
        await raw.lset(queue_key, 0, json.dumps(ticket, separators=(",", ":")))

        with pytest.raises(GPUArbiterStoreError, match="GPU ledger decode failed"):
            await first.snapshot(resource_id)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_uncertain_stale_reservation_remains_owned_until_terminal_release(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    kwargs = _admission_kwargs(
        "backend-a", "lease-a", max_concurrency=1, owner_id="owner-a"
    )
    kwargs["heartbeat_ttl_ms"] = 20
    kwargs["hard_ttl_ms"] = 40
    assert (await first.admit(resource_id, **kwargs)).admitted

    wrong_owner = await first.mark_lease_uncertain(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-b",
        generation="1",
    )
    assert wrong_owner.status == "owner_mismatch"
    uncertain = await first.mark_lease_uncertain(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    assert uncertain.lease_state is GPURequestLeaseState.UNCERTAIN

    deadline = asyncio.get_running_loop().time() + 1
    while True:
        changed, total = await first.sweep_expired_leases(
            resource_id, backend_id="backend-a"
        )
        if changed:
            break
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("uncertain lease did not become stale")
        await asyncio.sleep(0.005)
    assert (changed, total) == (1, 1)
    snapshot = await first.snapshot(resource_id)
    assert snapshot.leases[0].state is GPURequestLeaseState.STALE
    assert snapshot.committed_mb == 60

    blocked = await first.admit(
        resource_id,
        **_admission_kwargs(
            "backend-a", "lease-b", max_concurrency=1, owner_id="owner-b"
        ),
    )
    assert (blocked.status, blocked.reason) == (
        "not_ready",
        "cold_allocation_in_progress",
    )

    reservation_guard = await first.release_lease(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    assert reservation_guard.status == "reservation_active"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.UNKNOWN,
            request_lease_id="lease-a",
            request_owner_id="owner-a",
        )
    ).status == "transitioned"
    released = await first.release_lease(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    assert released.status == "released"
    repeated = await first.release_lease(
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    assert repeated.status == "missing"


@pytest.mark.asyncio
async def test_generation_cas_and_active_lease_guard_transitions(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a"))
    ).admitted

    stale = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        target_state=GPUAllocationState.LOADING,
    )
    assert stale.status == "stale_generation"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="lease-a",
            request_owner_id="owner-a",
        )
    ).status == "transitioned"
    with pytest.raises(
        ValueError,
        match="Resident cold terminal requires finalize_cold_allocation",
    ):
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.RESIDENT,
            request_lease_id="lease-a",
            request_owner_id="owner-a",
        )
    assert (
        await first.finalize_cold_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            request_lease_id="lease-a",
            request_owner_id="owner-a",
            target_state=GPUAllocationState.RESIDENT,
            target_evictable=True,
            resident_cooldown_ms=1,
        )
    ).status == "transitioned"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.DRAINING,
            next_generation="2",
            transition_owner_id="transition-owner",
            transition_operation="evict",
        )
    ).status == "transitioned"
    guarded = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADING,
        transition_owner_id="transition-owner",
        transition_operation="evict",
    )
    assert guarded.status == "active_leases"

    # A request from the old generation may still release its own lease.
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    unloading = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADING,
        transition_owner_id="transition-owner",
        transition_operation="evict",
    )
    assert unloading.status == "transitioned"
    unloaded = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADED,
        transition_owner_id="transition-owner",
        transition_operation="evict",
    )
    assert unloaded.status == "transitioned"
    assert unloaded.committed_mb == 0


@pytest.mark.asyncio
async def test_begin_idle_eviction_selects_priority_lru_and_replays_exactly(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        budget_mb=30,
        eviction_priority=1,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="lease-b",
        owner_id="owner-b",
        budget_mb=30,
        eviction_priority=0,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"

    stale = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=1,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (stale.status, stale.reason, stale.victim_backend_id) == (
        "stale_selection",
        "victim_order_or_generation_changed",
        "backend-b",
    )
    before = await first.snapshot(resource_id)
    selected = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=1,
        victim_backend_id="backend-b",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (selected.status, selected.idempotent) == ("selected", False)
    assert selected.shortfall_mb == 10
    assert selected.victim_budget_mb == 30
    assert selected.owner_expires_at_ms is not None
    assert selected.owner_hard_deadline_ms is not None
    assert selected.owner_expires_at_ms < selected.owner_hard_deadline_ms
    after = await first.snapshot(resource_id)
    allocations = {item.backend_id: item for item in after.allocations}
    assert after.ledger_revision == before.ledger_revision + 1
    assert after.committed_mb == 60
    assert allocations["backend-b"].state is GPUAllocationState.DRAINING
    assert allocations["backend-b"].generation == "2"

    retried = await second.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=1,
        victim_backend_id="backend-b",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (retried.status, retried.idempotent) == ("selected", True)
    assert retried.owner_expires_at_ms == selected.owner_expires_at_ms
    assert (await first.snapshot(resource_id)).ledger_revision == after.ledger_revision

    conflict = await second.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=1,
        victim_backend_id="backend-b",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="other-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert conflict.status == "transition_in_progress"
    renewed = await first.heartbeat_transition_owner(
        resource_id,
        backend_id="backend-b",
        owner_id="eviction-owner",
        generation="2",
        operation="evict",
        ttl_ms=60_000,
    )
    assert renewed.status == "renewed"
    assert renewed.expires_at_ms == selected.owner_hard_deadline_ms
    late = await second.admit(
        resource_id,
        **_admission_kwargs(
            "backend-b",
            "lease-late",
            budget_mb=30,
            generation="2",
            owner_id="late-owner",
            eviction_priority=0,
        ),
    )
    assert late.status == "transition_in_progress"


@pytest.mark.asyncio
async def test_idle_eviction_freezes_unload_branch_before_unloading(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-idle-unload-branch/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        budget_mb=60,
        eviction_priority=0,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=1,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="idle-unload-owner",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
        )
    ).status == "selected"

    unloading = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADING,
        transition_owner_id="idle-unload-owner",
    )

    assert unloading.status == "transitioned"
    snapshot = await first.snapshot(resource_id)
    assert snapshot.transition is not None
    assert snapshot.transition["eviction_branch"] == "unload"
    assert await first._redis.pttl(first.keys(resource_id).transition) == -1

    unloaded = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.UNLOADING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADED,
        transition_owner_id="idle-unload-owner",
    )

    assert (unloaded.status, unloaded.committed_mb) == ("transitioned", 0)
    terminal = await first.snapshot(resource_id)
    assert terminal.allocations[0].state is GPUAllocationState.UNLOADED
    assert terminal.allocations[0].evictable is False
    assert terminal.transition is not None
    assert terminal.transition["eviction_branch"] == "unload"


@pytest.mark.asyncio
async def test_begin_idle_eviction_uses_lru_then_backend_id_tie_break(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="lease-b",
        owner_id="owner-b",
        budget_mb=30,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"
    await asyncio.sleep(0.003)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        budget_mb=30,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"

    lru = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=0,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (lru.status, lru.victim_backend_id) == (
        "stale_selection",
        "backend-b",
    )

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        for backend_id in ("backend-a", "backend-b"):
            allocation = json.loads(await raw.hget(keys.allocations, backend_id))
            allocation["last_used_at_ms"] = 1
            await raw.hset(keys.allocations, backend_id, json.dumps(allocation))
    finally:
        await raw.aclose()

    tied = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=0,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (tied.status, tied.victim_backend_id) == ("selected", "backend-a")


@pytest.mark.asyncio
async def test_begin_idle_eviction_waits_for_cumulative_residency_cooldown(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-cooldown/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    for backend_id, cooldown_ms in (("backend-a", 40), ("backend-b", 80)):
        await _admit_resident(
            first,
            resource_id,
            backend_id=backend_id,
            lease_id=f"lease-{backend_id}",
            owner_id=f"owner-{backend_id}",
            budget_mb=30,
            resident_cooldown_ms=cooldown_ms,
            wait_for_cooldown=False,
        )
        assert (
            await first.release_lease(
                resource_id,
                backend_id=backend_id,
                lease_id=f"lease-{backend_id}",
                owner_id=f"owner-{backend_id}",
                generation="1",
            )
        ).status == "released"

    before = await first.snapshot(resource_id)
    retry_at_ms = max(
        allocation.not_evict_before_ms for allocation in before.allocations
    )
    kwargs = {
        "requester_backend_id": "backend-c",
        "requester_membership_epoch": 1,
        "requester_budget_mb": 80,
        "requester_eviction_priority": 0,
        "victim_backend_id": "backend-a",
        "victim_membership_epoch": 1,
        "victim_expected_generation": "1",
        "victim_next_generation": "2",
        "owner_id": "cooldown-owner",
        "ttl_ms": 30_000,
        "hard_ttl_ms": 60_000,
    }
    cooling = await first.begin_idle_eviction(resource_id, **kwargs)

    assert (cooling.status, cooling.reason, cooling.retry_at_ms) == (
        "cooldown_active",
        "eligible_victim_cooldown_active",
        retry_at_ms,
    )
    unchanged = await first.snapshot(resource_id)
    assert unchanged.ledger_revision == before.ledger_revision
    assert unchanged.transition_present is False
    assert tuple(
        (item.backend_id, item.state, item.generation) for item in unchanged.allocations
    ) == tuple(
        (item.backend_id, item.state, item.generation) for item in before.allocations
    )

    await asyncio.sleep(max(0, retry_at_ms - unchanged.observed_at_ms) / 1000 + 0.005)
    selected = await first.begin_idle_eviction(resource_id, **kwargs)
    assert (selected.status, selected.victim_backend_id) == (
        "selected",
        "backend-a",
    )


@pytest.mark.asyncio
async def test_begin_idle_eviction_rejects_resident_without_cooldown_deadline(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-invalid-resident-cooldown/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        allocation = json.loads(await raw.hget(keys.allocations, "backend-a"))
        allocation["not_evict_before_ms"] = 0
        corrupted = json.dumps(allocation)
        await raw.hset(keys.allocations, "backend-a", corrupted)

        rejected = await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-b",
            requester_membership_epoch=1,
            requester_budget_mb=60,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="invalid-cooldown-owner",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
        )

        assert rejected.status == "ledger_corrupt"
        assert await raw.get(keys.transition) is None
        assert await raw.hget(keys.allocations, "backend-a") == corrupted
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_begin_idle_eviction_is_side_effect_free_without_safe_capacity(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        budget_mb=60,
        eviction_priority=0,
    )
    capacity_before = await first.snapshot(resource_id)
    capacity = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=40,
        requester_eviction_priority=0,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert capacity.status == "capacity_available"
    assert (await first.snapshot(resource_id)).ledger_revision == (
        capacity_before.ledger_revision
    )

    busy = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=0,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (busy.status, busy.reason) == (
        "victim_busy",
        "eligible_victim_has_leases",
    )
    assert (await first.snapshot(resource_id)).transition_present is False

    resource_id = "node-a/index:1"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="lease-a-small",
        owner_id="owner-a-small",
        budget_mb=30,
        eviction_priority=0,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a-small",
            owner_id="owner-a-small",
            generation="1",
        )
    ).status == "released"
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="lease-b",
        owner_id="owner-b",
        budget_mb=50,
        eviction_priority=2,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"
    insufficient_before = await first.snapshot(resource_id)
    insufficient = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=60,
        requester_eviction_priority=1,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="eviction-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
    )
    assert (insufficient.status, insufficient.reason) == (
        "capacity_unavailable",
        "eligible_capacity_insufficient",
    )
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == insufficient_before.ledger_revision
    assert after.transition_present is False
    assert all(item.state is GPUAllocationState.RESIDENT for item in after.allocations)


@pytest.mark.asyncio
async def test_begin_busy_eviction_preserves_leases_and_closes_new_admission(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-eviction/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        budget_mb=40,
        eviction_priority=-1,
    )
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-b",
        lease_id="lease-b",
        owner_id="owner-b",
        budget_mb=40,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.enqueue_card(
            resource_id,
            backend_id="backend-c",
            membership_epoch=1,
            ticket_id="busy-card-ticket",
            owner_id="busy-card-owner",
            ttl_ms=30_000,
        )
    ).position == 1
    common = {
        "requester_backend_id": "backend-c",
        "requester_membership_epoch": 1,
        "requester_budget_mb": 40,
        "requester_eviction_priority": 0,
        "victim_backend_id": "backend-a",
        "victim_membership_epoch": 1,
        "victim_expected_generation": "1",
        "victim_next_generation": "2",
        "owner_id": "busy-eviction-owner",
        "ttl_ms": 30_000,
        "hard_ttl_ms": 60_000,
        "allow_busy": True,
        "requester_card_ticket_id": "busy-card-ticket",
        "requester_queue_owner_id": "busy-card-owner",
    }

    selected = await first.begin_idle_eviction(resource_id, **common)
    assert (selected.status, selected.idempotent) == ("selected", False)
    snapshot = await first.snapshot(resource_id)
    victim = next(
        item for item in snapshot.allocations if item.backend_id == "backend-a"
    )
    assert (victim.state, victim.generation) == (GPUAllocationState.DRAINING, "2")
    assert [(item.lease_id, item.generation) for item in snapshot.leases] == [
        ("lease-a", "1")
    ]

    replayed = await second.begin_idle_eviction(resource_id, **common)
    assert (replayed.status, replayed.idempotent) == ("selected", True)
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-c",
            ticket_id="busy-card-ticket",
            card_queue=True,
        )
    ).position == 1
    assert (
        await second.admit(
            resource_id,
            **_admission_kwargs(
                "backend-b",
                "lease-b-new",
                budget_mb=40,
                generation="1",
                owner_id="owner-b-new",
            ),
        )
    ).status == "admitted"
    assert (
        await second.admit(
            resource_id,
            **_admission_kwargs(
                "backend-a",
                "lease-late",
                generation="2",
                owner_id="owner-late",
            ),
        )
    ).status == "transition_in_progress"
    assert (
        await first.heartbeat_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
            heartbeat_ttl_ms=5_000,
        )
    ).status == "heartbeated"
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="busy-eviction-owner",
        )
    ).status == "active_leases"
    assert await first._redis.pttl(first.keys(resource_id).transition) > 0
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="busy-eviction-owner",
        )
    ).status == "transitioned"
    assert await first._redis.pttl(first.keys(resource_id).transition) == -1
    blocked_cancel = await second.arm_eviction_cancel(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        transition_owner_id="busy-eviction-owner",
    )
    assert (blocked_cancel.status, blocked_cancel.branch) == (
        "branch_conflict",
        "unload",
    )
    assert (
        await second.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.RESIDENT,
            next_generation="3",
            transition_owner_id="busy-eviction-owner",
        )
    ).status == "invalid_transition"


@pytest.mark.asyncio
async def test_begin_busy_eviction_still_obeys_residency_cooldown(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-busy-cooldown/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        resident_cooldown_ms=1_000,
        wait_for_cooldown=False,
    )
    before = await first.snapshot(resource_id)

    cooling = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_budget_mb=50,
        requester_eviction_priority=0,
        victim_backend_id="backend-a",
        victim_membership_epoch=1,
        victim_expected_generation="1",
        victim_next_generation="2",
        owner_id="busy-cooldown-owner",
        ttl_ms=30_000,
        hard_ttl_ms=60_000,
        allow_busy=True,
    )

    assert (cooling.status, cooling.reason) == (
        "cooldown_active",
        "eligible_victim_cooldown_active",
    )
    assert cooling.retry_at_ms is not None
    assert cooling.retry_at_ms > before.observed_at_ms
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.transition_present is False
    assert after.allocations[0].state is GPUAllocationState.RESIDENT


@pytest.mark.asyncio
async def test_busy_eviction_cancel_rolls_back_with_new_generation_and_leases(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-cancel/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    before_cancel = await first.snapshot(resource_id)
    original_cooldown = before_cancel.allocations[0].not_evict_before_ms
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="busy-cancel-owner",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        )
    ).status == "selected"

    before_arm = await first.snapshot(resource_id)
    assert before_arm.transition is not None
    assert (
        before_arm.transition["resource_id"],
        before_arm.transition["backend_id"],
        before_arm.transition["owner_id"],
        before_arm.transition["generation"],
        before_arm.transition["operation"],
        before_arm.transition["require_idle"],
        before_arm.transition.get("eviction_branch"),
    ) == (
        resource_id,
        "backend-a",
        "busy-cancel-owner",
        "2",
        "evict",
        False,
        None,
    )
    keys = first.keys(resource_id)
    transition_raw = await first._redis.get(keys.transition)
    assert transition_raw is not None
    incomplete_transition = json.loads(transition_raw)
    incomplete_transition.pop("requester_budget_mb")
    incomplete_raw = json.dumps(incomplete_transition, separators=(",", ":"))
    await first._redis.set(keys.transition, incomplete_raw, keepttl=True)
    await first._redis.hset(keys.card, "transition_mirror", incomplete_raw)
    with pytest.raises(
        GPUArbiterStoreError, match="GPU eviction transition is incomplete"
    ):
        await first.snapshot(resource_id)
    await first._redis.set(keys.transition, transition_raw, keepttl=True)
    await first._redis.hset(keys.card, "transition_mirror", transition_raw)

    armed = await first.arm_eviction_cancel(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        transition_owner_id="busy-cancel-owner",
    )
    assert (armed.status, armed.branch, armed.idempotent) == (
        "armed",
        "cancel",
        False,
    )
    assert await first._redis.pttl(first.keys(resource_id).transition) == -1
    replayed_arm = await second.arm_eviction_cancel(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        transition_owner_id="busy-cancel-owner",
    )
    assert (replayed_arm.status, replayed_arm.branch, replayed_arm.idempotent) == (
        "armed",
        "cancel",
        True,
    )
    after_arm = await first.snapshot(resource_id)
    assert after_arm.ledger_revision == before_arm.ledger_revision + 1
    assert after_arm.transition is not None
    assert after_arm.transition["eviction_branch"] == "cancel"
    assert await first._redis.pexpire(keys.transition, 30_000)
    with pytest.raises(
        GPUArbiterStoreError, match="GPU frozen transition expiry is invalid"
    ):
        await first.snapshot(resource_id)
    assert await first._redis.persist(keys.transition)
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="busy-cancel-owner",
            generation="2",
            operation="evict",
        )
    ).status == "invalid_transition"
    assert (
        await first.heartbeat_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="busy-cancel-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "renewed"
    assert await first._redis.pttl(first.keys(resource_id).transition) == -1

    cancelled = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.RESIDENT,
        next_generation="3",
        transition_owner_id="busy-cancel-owner",
    )
    assert (cancelled.status, cancelled.idempotent) == ("transitioned", False)
    after_cancel = await first.snapshot(resource_id)
    victim = next(
        item for item in after_cancel.allocations if item.backend_id == "backend-a"
    )
    assert (
        victim.state,
        victim.generation,
        victim.evictable,
        victim.not_evict_before_ms,
    ) == (
        GPUAllocationState.RESIDENT,
        "3",
        True,
        original_cooldown,
    )
    assert [(item.lease_id, item.generation) for item in after_cancel.leases] == [
        ("lease-a", "1")
    ]

    retry = await second.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.RESIDENT,
        next_generation="3",
        transition_owner_id="busy-cancel-owner",
    )
    assert (retry.status, retry.idempotent) == ("transitioned", True)
    assert (await first.snapshot(resource_id)).ledger_revision == (
        after_cancel.ledger_revision
    )
    assert (
        await first.heartbeat_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
            heartbeat_ttl_ms=5_000,
        )
    ).status == "heartbeated"
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    after_old_lease = await first.snapshot(resource_id)
    assert after_old_lease.allocations == after_cancel.allocations
    assert after_old_lease.leases == ()
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="busy-cancel-owner",
            generation="2",
            operation="evict",
        )
    ).status == "released"
    assert (
        await second.admit(
            resource_id,
            **_admission_kwargs(
                "backend-a",
                "lease-new",
                generation="3",
                owner_id="owner-new",
            ),
        )
    ).status == "admitted"


@pytest.mark.asyncio
async def test_busy_eviction_uncertainty_keeps_leases_in_unknown(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-busy-unknown/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="busy-unknown-owner",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        )
    ).status == "selected"

    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="busy-unknown-owner",
        )
    ).status == "armed"

    unknown = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNKNOWN,
        transition_owner_id="busy-unknown-owner",
    )
    assert (unknown.status, unknown.committed_mb) == ("transitioned", 60)
    snapshot = await first.snapshot(resource_id)
    victim = next(
        item for item in snapshot.allocations if item.backend_id == "backend-a"
    )
    assert (victim.state, victim.evictable) == (GPUAllocationState.UNKNOWN, False)
    assert [(item.lease_id, item.generation) for item in snapshot.leases] == [
        ("lease-a", "1")
    ]
    retry = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNKNOWN,
        transition_owner_id="busy-unknown-owner",
    )
    assert (retry.status, retry.idempotent) == ("transitioned", True)
    assert (
        await first.heartbeat_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
            heartbeat_ttl_ms=5_000,
        )
    ).status == "heartbeated"
    after_heartbeat = await first.snapshot(resource_id)
    assert after_heartbeat.allocations == snapshot.allocations
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    after_release = await first.snapshot(resource_id)
    assert after_release.leases == ()
    assert after_release.allocations[0].state is GPUAllocationState.UNKNOWN
    assert after_release.committed_mb == 60


@pytest.mark.asyncio
async def test_busy_eviction_cancel_and_unload_freeze_exactly_one_branch(
    redis_stores,
) -> None:
    first, second = redis_stores
    for index in range(8):
        resource_id = f"node-busy-branch-race-{index}/index:0"
        owner_id = f"busy-race-owner-{index}"
        await _bootstrap_empty_card(first, resource_id, 100)
        await _admit_resident(first, resource_id)
        assert (
            await first.release_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
            )
        ).status == "released"
        assert (
            await first.begin_idle_eviction(
                resource_id,
                requester_backend_id="backend-c",
                requester_membership_epoch=1,
                requester_budget_mb=50,
                requester_eviction_priority=0,
                victim_backend_id="backend-a",
                victim_membership_epoch=1,
                victim_expected_generation="1",
                victim_next_generation="2",
                owner_id=owner_id,
                ttl_ms=30_000,
                hard_ttl_ms=60_000,
                allow_busy=True,
            )
        ).status == "selected"
        before = await first.snapshot(resource_id)

        cancel_result, unload_result = await asyncio.gather(
            first.arm_eviction_cancel(
                resource_id,
                backend_id="backend-a",
                expected_generation="2",
                transition_owner_id=owner_id,
            ),
            second.transition_eviction_allocation(
                resource_id,
                backend_id="backend-a",
                expected_state=GPUAllocationState.DRAINING,
                expected_generation="2",
                target_state=GPUAllocationState.UNLOADING,
                transition_owner_id=owner_id,
            ),
        )
        assert await first._redis.pttl(first.keys(resource_id).transition) == -1
        after_branch = await first.snapshot(resource_id)
        assert after_branch.ledger_revision == before.ledger_revision + 1

        cancel_won = cancel_result.status == "armed"
        unload_won = unload_result.status == "transitioned"
        assert cancel_won is not unload_won
        if cancel_won:
            assert unload_result.status == "branch_conflict"
            terminal = await first.transition_eviction_allocation(
                resource_id,
                backend_id="backend-a",
                expected_state=GPUAllocationState.DRAINING,
                expected_generation="2",
                target_state=GPUAllocationState.RESIDENT,
                next_generation="3",
                transition_owner_id=owner_id,
            )
        else:
            assert (cancel_result.status, cancel_result.branch) == (
                "branch_conflict",
                "unload",
            )
            terminal = await first.transition_eviction_allocation(
                resource_id,
                backend_id="backend-a",
                expected_state=GPUAllocationState.UNLOADING,
                expected_generation="2",
                target_state=GPUAllocationState.UNLOADED,
                transition_owner_id=owner_id,
            )
        assert terminal.status == "transitioned"
        assert (
            await first.release_transition_owner(
                resource_id,
                backend_id="backend-a",
                owner_id=owner_id,
                generation="2",
                operation="evict",
            )
        ).status == "released"


@pytest.mark.asyncio
async def test_busy_eviction_branches_are_isolated_per_gpu_resource(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_cancel = "node-busy-branch-isolation/index:0"
    resource_unload = "node-busy-branch-isolation/index:1"
    await asyncio.gather(
        _bootstrap_empty_card(first, resource_cancel, 100),
        _bootstrap_empty_card(second, resource_unload, 100),
    )
    await asyncio.gather(
        _admit_resident(first, resource_cancel),
        _admit_resident(
            second,
            resource_unload,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
        ),
    )
    assert (
        await second.release_lease(
            resource_unload,
            backend_id="backend-b",
            lease_id="lease-b",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"
    selected_cancel, selected_unload = await asyncio.gather(
        first.begin_idle_eviction(
            resource_cancel,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="branch-isolation-cancel",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        ),
        second.begin_idle_eviction(
            resource_unload,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-b",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="branch-isolation-unload",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        ),
    )
    assert (selected_cancel.status, selected_unload.status) == (
        "selected",
        "selected",
    )

    cancel_branch, unload_branch = await asyncio.gather(
        first.arm_eviction_cancel(
            resource_cancel,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="branch-isolation-cancel",
        ),
        second.transition_eviction_allocation(
            resource_unload,
            backend_id="backend-b",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="branch-isolation-unload",
        ),
    )
    assert (cancel_branch.status, cancel_branch.branch) == ("armed", "cancel")
    assert unload_branch.status == "transitioned"
    assert (
        await first.transition_eviction_allocation(
            resource_cancel,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="branch-isolation-cancel",
        )
    ).status == "branch_conflict"
    late_cancel = await second.arm_eviction_cancel(
        resource_unload,
        backend_id="backend-b",
        expected_generation="2",
        transition_owner_id="branch-isolation-unload",
    )
    assert (late_cancel.status, late_cancel.branch) == (
        "branch_conflict",
        "unload",
    )

    cancelled, unloaded = await asyncio.gather(
        first.transition_eviction_allocation(
            resource_cancel,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.RESIDENT,
            next_generation="3",
            transition_owner_id="branch-isolation-cancel",
        ),
        second.transition_eviction_allocation(
            resource_unload,
            backend_id="backend-b",
            expected_state=GPUAllocationState.UNLOADING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADED,
            transition_owner_id="branch-isolation-unload",
        ),
    )
    assert cancelled.state is GPUAllocationState.RESIDENT
    assert unloaded.state is GPUAllocationState.UNLOADED
    released_cancel, released_unload = await asyncio.gather(
        first.release_transition_owner(
            resource_cancel,
            backend_id="backend-a",
            owner_id="branch-isolation-cancel",
            generation="2",
            operation="evict",
        ),
        second.release_transition_owner(
            resource_unload,
            backend_id="backend-b",
            owner_id="branch-isolation-unload",
            generation="2",
            operation="evict",
        ),
    )
    assert (released_cancel.status, released_unload.status) == (
        "released",
        "released",
    )


@pytest.mark.asyncio
async def test_frozen_cancel_branch_survives_owner_deadline_until_terminal(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-frozen-cancel/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-cancel-owner",
            ttl_ms=100,
            hard_ttl_ms=150,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="frozen-cancel-owner",
        )
    ).status == "armed"

    await asyncio.sleep(0.2)

    keys = first.keys(resource_id)
    assert await first._redis.pttl(keys.transition) == -1
    blocked_unload = await second.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADING,
        transition_owner_id="frozen-cancel-owner",
    )
    assert blocked_unload.status == "branch_conflict"
    assert (
        await second.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="replacement-owner",
            generation="3",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "invalid_transition"
    assert (
        await first.heartbeat_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="frozen-cancel-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "invalid_transition"
    assert await first._redis.pttl(keys.transition) == -1

    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNKNOWN,
            transition_owner_id="frozen-cancel-owner",
        )
    ).status == "transitioned"
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="frozen-cancel-owner",
            generation="2",
            operation="evict",
        )
    ).status == "released"
    assert await first._redis.exists(keys.transition) == 0


@pytest.mark.asyncio
async def test_frozen_unload_branch_survives_owner_deadline_until_terminal(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-frozen-unload/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-unload-owner",
            ttl_ms=100,
            hard_ttl_ms=150,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="frozen-unload-owner",
        )
    ).status == "transitioned"

    await asyncio.sleep(0.2)

    keys = first.keys(resource_id)
    assert await first._redis.pttl(keys.transition) == -1
    blocked_cancel = await second.arm_eviction_cancel(
        resource_id,
        backend_id="backend-a",
        expected_generation="2",
        transition_owner_id="frozen-unload-owner",
    )
    assert (blocked_cancel.status, blocked_cancel.branch) == (
        "branch_conflict",
        "unload",
    )
    assert (
        await first.heartbeat_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="frozen-unload-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "invalid_transition"
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="frozen-unload-owner",
            generation="2",
            operation="evict",
        )
    ).status == "invalid_transition"
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.UNLOADING,
            expected_generation="2",
            target_state=GPUAllocationState.UNKNOWN,
            transition_owner_id="frozen-unload-owner",
        )
    ).status == "transitioned"
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="frozen-unload-owner",
            generation="2",
            operation="evict",
        )
    ).status == "released"
    assert await first._redis.exists(keys.transition) == 0


@pytest.mark.parametrize("invalid_branch", ("invalid", None))
@pytest.mark.asyncio
async def test_frozen_branch_corruption_fails_closed_until_proof_reset(
    redis_stores,
    invalid_branch: str | None,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-frozen-corrupt/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-corrupt-owner",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="frozen-corrupt-owner",
        )
    ).status == "armed"
    marked = await first.mark_card_not_ready(
        resource_id,
        100,
        reason="membership_change",
    )
    keys = first.keys(resource_id)
    transition = json.loads(await first._redis.get(keys.transition))
    transition["eviction_branch"] = invalid_branch
    corrupted = json.dumps(transition, sort_keys=True, separators=(",", ":"))
    await first._redis.set(keys.transition, corrupted)
    await first._redis.hset(keys.card, "transition_mirror", corrupted)

    evolved = await second.evolve_backend_domains(
        resource_id,
        expected_ledger_revision=marked.ledger_revision,
        expected_ledger_incarnation=marked.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        evolution_id="corrupt-branch-evolution",
    )
    assert (evolved.status, evolved.reason) == (
        "ledger_corrupt",
        "transition_domain_invalid",
    )
    with pytest.raises(GPUArbiterStoreError, match="GPU eviction branch is invalid"):
        await first.snapshot(resource_id)

    prepared = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=marked.ledger_revision,
        expected_ledger_incarnation=marked.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        reset_id="corrupt-branch-reset",
    )
    assert prepared.status == "prepared"
    committed = await first.commit_proof_reset(
        resource_id,
        100,
        reset_id="corrupt-branch-reset",
        expected_reset_revision=prepared.ledger_revision,
        expected_reset_incarnation=prepared.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(
            _reconcile_allocation(
                backend_id="backend-a",
                state=GPUAllocationState.UNKNOWN,
                generation="2",
            ),
        ),
        ready=False,
        evidence_deadline_ms=0,
        proof_fingerprint=hashlib.sha256(b"corrupt-branch-proof").hexdigest(),
    )
    assert (committed.status, committed.ready, committed.committed_mb) == (
        "not_ready",
        False,
        60,
    )
    assert await first._redis.exists(keys.transition) == 0
    recovered = await first.snapshot(resource_id)
    assert recovered.transition_present is False
    assert recovered.allocations[0].state is GPUAllocationState.UNKNOWN


@pytest.mark.parametrize("owner_action", ("heartbeat", "release"))
@pytest.mark.asyncio
async def test_invalid_frozen_branch_owner_actions_fail_closed(
    redis_stores,
    owner_action: str,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-frozen-owner-corrupt/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-owner-corrupt",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="frozen-owner-corrupt",
        )
    ).status == "armed"
    keys = first.keys(resource_id)
    transition = json.loads(await first._redis.get(keys.transition))
    transition["eviction_branch"] = "invalid"
    corrupted = json.dumps(transition, sort_keys=True, separators=(",", ":"))
    await first._redis.set(keys.transition, corrupted)
    await first._redis.hset(keys.card, "transition_mirror", corrupted)

    owner_kwargs = {
        "backend_id": "backend-a",
        "owner_id": "frozen-owner-corrupt",
        "generation": "2",
        "operation": "evict",
    }
    if owner_action == "heartbeat":
        result = await second.heartbeat_transition_owner(
            resource_id,
            ttl_ms=30_000,
            **owner_kwargs,
        )
    else:
        result = await second.release_transition_owner(
            resource_id,
            **owner_kwargs,
        )
    assert result.status == "ledger_corrupt"
    card = await first._redis.hgetall(keys.card)
    assert card["bootstrap_state"] == "not_ready"
    assert card["not_ready_reason"] == "transition_invalid"


@pytest.mark.asyncio
async def test_frozen_branch_invalid_hard_deadline_fails_closed(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-busy-frozen-deadline-corrupt/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-deadline-corrupt",
            ttl_ms=30_000,
            hard_ttl_ms=60_000,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="frozen-deadline-corrupt",
        )
    ).status == "armed"
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    transition = json.loads(await first._redis.get(keys.transition))
    transition["hard_deadline_ms"] = transition["expires_at_ms"] - 1
    corrupted = json.dumps(transition, sort_keys=True, separators=(",", ":"))
    await first._redis.set(keys.transition, corrupted)
    await first._redis.hset(keys.card, "transition_mirror", corrupted)

    with pytest.raises(
        GPUArbiterStoreError,
        match="GPU transition deadline is invalid",
    ):
        await first.snapshot(resource_id)
    reconciled = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=before.allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="frozen-deadline-corrupt-repair",
    )
    assert (reconciled.status, reconciled.reason) == (
        "ledger_corrupt",
        "transition_invalid",
    )


@pytest.mark.parametrize("owner_action", ("heartbeat", "release"))
@pytest.mark.asyncio
async def test_missing_expired_frozen_branch_key_fails_closed(
    redis_stores,
    owner_action: str,
) -> None:
    first, second = redis_stores
    resource_id = "node-busy-frozen-missing/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.begin_idle_eviction(
            resource_id,
            requester_backend_id="backend-c",
            requester_membership_epoch=1,
            requester_budget_mb=50,
            requester_eviction_priority=0,
            victim_backend_id="backend-a",
            victim_membership_epoch=1,
            victim_expected_generation="1",
            victim_next_generation="2",
            owner_id="frozen-missing-owner",
            ttl_ms=100,
            hard_ttl_ms=150,
            allow_busy=True,
        )
    ).status == "selected"
    assert (
        await first.arm_eviction_cancel(
            resource_id,
            backend_id="backend-a",
            expected_generation="2",
            transition_owner_id="frozen-missing-owner",
        )
    ).status == "armed"
    keys = first.keys(resource_id)
    await first._redis.delete(keys.transition)
    await asyncio.sleep(0.2)

    owner_kwargs = {
        "backend_id": "backend-a",
        "owner_id": "frozen-missing-owner",
        "generation": "2",
        "operation": "evict",
    }
    if owner_action == "heartbeat":
        rejected = await second.heartbeat_transition_owner(
            resource_id,
            ttl_ms=30_000,
            **owner_kwargs,
        )
    else:
        rejected = await second.release_transition_owner(
            resource_id,
            **owner_kwargs,
        )
    assert rejected.status == "ledger_corrupt"
    card = await first._redis.hgetall(keys.card)
    assert card["bootstrap_state"] == "not_ready"
    assert card["not_ready_reason"] == "transition_key_missing_before_expiry"


@pytest.mark.asyncio
async def test_begin_idle_eviction_requires_exact_live_card_fifo_head(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-eviction-fifo/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(
        first,
        resource_id,
        backend_id="backend-a",
        lease_id="victim-lease",
        owner_id="victim-owner",
        budget_mb=60,
    )
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="victim-lease",
            owner_id="victim-owner",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.enqueue_card(
            resource_id,
            backend_id="backend-c",
            membership_epoch=1,
            ticket_id="card-head",
            owner_id="dispatch-head",
            ttl_ms=30_000,
        )
    ).position == 1
    assert (
        await second.enqueue_card(
            resource_id,
            backend_id="backend-b",
            membership_epoch=1,
            ticket_id="card-second",
            owner_id="dispatch-second",
            ttl_ms=30_000,
        )
    ).position == 2

    common = {
        "requester_budget_mb": 50,
        "requester_eviction_priority": 0,
        "victim_backend_id": "backend-a",
        "victim_membership_epoch": 1,
        "victim_expected_generation": "1",
        "victim_next_generation": "2",
        "owner_id": "eviction-owner",
        "ttl_ms": 30_000,
        "hard_ttl_ms": 60_000,
    }
    missing = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_card_ticket_id="missing-card-ticket",
        requester_queue_owner_id="dispatch-head",
        **common,
    )
    assert (missing.status, missing.reason) == ("card_queued", "card_fifo_wait")
    out_of_order = await second.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-b",
        requester_membership_epoch=1,
        requester_card_ticket_id="card-second",
        requester_queue_owner_id="dispatch-second",
        **common,
    )
    assert (out_of_order.status, out_of_order.reason) == (
        "card_queued",
        "card_fifo_wait",
    )
    assert (await first.snapshot(resource_id)).transition_present is False

    selected = await first.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_card_ticket_id="card-head",
        requester_queue_owner_id="dispatch-head",
        **common,
    )
    assert selected.status == "selected"
    assert selected.victim_backend_id == "backend-a"
    replayed = await second.begin_idle_eviction(
        resource_id,
        requester_backend_id="backend-c",
        requester_membership_epoch=1,
        requester_card_ticket_id="card-head",
        requester_queue_owner_id="dispatch-head",
        **common,
    )
    assert (replayed.status, replayed.idempotent) == ("selected", True)
    assert (
        await first.queue_position(
            resource_id,
            backend_id="backend-c",
            ticket_id="card-head",
            card_queue=True,
        )
    ).position == 1


@pytest.mark.asyncio
async def test_eviction_transition_is_exact_and_response_loss_idempotent(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="eviction-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
            require_idle=True,
        )
    ).status == "acquired"
    assert (
        await first.revalidate_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="eviction-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
            require_idle=True,
        )
    ).status == "renewed"

    draining = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.RESIDENT,
        expected_generation="1",
        target_state=GPUAllocationState.DRAINING,
        next_generation="2",
        transition_owner_id="eviction-owner",
    )
    assert (draining.status, draining.idempotent) == ("transitioned", False)
    after_draining = await first.snapshot(resource_id)
    draining_retry = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.RESIDENT,
        expected_generation="1",
        target_state=GPUAllocationState.DRAINING,
        next_generation="2",
        transition_owner_id="eviction-owner",
    )
    assert (draining_retry.status, draining_retry.idempotent) == (
        "transitioned",
        True,
    )
    assert (await first.snapshot(resource_id)).ledger_revision == (
        after_draining.ledger_revision
    )

    wrong_owner = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.RESIDENT,
        expected_generation="1",
        target_state=GPUAllocationState.DRAINING,
        next_generation="2",
        transition_owner_id="other-owner",
    )
    assert wrong_owner.status == "owner_mismatch"

    unloading = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADING,
        transition_owner_id="eviction-owner",
    )
    assert (unloading.status, unloading.idempotent) == ("transitioned", False)
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.DRAINING,
            expected_generation="2",
            target_state=GPUAllocationState.UNLOADING,
            transition_owner_id="eviction-owner",
        )
    ).idempotent is True

    unloaded = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.UNLOADING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADED,
        transition_owner_id="eviction-owner",
    )
    assert (unloaded.status, unloaded.idempotent, unloaded.committed_mb) == (
        "transitioned",
        False,
        0,
    )
    terminal_snapshot = await first.snapshot(resource_id)
    assert terminal_snapshot.allocations[0].evictable is False
    terminal_retry = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.UNLOADING,
        expected_generation="2",
        target_state=GPUAllocationState.UNLOADED,
        transition_owner_id="eviction-owner",
    )
    assert (terminal_retry.status, terminal_retry.idempotent) == (
        "transitioned",
        True,
    )
    assert (await first.snapshot(resource_id)).ledger_revision == (
        terminal_snapshot.ledger_revision
    )


@pytest.mark.asyncio
async def test_eviction_unknown_stays_counted_and_non_evictable(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="eviction-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
            require_idle=True,
        )
    ).status == "acquired"
    assert (
        await first.transition_eviction_allocation(
            resource_id,
            backend_id="backend-a",
            expected_state=GPUAllocationState.RESIDENT,
            expected_generation="1",
            target_state=GPUAllocationState.DRAINING,
            next_generation="2",
            transition_owner_id="eviction-owner",
        )
    ).status == "transitioned"
    unknown = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNKNOWN,
        transition_owner_id="eviction-owner",
    )
    assert (unknown.status, unknown.committed_mb) == ("transitioned", 60)
    snapshot = await first.snapshot(resource_id)
    assert snapshot.allocations[0].state is GPUAllocationState.UNKNOWN
    assert snapshot.allocations[0].evictable is False
    retry = await first.transition_eviction_allocation(
        resource_id,
        backend_id="backend-a",
        expected_state=GPUAllocationState.DRAINING,
        expected_generation="2",
        target_state=GPUAllocationState.UNKNOWN,
        transition_owner_id="eviction-owner",
    )
    assert (retry.status, retry.idempotent, retry.committed_mb) == (
        "transitioned",
        True,
        60,
    )


@pytest.mark.asyncio
async def test_expired_transition_owner_cannot_commit_after_takeover(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)

    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="old-owner",
            generation="2",
            operation="evict",
            ttl_ms=30,
        )
    ).status == "acquired"

    deadline = asyncio.get_running_loop().time() + 1
    while True:
        takeover = await second.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="new-owner",
            generation="3",
            operation="evict",
            ttl_ms=30_000,
        )
        if takeover.status == "acquired":
            break
        assert takeover.status == "busy"
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("transition owner did not become available")
        await asyncio.sleep(0.005)

    stale_owner = await first.transition_allocation(
        resource_id,
        backend_id="backend-a",
        expected_generation="1",
        target_state=GPUAllocationState.DRAINING,
        next_generation="2",
        transition_owner_id="old-owner",
        transition_operation="evict",
    )
    assert stale_owner.status == "owner_mismatch"
    assert (
        await second.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.DRAINING,
            next_generation="3",
            transition_owner_id="new-owner",
            transition_operation="evict",
        )
    ).status == "transitioned"


@pytest.mark.asyncio
async def test_idle_transition_owner_closes_the_admission_window(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)

    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-missing",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="1",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "missing"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="1",
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "stale_generation"
    active = await first.acquire_transition_owner(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        owner_id="transition-owner",
        generation="2",
        operation="evict",
        ttl_ms=30_000,
        require_idle=True,
    )
    assert active.status == "active_leases"
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="2",
            operation="evict",
            ttl_ms=30_000,
            require_idle=True,
        )
    ).status == "acquired"

    late_admission = await second.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-late", owner_id="owner-late"),
    )
    assert late_admission.status == "transition_in_progress"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.DRAINING,
            next_generation="2",
            transition_owner_id="transition-owner",
            transition_operation="evict",
        )
    ).status == "transitioned"
    assert len((await first.snapshot(resource_id)).leases) == 0


@pytest.mark.asyncio
async def test_generation_tombstone_is_strict_beyond_lua_safe_integer(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    initial_generation = "9007199254740992"
    drain_generation = "9007199254740993"
    next_generation = "9007199254740994"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id, generation=initial_generation)
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation=drain_generation,
            operation="evict",
            ttl_ms=30_000,
        )
    ).status == "acquired"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation=initial_generation,
            target_state=GPUAllocationState.DRAINING,
            next_generation=drain_generation,
            transition_owner_id="transition-owner",
            transition_operation="evict",
        )
    ).status == "transitioned"
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation=initial_generation,
        )
    ).status == "released"
    for state in (GPUAllocationState.UNLOADING, GPUAllocationState.UNLOADED):
        assert (
            await first.transition_allocation(
                resource_id,
                backend_id="backend-a",
                expected_generation=drain_generation,
                target_state=state,
                transition_owner_id="transition-owner",
                transition_operation="evict",
            )
        ).status == "transitioned"
    assert (
        await first.release_transition_owner(
            resource_id,
            backend_id="backend-a",
            owner_id="transition-owner",
            generation=drain_generation,
            operation="evict",
        )
    ).status == "released"

    for rejected_generation in (initial_generation, drain_generation):
        rejected = await first.admit(
            resource_id,
            **_admission_kwargs(
                "backend-a", "lease-rejected", generation=rejected_generation
            ),
        )
        assert rejected.status == "stale_generation"
    assert (
        await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-next", generation=next_generation),
        )
    ).admitted


@pytest.mark.asyncio
async def test_allocation_configuration_is_authoritative(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-a", max_concurrency=4),
        )
    ).admitted

    smaller_limit = await first.admit(
        resource_id,
        **_admission_kwargs(
            "backend-a",
            "lease-smaller",
            owner_id="owner-smaller",
            max_concurrency=1,
        ),
    )
    assert smaller_limit.status == "config_mismatch"
    larger_limit = await first.admit(
        resource_id,
        **_admission_kwargs(
            "backend-a",
            "lease-b",
            owner_id="owner-b",
            max_concurrency=8,
        ),
    )
    assert larger_limit.status == "config_mismatch"
    operation_retry_kwargs = _admission_kwargs(
        "backend-a", "lease-a", max_concurrency=4
    )
    operation_retry_kwargs["operation"] = "warmup"
    operation_retry = await first.admit(resource_id, **operation_retry_kwargs)
    assert operation_retry.status == "lease_conflict"
    assert operation_retry.reason == "idempotent_operation_mismatch"
    changed_retry = await first.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-a", max_concurrency=8),
    )
    assert changed_retry.status == "lease_conflict"
    assert changed_retry.reason == "idempotent_config_mismatch"
    assert len((await first.snapshot(resource_id)).leases) == 1


@pytest.mark.asyncio
async def test_reconcile_cannot_change_counted_allocation_evictability(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-counted-config/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    before = await first.snapshot(resource_id)
    changed = replace(before.allocations[0], evictable=False)

    rejected = await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(changed,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="change-counted-evictability",
    )
    assert rejected.status == "config_mismatch"
    after = await first.snapshot(resource_id)
    assert after.ledger_revision == before.ledger_revision
    assert after.allocations == before.allocations


@pytest.mark.asyncio
async def test_overcommit_disables_new_resident_fast_path(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)

    before = await first.snapshot(resource_id)
    repair = await first.reconcile_card(
        resource_id,
        50,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=before.allocations,
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="test-overcommit",
    )
    assert repair.status == "not_ready"
    assert repair.committed_mb == 60
    snapshot = await first.snapshot(resource_id)
    assert snapshot.ready is False
    assert snapshot.committed_mb == 60

    idempotent_retry = await first.admit(
        resource_id, **_admission_kwargs("backend-a", "lease-a")
    )
    assert idempotent_retry.status == "not_ready"
    blocked = await first.admit(
        resource_id,
        **_admission_kwargs("backend-a", "lease-b", owner_id="owner-b"),
    )
    assert blocked.status == "not_ready"
    assert len((await first.snapshot(resource_id)).leases) == 1


@pytest.mark.asyncio
async def test_card_fifo_and_resource_shards_are_independent(redis_stores) -> None:
    first, second = redis_stores
    resource_a = "node-a/index:0"
    resource_b = "node-a/index:1"
    await asyncio.gather(
        _bootstrap_empty_card(first, resource_a, 50),
        _bootstrap_empty_card(second, resource_b, 50),
    )
    await _admit_resident(
        second,
        resource_b,
        backend_id="backend-b",
        lease_id="lease-b-initial",
        owner_id="owner-b",
        budget_mb=50,
    )
    assert (
        await second.release_lease(
            resource_b,
            backend_id="backend-b",
            lease_id="lease-b-initial",
            owner_id="owner-b",
            generation="1",
        )
    ).status == "released"
    await first.enqueue_card(
        resource_a,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="card-ticket-a",
        owner_id="owner-a",
        ttl_ms=30_000,
    )
    await second.enqueue_card(
        resource_a,
        backend_id="backend-b",
        membership_epoch=1,
        ticket_id="card-ticket-b",
        owner_id="owner-b",
        ttl_ms=30_000,
    )

    out_of_order = await second.admit(
        resource_a,
        card_ticket_id="card-ticket-b",
        **_admission_kwargs("backend-b", "lease-a-b", budget_mb=50, owner_id="owner-b"),
    )
    assert out_of_order.status == "card_queued"

    # Card A and card B do not share ledger keys. The Redis service itself remains
    # a common infrastructure failure and latency domain.
    admitted_b = await second.admit(
        resource_b,
        require_resident=True,
        **_admission_kwargs("backend-b", "lease-b", budget_mb=50, owner_id="owner-b"),
    )
    assert admitted_b.admitted
    admitted_a = await first.admit(
        resource_a,
        card_ticket_id="card-ticket-a",
        **_admission_kwargs("backend-a", "lease-a", budget_mb=50),
    )
    assert admitted_a.admitted


@pytest.mark.asyncio
async def test_one_card_corrupt_ledger_does_not_poison_another_card(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_a = "node-a/index:0"
    resource_b = "node-a/index:1"
    await asyncio.gather(
        _bootstrap_empty_card(first, resource_a, 100),
        _bootstrap_empty_card(second, resource_b, 100),
    )

    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.set(first.keys(resource_a).allocations, "wrong-type")
        with pytest.raises(GPUArbiterStoreError, match="gpu_arbiter_unavailable"):
            await first.admit(
                resource_a,
                **_admission_kwargs("backend-a", "lease-a"),
            )
        assert (
            await second.admit(
                resource_b,
                **_admission_kwargs("backend-b", "lease-b"),
            )
        ).admitted
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_corrupt_queue_does_not_truncate_existing_tickets(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await first.enqueue_backend(
        resource_id,
        backend_id="backend-a",
        membership_epoch=1,
        ticket_id="ticket-a",
        owner_id="owner-a",
        ttl_ms=30_000,
    )
    queue_key = first.keys(resource_id).backend_queue("backend-a")
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.rpush(queue_key, "not-json")
        before = await raw.lrange(queue_key, 0, -1)
        result = await first.queue_position(
            resource_id,
            backend_id="backend-a",
            ticket_id="ticket-a",
            card_queue=False,
        )
        assert result.status == "ledger_corrupt"
        assert await raw.lrange(queue_key, 0, -1) == before
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_corrupt_sibling_allocation_cannot_partially_transition(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a"))
    ).admitted

    allocations_key = first.keys(resource_id).allocations
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        before = await raw.hget(allocations_key, "backend-a")
        await raw.hset(
            allocations_key,
            "backend-b",
            json.dumps({"backend_id": "backend-b", "state": "resident"}),
        )
        result = await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="lease-a",
            request_owner_id="owner-a",
        )
        assert result.status == "ledger_corrupt"
        assert await raw.hget(allocations_key, "backend-a") == before
    finally:
        await raw.aclose()


@pytest.mark.parametrize(
    "invalid_value",
    (None, -1, True, 9_007_199_254_740_992),
)
@pytest.mark.asyncio
async def test_snapshot_rejects_invalid_authoritative_cooldown_field(
    redis_stores,
    invalid_value: int | bool | None,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a"))
    ).admitted

    allocations_key = first.keys(resource_id).allocations
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        allocation = json.loads(await raw.hget(allocations_key, "backend-a"))
        if invalid_value is None:
            allocation.pop("not_evict_before_ms")
        else:
            allocation["not_evict_before_ms"] = invalid_value
        await raw.hset(allocations_key, "backend-a", json.dumps(allocation))
        with pytest.raises(GPUArbiterStoreError, match="ledger decode"):
            await first.snapshot(resource_id)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_reservation_lease_cannot_disappear_while_loading(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a"))
    ).admitted

    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hdel(first.keys(resource_id).leases("backend-a"), "lease-a")
        with pytest.raises(GPUArbiterStoreError, match="ledger decode"):
            await first.snapshot(resource_id)
        result = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-b", owner_id="owner-b"),
        )
        assert result.status == "ledger_corrupt"
        assert result.reason == "lease_count_cache_drift"
        safety_latch = await first.mark_card_not_ready(
            resource_id,
            100,
            reason="ledger_corrupt",
        )
        assert safety_latch.status == "not_ready"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_idempotent_admission_rejects_a_corrupt_sibling_lease(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-b", owner_id="owner-b"),
        )
    ).admitted

    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(keys.leases("backend-a"), "lease-b", "{broken")
        before_children = (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
        )
        result = await first.admit(
            resource_id,
            **_admission_kwargs("backend-a", "lease-a"),
        )
        assert result.status == "ledger_corrupt"
        assert result.reason == "lease_invalid"
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
        assert (
            await raw.hgetall(keys.allocations),
            await raw.hgetall(keys.leases("backend-a")),
        ) == before_children
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_snapshot_rejects_oversized_backend_domain_before_scanning(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-oversized-domain/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    keys = first.keys(resource_id)
    domain_raw = json.dumps(
        [f"backend-{index:02d}" for index in range(65)],
        separators=(",", ":"),
    )
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(
            keys.card,
            mapping={
                "backend_domain": domain_raw,
                "backend_domain_fingerprint": hashlib.sha256(
                    domain_raw.encode("utf-8")
                ).hexdigest(),
            },
        )
        with pytest.raises(GPUArbiterStoreError, match="ledger decode"):
            await first.snapshot(resource_id)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_snapshot_retries_when_incarnation_changes_at_same_revision(
    redis_stores,
    monkeypatch,
) -> None:
    first, second = redis_stores
    resource_id = "node-snapshot-incarnation-aba/index:0"
    old_allocation = _reconcile_allocation()
    await first.reconcile_card(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
        allocations=(old_allocation,),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="snapshot-old-bootstrap",
    )
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    original_hgetall = first._redis.hgetall
    interleaved = False

    async def hgetall_with_rebootstrap(name, *args, **kwargs):
        nonlocal interleaved
        result = await original_hgetall(name, *args, **kwargs)
        if name == keys.allocations and not interleaved:
            interleaved = True
            await raw.delete(
                keys.card,
                keys.allocations,
                keys.queue,
                keys.transition,
                *(
                    key
                    for backend_id in _TEST_BACKEND_DOMAIN
                    for key in (
                        keys.leases(backend_id),
                        keys.backend_queue(backend_id),
                    )
                ),
            )
            await second.reconcile_card(
                resource_id,
                100,
                expected_ledger_revision=None,
                expected_ledger_incarnation=None,
                backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
                allocations=(
                    replace(
                        old_allocation,
                        state=GPUAllocationState.UNLOADED,
                    ),
                ),
                lease_cleanup=None,
                ready=True,
                reconcile_deadline_ms=_future_reconcile_deadline_ms(),
                repair_id="snapshot-new-bootstrap",
            )
        return result

    try:
        monkeypatch.setattr(first._redis, "hgetall", hgetall_with_rebootstrap)
        snapshot = await first.snapshot(resource_id)
        assert interleaved is True
        assert snapshot.ledger_revision == 1
        assert snapshot.committed_mb == 0
        assert snapshot.allocations[0].state is GPUAllocationState.UNLOADED
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_snapshot_retries_across_concurrent_ledger_revisions(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    await _admit_resident(first, resource_id)
    assert (
        await first.release_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"

    finished = asyncio.Event()

    async def rewrite_ledger() -> None:
        generation = 1
        try:
            for cycle in range(4):
                drain_generation = str(generation + 1)
                transition_owner = f"transition-{cycle}"
                assert (
                    await second.acquire_transition_owner(
                        resource_id,
                        backend_id="backend-a",
                        membership_epoch=1,
                        owner_id=transition_owner,
                        generation=drain_generation,
                        operation="evict",
                        ttl_ms=30_000,
                    )
                ).status == "acquired"
                assert (
                    await second.transition_allocation(
                        resource_id,
                        backend_id="backend-a",
                        expected_generation=str(generation),
                        target_state=GPUAllocationState.DRAINING,
                        next_generation=drain_generation,
                        transition_owner_id=transition_owner,
                        transition_operation="evict",
                    )
                ).status == "transitioned"
                for state in (
                    GPUAllocationState.UNLOADING,
                    GPUAllocationState.UNLOADED,
                ):
                    assert (
                        await second.transition_allocation(
                            resource_id,
                            backend_id="backend-a",
                            expected_generation=drain_generation,
                            target_state=state,
                            transition_owner_id=transition_owner,
                            transition_operation="evict",
                        )
                    ).status == "transitioned"
                assert (
                    await second.release_transition_owner(
                        resource_id,
                        backend_id="backend-a",
                        owner_id=transition_owner,
                        generation=drain_generation,
                        operation="evict",
                    )
                ).status == "released"

                generation += 2
                lease_id = f"lease-{cycle}"
                owner_id = f"owner-{cycle}"
                await _admit_resident(
                    second,
                    resource_id,
                    lease_id=lease_id,
                    owner_id=owner_id,
                    generation=str(generation),
                )
                assert (
                    await second.release_lease(
                        resource_id,
                        backend_id="backend-a",
                        lease_id=lease_id,
                        owner_id=owner_id,
                        generation=str(generation),
                    )
                ).status == "released"
                await asyncio.sleep(0.001)
        finally:
            finished.set()

    writer = asyncio.create_task(rewrite_ledger())
    snapshots = 0
    while not finished.is_set():
        snapshot = await first.snapshot(resource_id)
        assert snapshot.committed_mb in {0, 60}
        snapshots += 1
        await asyncio.sleep(0)
    await writer
    assert snapshots > 0
    assert (await first.snapshot(resource_id)).committed_mb == 60


@pytest.mark.asyncio
async def test_allocation_and_lease_keys_never_expire(redis_stores) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.admit(resource_id, **_admission_kwargs("backend-a", "lease-a"))
    ).admitted
    assert await first.key_ttls(resource_id, backend_id="backend-a") == (-1, -1)


@pytest.mark.asyncio
async def test_revision_headroom_rejects_new_queue_work_without_mutation(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-revision-headroom/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        new_work_limit = _LEDGER_REVISION_REBASE_THRESHOLD
        await raw.hset(keys.card, "ledger_revision", str(new_work_limit))

        rejected = await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="must-not-queue",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
        assert rejected.status == "not_ready"
        assert int(await raw.hget(keys.card, "ledger_revision")) == (new_work_limit)
        assert await raw.llen(keys.backend_queue("backend-a")) == 0
        assert await raw.hget(keys.card, "bootstrap_state") == "not_ready"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_last_headroom_admission_can_finish_owned_cleanup(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-revision-cleanup-runway/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        await raw.hset(
            keys.card,
            "ledger_revision",
            str(_LEDGER_REVISION_REBASE_THRESHOLD - 1),
        )
        await _admit_resident(first, resource_id)
        heartbeat = await first.heartbeat_lease(
            resource_id,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
            heartbeat_ttl_ms=5_000,
        )
        assert heartbeat.status == "heartbeated"
        assert (
            await first.release_lease(
                resource_id,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
            )
        ).status == "released"
        assert await raw.hlen(keys.leases("backend-a")) == 0
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_cutoff_response_loss_retries_confirm_existing_ownership(
    redis_stores,
) -> None:
    first, _ = redis_stores
    cutoff = _LEDGER_REVISION_REBASE_THRESHOLD
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        admission_resource = "node-cutoff-admission-retry/index:0"
        await _bootstrap_empty_card(first, admission_resource, 100)
        admission_keys = first.keys(admission_resource)
        await raw.hset(admission_keys.card, "ledger_revision", str(cutoff - 1))
        admission_kwargs = _admission_kwargs("backend-a", "cutoff-lease")
        first_admission = await first.admit(admission_resource, **admission_kwargs)
        assert first_admission.status == "admitted"
        retried_admission = await first.admit(admission_resource, **admission_kwargs)
        assert retried_admission.status == "admitted"
        assert retried_admission.idempotent is True
        assert int(await raw.hget(admission_keys.card, "ledger_revision")) == cutoff

        queue_resource = "node-cutoff-queue-retry/index:0"
        await _bootstrap_empty_card(first, queue_resource, 100)
        queue_keys = first.keys(queue_resource)
        await raw.hset(queue_keys.card, "ledger_revision", str(cutoff - 1))
        first_ticket = await first.enqueue_backend(
            queue_resource,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="cutoff-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
        retried_ticket = await first.enqueue_backend(
            queue_resource,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="cutoff-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
        assert first_ticket.status == retried_ticket.status == "queued"
        assert retried_ticket.expires_at_ms == first_ticket.expires_at_ms
        assert int(await raw.hget(queue_keys.card, "ledger_revision")) == cutoff

        owner_resource = "node-cutoff-owner-retry/index:0"
        await _bootstrap_empty_card(first, owner_resource, 100)
        await _admit_resident(first, owner_resource)
        assert (
            await first.release_lease(
                owner_resource,
                backend_id="backend-a",
                lease_id="lease-a",
                owner_id="owner-a",
                generation="1",
            )
        ).status == "released"
        owner_keys = first.keys(owner_resource)
        await raw.hset(owner_keys.card, "ledger_revision", str(cutoff - 1))
        owner_kwargs = {
            "backend_id": "backend-a",
            "membership_epoch": 1,
            "owner_id": "cutoff-owner",
            "generation": "2",
            "operation": "evict",
            "ttl_ms": 30_000,
        }
        first_owner = await first.acquire_transition_owner(
            owner_resource, **owner_kwargs
        )
        retried_owner = await first.acquire_transition_owner(
            owner_resource, **owner_kwargs
        )
        assert first_owner.status == retried_owner.status == "acquired"
        assert retried_owner.idempotent is True
        assert retried_owner.expires_at_ms == first_owner.expires_at_ms
        assert int(await raw.hget(owner_keys.card, "ledger_revision")) == cutoff
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_queue_position_without_pruning_is_revision_read_only(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-queue-position-read-only/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="live-ticket",
            owner_id="owner-a",
            ttl_ms=30_000,
        )
    ).status == "queued"
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        revision = int(await raw.hget(keys.card, "ledger_revision"))
        for _ in range(2):
            assert (
                await first.queue_position(
                    resource_id,
                    backend_id="backend-a",
                    ticket_id="live-ticket",
                    card_queue=False,
                )
            ).position == 1
        assert int(await raw.hget(keys.card, "ledger_revision")) == revision
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_reconcile_rotates_incarnation_and_rebases_revision_headroom(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-revision-rebase/index:0"
    await _bootstrap_empty_card(first, resource_id, 100)
    before = await first.snapshot(resource_id)
    keys = first.keys(resource_id)
    raw = Redis.from_url(_redis_url(), decode_responses=True)
    try:
        rebase_boundary = _LEDGER_REVISION_REBASE_THRESHOLD - 1
        await raw.hset(keys.card, "ledger_revision", str(rebase_boundary))

        rebased = await first.reconcile_card(
            resource_id,
            100,
            expected_ledger_revision=rebase_boundary,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_TEST_BACKEND_MEMBERSHIPS,
            allocations=before.allocations,
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="revision-headroom-rebase",
        )
        assert rebased.status == "reconciled"
        assert rebased.ledger_revision == 1
        assert rebased.ledger_incarnation != before.ledger_incarnation
        snapshot = await first.snapshot(resource_id)
        assert snapshot.ready is True
        assert snapshot.ledger_revision == 1
        assert snapshot.ledger_incarnation == rebased.ledger_incarnation
    finally:
        await raw.aclose()


def test_max_concurrency_requires_a_positive_integer() -> None:
    assert normalize_gpu_backend_max_concurrency(None) == 4
    assert normalize_gpu_backend_max_concurrency(2) == 2
    for invalid in (True, False, 0, -1, "2", 1.5, 10_001):
        with pytest.raises(ValueError):
            normalize_gpu_backend_max_concurrency(invalid)


@pytest.mark.asyncio
async def test_ttl_durations_cannot_write_unsafe_absolute_deadlines(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-a/index:0"
    maximum_ttl_ms = 2_147_483_647
    invalid_ttl_ms = maximum_ttl_ms + 1
    await _bootstrap_empty_card(first, resource_id, 100)

    kwargs = _admission_kwargs("backend-a", "lease-a")
    kwargs["heartbeat_ttl_ms"] = maximum_ttl_ms
    kwargs["hard_ttl_ms"] = maximum_ttl_ms
    assert (await first.admit(resource_id, **kwargs)).admitted
    assert len((await first.snapshot(resource_id)).leases) == 1
    assert (
        await first.enqueue_backend(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="ticket-a",
            owner_id="owner-a",
            ttl_ms=maximum_ttl_ms,
        )
    ).status == "queued"
    assert (
        await first.transition_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            target_state=GPUAllocationState.LOADING,
            request_lease_id="lease-a",
            request_owner_id="owner-a",
        )
    ).status == "transitioned"
    assert (
        await first.finalize_cold_allocation(
            resource_id,
            backend_id="backend-a",
            expected_generation="1",
            request_lease_id="lease-a",
            request_owner_id="owner-a",
            target_state=GPUAllocationState.RESIDENT,
            target_evictable=True,
            resident_cooldown_ms=maximum_ttl_ms,
        )
    ).status == "transitioned"
    assert (
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="transition-owner",
            generation="2",
            operation="evict",
            ttl_ms=maximum_ttl_ms,
        )
    ).status == "acquired"

    invalid_kwargs = _admission_kwargs("backend-b", "lease-invalid")
    invalid_kwargs["heartbeat_ttl_ms"] = invalid_ttl_ms
    invalid_kwargs["hard_ttl_ms"] = invalid_ttl_ms
    with pytest.raises(ValueError, match="at most"):
        await first.admit(resource_id, **invalid_kwargs)
    with pytest.raises(ValueError, match="at most"):
        await first.enqueue_card(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            ticket_id="ticket-invalid",
            owner_id="owner-a",
            ttl_ms=invalid_ttl_ms,
        )
    with pytest.raises(ValueError, match="at most"):
        await first.acquire_transition_owner(
            resource_id,
            backend_id="backend-a",
            membership_epoch=1,
            owner_id="invalid-owner",
            generation="2",
            operation="evict",
            ttl_ms=invalid_ttl_ms,
        )


def test_store_is_recreated_and_closed_in_each_asyncio_run() -> None:
    redis_url = _redis_url()
    namespace = f"gpu-arbiter-test:{uuid.uuid4().hex}"

    async def one_cycle(resource_id: str) -> GPUArbiterStore:
        store = GPUArbiterStore.from_url(redis_url, namespace=namespace)
        async with store:
            assert await store.ping() is True
            await store.configure_card(resource_id, 100, ready=False)
        return store

    async def assert_closed(store: GPUArbiterStore) -> None:
        with pytest.raises(GPUArbiterStoreError, match="store is closed"):
            await store.ping()

    try:
        closed = asyncio.run(one_cycle("node-a/index:0"))
        asyncio.run(assert_closed(closed))
        closed = asyncio.run(one_cycle("node-a/index:1"))
        asyncio.run(assert_closed(closed))
    finally:
        asyncio.run(_clean_namespace(redis_url, namespace))


@pytest.mark.asyncio
async def test_unavailable_redis_fails_within_client_timeout() -> None:
    transports: list[asyncio.BaseTransport] = []

    class BlackholeProtocol(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            transports.append(transport)

    server = await asyncio.get_running_loop().create_server(
        BlackholeProtocol, "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]
    store = GPUArbiterStore.from_url(
        f"redis://127.0.0.1:{port}/0",
        namespace=f"gpu-arbiter-test:{uuid.uuid4().hex}",
    )
    started = time.monotonic()
    try:
        with pytest.raises(GPUArbiterStoreError, match="gpu_arbiter_unavailable"):
            await store.ping()
        assert time.monotonic() - started < 3
    finally:
        await store.aclose()
        server.close()
        await server.wait_closed()
        for transport in transports:
            transport.close()


@pytest.mark.asyncio
async def test_failed_close_is_bounded_and_can_be_retried(monkeypatch) -> None:
    class HangingRedis:
        should_hang = True

        def register_script(self, _source: str) -> object:
            return object()

        async def aclose(self) -> None:
            if self.should_hang:
                await asyncio.Event().wait()

        async def ping(self) -> bool:
            return True

    monkeypatch.setattr(gpu_arbiter_store_module, "_REDIS_CALL_DEADLINE_SECONDS", 0.02)
    redis = HangingRedis()
    store = GPUArbiterStore(redis)
    with pytest.raises(GPUArbiterStoreError, match="gpu_arbiter_unavailable"):
        await store.aclose()

    redis.should_hang = False
    await store.aclose()
    with pytest.raises(GPUArbiterStoreError, match="store is closed"):
        await store.ping()


@pytest.mark.asyncio
async def test_retiring_collection_shrinks_exact_domain_and_receipt_replays(
    redis_stores,
) -> None:
    first, _ = redis_stores
    resource_id = "node-gc/GPU-exact"
    memberships = (
        GPUBackendDomainMember("backend-active", 1, "active"),
        GPUBackendDomainMember("backend-retiring", 2, "retiring"),
    )
    prepared = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=memberships,
        reset_id="gc-bootstrap",
    )
    assert prepared.status == "prepared"
    context = await first.prepared_proof_reset(resource_id)
    assert context is not None
    committed = await first.commit_proof_reset(
        resource_id,
        100,
        reset_id=context.reset_id,
        expected_reset_revision=context.ledger_revision,
        expected_reset_incarnation=context.ledger_incarnation,
        backend_memberships=memberships,
        allocations=(
            _reconcile_allocation(
                backend_id="backend-active",
                generation=None,
                budget_mb=40,
                last_used_at_ms=1,
            ),
            _reconcile_allocation(
                backend_id="backend-retiring",
                generation=None,
                budget_mb=60,
                last_used_at_ms=1,
            ),
        ),
        ready=False,
        evidence_deadline_ms=0,
        proof_fingerprint="1" * 64,
    )
    assert committed.status == "not_ready"
    before = await first.snapshot(resource_id)
    evidence_deadline_ms = int(time.time() * 1000) + 120_000
    retirement_id = str(uuid.uuid4())

    # A target ticket hidden under a sibling queue must block collection even
    # when all queue-count mirrors are internally consistent.
    keys = first.keys(resource_id)
    sibling_queue = keys.backend_queue("backend-active")
    await first._redis.rpush(
        sibling_queue,
        json.dumps(
            {
                "kind": "backend",
                "ticket_id": "hidden-retiring-ticket",
                "owner_id": "owner-hidden",
                "backend_id": "backend-retiring",
                "membership_epoch": "2",
                "enqueued_at_ms": 1,
                "expires_at_ms": evidence_deadline_ms,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    await first._redis.hset(
        keys.card,
        "backend_queue_counts",
        json.dumps(
            {"backend-active": 1, "backend-retiring": 0},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    blocked = await first.collect_retired_backend(
        resource_id,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
        vram_budget_mb=60,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint="2" * 64,
        collection_id="gc-exact",
    )
    assert blocked.status == "ledger_corrupt"
    assert blocked.reason == "backend_queue_invalid"
    assert await first._redis.llen(sibling_queue) == 1
    await first._redis.delete(sibling_queue)
    await first._redis.hset(
        keys.card,
        "backend_queue_counts",
        json.dumps(
            {"backend-active": 0, "backend-retiring": 0},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )

    collection = await first.collect_retired_backend(
        resource_id,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
        vram_budget_mb=60,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint="2" * 64,
        collection_id="gc-exact",
    )

    assert collection.status == "collected"
    assert collection.idempotent is False
    after = await first.snapshot(resource_id)
    assert after.backend_ids == ("backend-active",)
    assert after.active_backend_ids == ("backend-active",)
    assert after.committed_mb == 40
    assert tuple(item.backend_id for item in after.allocations) == ("backend-active",)
    await first._redis.hincrby(keys.card, "ledger_revision", 1)
    evolved_memberships = (
        *memberships,
        GPUBackendDomainMember("backend-pending", 1, "pending"),
    )
    receipt = await first.verify_tombstone_gc_receipt(
        resource_id,
        backend_memberships=evolved_memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
    )
    assert receipt is not None
    assert receipt.ledger_revision == after.ledger_revision + 1

    replay = await first.collect_retired_backend(
        resource_id,
        expected_ledger_revision=before.ledger_revision,
        expected_ledger_incarnation=before.ledger_incarnation,
        backend_memberships=memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
        vram_budget_mb=60,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint="2" * 64,
        collection_id="gc-exact",
    )
    assert replay.status == "collected"
    assert replay.idempotent is True
    assert replay.ledger_revision == after.ledger_revision + 1

    reset = await first.begin_proof_reset(
        resource_id,
        100,
        expected_ledger_revision=replay.ledger_revision,
        expected_ledger_incarnation=replay.ledger_incarnation,
        backend_memberships=memberships,
        reset_id="gc-rebootstrap",
    )
    assert reset.status == "prepared"
    reset_context = await first.prepared_proof_reset(resource_id)
    assert reset_context is not None
    reset_commit = await first.commit_proof_reset(
        resource_id,
        100,
        reset_id=reset_context.reset_id,
        expected_reset_revision=reset_context.ledger_revision,
        expected_reset_incarnation=reset_context.ledger_incarnation,
        backend_memberships=memberships,
        allocations=(
            _reconcile_allocation(
                backend_id="backend-active",
                generation=None,
                budget_mb=40,
                last_used_at_ms=2,
            ),
            _reconcile_allocation(
                backend_id="backend-retiring",
                generation=None,
                budget_mb=60,
                last_used_at_ms=2,
            ),
        ),
        ready=False,
        evidence_deadline_ms=0,
        proof_fingerprint="3" * 64,
    )
    assert reset_commit.status == "not_ready"
    reset_snapshot = await first.snapshot(resource_id)
    assert reset_snapshot.ledger_incarnation != replay.ledger_incarnation

    recollected = await first.collect_retired_backend(
        resource_id,
        expected_ledger_revision=reset_snapshot.ledger_revision,
        expected_ledger_incarnation=reset_snapshot.ledger_incarnation,
        backend_memberships=memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
        vram_budget_mb=60,
        evidence_deadline_ms=evidence_deadline_ms,
        evidence_fingerprint="4" * 64,
        collection_id="gc-after-reset",
    )
    assert recollected.status == "collected"
    assert recollected.idempotent is False
    refreshed_receipt = await first.verify_tombstone_gc_receipt(
        resource_id,
        backend_memberships=memberships,
        backend_id="backend-retiring",
        membership_epoch=2,
        retirement_id=retirement_id,
    )
    assert refreshed_receipt is not None
    assert refreshed_receipt.ledger_incarnation == reset_snapshot.ledger_incarnation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "source", "has_allocation"),
    [
        (GPUAllocationState.UNKNOWN, None, True),
        (GPUAllocationState.UNKNOWN, "7", True),
        (GPUAllocationState.UNLOADED, "7", True),
        (None, "7", False),
    ],
)
async def test_installation_drain_settles_exact_generation_and_keeps_fence(
    redis_stores, state, source, has_allocation
) -> None:
    first, second = redis_stores
    resource = f"installation-{state}-{source}-{has_allocation}"
    allocation = (
        _reconcile_allocation(state=state, generation=source)
        if has_allocation
        else None
    )
    await first.reconcile_card(
        resource,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a"),
        allocations=(allocation,) if allocation else (),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="bootstrap-installation",
    )
    args = dict(
        backend_id="backend-a",
        membership_epoch=1,
        source_generation=source,
        generation="8",
        owner_id="install-owner",
    )
    assert (await first.begin_installation_drain(resource, **args)).status == "owned"
    assert (await second.begin_installation_drain(resource, **args)).idempotent
    assert (
        await second.begin_installation_drain(resource, **{**args, "owner_id": "other"})
    ).status == "busy"
    before = await first.snapshot(resource)
    assert before.transition["operation"] == "installation_drain"
    assert before.committed_mb == (60 if state is GPUAllocationState.UNKNOWN else 0)
    assert (
        await first.release_installation_drain(
            resource,
            **{key: value for key, value in args.items() if key != "source_generation"},
        )
    ).status == "invalid_transition"
    assert (
        await first.begin_proof_reset(
            resource,
            100,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            reset_id="unsafe-reset",
        )
    ).status == "not_ready"
    assert (
        await first.reconcile_card(
            resource,
            100,
            expected_ledger_revision=before.ledger_revision,
            expected_ledger_incarnation=before.ledger_incarnation,
            backend_memberships=_memberships("backend-a"),
            allocations=before.allocations,
            lease_cleanup=None,
            ready=True,
            reconcile_deadline_ms=_future_reconcile_deadline_ms(),
            repair_id="unsafe-repair",
        )
    ).status == "busy"
    terminal = {key: value for key, value in args.items() if key != "source_generation"}
    # The caller supplies independently verified fresh all-pool evidence before finish.
    assert (
        await first.finish_installation_drain(resource, **terminal)
    ).status == "settled"
    assert (await second.finish_installation_drain(resource, **terminal)).idempotent
    settled = await first.snapshot(resource)
    assert settled.transition_present
    assert settled.committed_mb == 0
    if allocation:
        assert settled.allocations[0].state is GPUAllocationState.UNLOADED
        assert settled.allocations[0].generation == "8"
    else:
        assert settled.allocations == ()
    assert (
        await first.release_installation_drain(resource, **terminal)
    ).status == "released"
    assert (await second.release_installation_drain(resource, **terminal)).idempotent
    retry = await first.begin_installation_drain(resource, **args)
    assert retry.status == "released" and retry.idempotent
    assert not (await first.snapshot(resource)).transition_present


@pytest.mark.asyncio
async def test_installation_drain_receipts_survive_other_member_and_active_transition(
    redis_stores,
):
    first, _ = redis_stores
    resource = "installation-receipt-isolation"
    await first.reconcile_card(
        resource,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=_memberships("backend-a", "backend-b"),
        allocations=(),
        lease_cleanup=None,
        ready=True,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="bootstrap-receipt-isolation",
    )
    a = dict(
        backend_id="backend-a",
        membership_epoch=1,
        generation="1",
        owner_id="installation:a",
    )
    b = dict(
        backend_id="backend-b",
        membership_epoch=1,
        generation="1",
        owner_id="installation:b",
    )
    assert (
        await first.begin_installation_drain(resource, source_generation=None, **a)
    ).status == "owned"
    assert (await first.finish_installation_drain(resource, **a)).status == "settled"
    assert (await first.release_installation_drain(resource, **a)).status == "released"
    assert (
        await first.begin_installation_drain(resource, source_generation=None, **b)
    ).status == "owned"
    assert (await first.release_installation_drain(resource, **a)).idempotent
    assert (await first.finish_installation_drain(resource, **b)).status == "settled"
    assert (await first.release_installation_drain(resource, **b)).status == "released"
    assert (await first.release_installation_drain(resource, **a)).idempotent


@pytest.mark.asyncio
async def test_installation_drain_fences_admissions_and_waits_for_uncertain_leases(
    redis_stores,
) -> None:
    first, second = redis_stores
    resource = "installation-busy"
    await _bootstrap_empty_card(first, resource, 100)
    await _admit_resident(first, resource, wait_for_cooldown=False)
    await first.mark_lease_uncertain(
        resource,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    args = dict(
        backend_id="backend-a",
        membership_epoch=1,
        source_generation="1",
        generation="2",
        owner_id="install-owner",
    )
    owned = await first.begin_installation_drain(resource, **args)
    assert owned.status == "active_leases" and owned.lease_count == 1
    assert (
        await second.admit(resource, **_admission_kwargs("backend-a", "new-lease"))
    ).status == "transition_in_progress"
    terminal = {key: value for key, value in args.items() if key != "source_generation"}
    assert (
        await first.finish_installation_drain(resource, **terminal)
    ).status == "active_leases"
    assert (
        await first.release_lease(
            resource,
            backend_id="backend-a",
            lease_id="lease-a",
            owner_id="owner-a",
            generation="1",
        )
    ).status == "released"
    assert (await second.begin_installation_drain(resource, **args)).status == "owned"
    await first.mark_card_not_ready(resource, 100, reason="installation-drain")
    assert (
        await first.finish_installation_drain(resource, **terminal)
    ).status == "settled"
    settled = await first.snapshot(resource)
    assert settled.committed_mb == 0
    assert settled.allocations[0].generation == "2"


@pytest.mark.asyncio
async def test_installation_owner_concludes_only_post_deadline_uncertain_leases(
    redis_stores,
):
    first, second = redis_stores
    resource = "installation-proven-uncertain"
    await _bootstrap_empty_card(first, resource, 150)
    await _admit_resident(first, resource, wait_for_cooldown=False)
    await _admit_resident(
        first,
        resource,
        backend_id="backend-b",
        lease_id="peer",
        wait_for_cooldown=False,
    )
    await first.mark_lease_uncertain(
        resource,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    args = dict(
        backend_id="backend-a",
        membership_epoch=1,
        source_generation="1",
        generation="2",
        owner_id="installation:owner",
    )
    assert (
        await first.begin_installation_drain(resource, **args)
    ).status == "active_leases"
    before = await first.snapshot(resource)
    terminal = {k: v for k, v in args.items() if k != "source_generation"}
    proof = dict(
        ledger_incarnation=before.ledger_incarnation,
        ledger_revision=before.ledger_revision,
        probe_started_at_ms=before.observed_at_ms,
    )
    assert (
        await first.conclude_installation_leases(resource, **terminal, **proof)
    ).status == "active_leases"
    assert (
        await first.finish_installation_drain(resource, **terminal)
    ).status == "active_leases"
    raw = first._redis
    key = first.keys(resource).leases("backend-a")
    lease = json.loads(await raw.hget(key, "lease-a"))
    lease["heartbeat_deadline_ms"] = lease["created_at_ms"]
    lease["hard_deadline_ms"] = lease["created_at_ms"]
    await raw.hset(key, "lease-a", json.dumps(lease))
    await asyncio.sleep(0.01)
    snapshot = await first.snapshot(resource)
    proof = dict(
        ledger_incarnation=snapshot.ledger_incarnation,
        ledger_revision=snapshot.ledger_revision,
        probe_started_at_ms=snapshot.observed_at_ms,
    )
    assert (await second.begin_installation_drain(resource, **args)).idempotent
    assert (
        await first.conclude_installation_leases(
            resource,
            **terminal,
            **{**proof, "ledger_revision": proof["ledger_revision"] - 1},
        )
    ).status == "not_ready"
    assert (
        await first.conclude_installation_leases(
            resource,
            **terminal,
            **{**proof, "ledger_incarnation": "wrong"},
        )
    ).status == "not_ready"
    assert (
        await first.conclude_installation_leases(resource, **terminal, **proof)
    ).status == "owned"
    after = await first.snapshot(resource)
    assert [lease.lease_id for lease in after.leases] == ["peer"]
    assert after.committed_mb == 120
    assert after.transition_present
    assert (
        await first.finish_installation_drain(resource, **terminal)
    ).status == "settled"
    after = await first.snapshot(resource)
    assert after.committed_mb == 60
    assert [lease.lease_id for lease in after.leases] == ["peer"]


@pytest.mark.asyncio
async def test_installation_owner_preserves_cold_reservation(redis_stores):
    first, _ = redis_stores
    resource = "installation-proven-cold"
    await _bootstrap_empty_card(first, resource, 100)
    kwargs = _admission_kwargs("backend-a", "lease-a")
    kwargs.update(heartbeat_ttl_ms=20, hard_ttl_ms=40)
    assert (await first.admit(resource, **kwargs)).admitted
    await first.mark_lease_uncertain(
        resource,
        backend_id="backend-a",
        lease_id="lease-a",
        owner_id="owner-a",
        generation="1",
    )
    args = dict(
        backend_id="backend-a",
        membership_epoch=1,
        source_generation="1",
        generation="2",
        owner_id="installation:owner",
    )
    assert (
        await first.begin_installation_drain(resource, **args)
    ).status == "active_leases"
    await asyncio.sleep(0.06)
    snapshot = await first.snapshot(resource)
    terminal = {k: v for k, v in args.items() if k != "source_generation"}
    result = await first.conclude_installation_leases(
        resource,
        **terminal,
        ledger_incarnation=snapshot.ledger_incarnation,
        ledger_revision=snapshot.ledger_revision,
        probe_started_at_ms=snapshot.observed_at_ms,
    )
    assert result.status == "active_leases"
    after = await first.snapshot(resource)
    assert after.leases == snapshot.leases
    assert after.committed_mb == snapshot.committed_mb == 60
    assert after.transition_present


@pytest.mark.asyncio
async def test_installation_drain_rejects_stale_identity(redis_stores) -> None:
    first, _ = redis_stores
    resource = "installation-stale"
    await _bootstrap_empty_card(first, resource, 100)
    await _admit_resident(first, resource, wait_for_cooldown=False)
    args = dict(
        backend_id="backend-a",
        membership_epoch=1,
        source_generation="1",
        generation="2",
        owner_id="install-owner",
    )
    assert (
        await first.begin_installation_drain(
            resource, **{**args, "membership_epoch": 2}
        )
    ).status == "config_mismatch"
    assert (
        await first.begin_installation_drain(
            resource, **{**args, "source_generation": "9"}
        )
    ).status == "stale_generation"
    assert (
        await first.begin_installation_drain(resource, **{**args, "generation": "1"})
    ).status == "stale_generation"
    assert not (await first.snapshot(resource)).transition_present


@pytest.mark.asyncio
async def test_installation_domain_evolves_missing_authoritative_members(
    redis_stores,
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from aap_inference.gpu.ledger import installation_domain

    first, _ = redis_stores
    resource = "installation-domain-empty"
    await first.mark_card_not_ready(resource, 100, reason="installation-drain")
    domain = _memberships("backend-a", "backend-b")

    class Provider:
        @asynccontextmanager
        async def transaction(self):
            yield object()

    async def lock_domain(_transaction, _resource):
        return SimpleNamespace(memberships=domain)

    monkeypatch.setattr(
        installation_domain, "_lock_gpu_resource_proof_domain", lock_domain
    )
    monkeypatch.setattr(
        installation_domain, "_gpu_domain_members", lambda members: members
    )
    await installation_domain.ensure_installation_domain(
        Provider(), first, resource, "backend-a", 1
    )
    snapshot = await first.snapshot(resource)
    assert snapshot.backend_memberships == domain
    assert not snapshot.ready
    assert (
        await first.begin_installation_drain(
            resource,
            backend_id="backend-a",
            membership_epoch=1,
            source_generation=None,
            generation="1",
            owner_id="install-owner",
        )
    ).status == "owned"


@pytest.mark.asyncio
async def test_installation_domain_refuses_to_drop_or_reidentify_members(
    redis_stores,
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from aap_inference.gpu.ledger import installation_domain
    from aap_inference_contract.gpu import GPUAuthorityUnavailable

    first, _ = redis_stores
    resource = "installation-domain-mismatch"
    await _bootstrap_empty_card(first, resource, 100)
    before = await first.snapshot(resource)
    domain = _memberships("backend-a")

    class Provider:
        @asynccontextmanager
        async def transaction(self):
            yield object()

    async def lock_domain(_transaction, _resource):
        return SimpleNamespace(memberships=domain)

    monkeypatch.setattr(
        installation_domain, "_lock_gpu_resource_proof_domain", lock_domain
    )
    monkeypatch.setattr(
        installation_domain, "_gpu_domain_members", lambda members: members
    )
    with pytest.raises(GPUAuthorityUnavailable):
        await installation_domain.ensure_installation_domain(
            Provider(), first, resource, "backend-a", 1
        )
    domain = _memberships(*_TEST_BACKEND_DOMAIN, epoch=2)
    with pytest.raises(GPUAuthorityUnavailable):
        await installation_domain.ensure_installation_domain(
            Provider(), first, resource, "backend-a", 2
        )
    after = await first.snapshot(resource)
    assert after.backend_memberships == before.backend_memberships
    assert after.ledger_revision == before.ledger_revision


@pytest.mark.asyncio
async def test_terminal_recovery_keeps_native_intent_after_ledger_generation_loss(
    redis_stores,
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aap_protocol_v2.lifecycle import verify_admission_token
    from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
    from aap_inference.gpu.installation_quiescence import quiesce_gpu_installation
    from aap_inference.gpu.ledger import installation_domain
    from aap_inference.gpu.signing import GPUAdmissionTokenSigner
    from aap_inference_contract.gpu import GPUInstallationControlRequest

    first, observer = redis_stores
    request = GPUInstallationControlRequest(
        registry_id=uuid.uuid4(),
        resource_id="terminal-recovery",
        endpoint="http://backend:8000",
        boot_id="boot-1",
        operation_id=uuid.uuid4(),
        container_id="a" * 64,
        pool_ids=["models"],
    )
    backend_id, resource = str(request.registry_id), request.resource_id
    domain = _memberships(backend_id)
    await first.reconcile_card(
        resource,
        100,
        expected_ledger_revision=None,
        expected_ledger_incarnation=None,
        backend_memberships=domain,
        allocations=(_reconcile_allocation(backend_id=backend_id, generation=None),),
        lease_cleanup=None,
        ready=False,
        reconcile_deadline_ms=_future_reconcile_deadline_ms(),
        repair_id="reconstructed-unknown",
    )
    preparation = {
        "membership_epoch": 1,
        "generation": "8",
        "source_generation": "7",
        "control_epoch": "3",
        "settled": False,
        "token_expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
    }
    endpoint = SimpleNamespace(
        registry_id=request.registry_id,
        url=request.endpoint,
        gpu_resource_id=resource,
        endpoint_sha256="b" * 64,
    )
    key = Ed25519PrivateKey.generate()
    signer = GPUAdmissionTokenSigner(active_kid="recovery", _private_key=key)

    class Boundary:
        terminal = False
        committed = False

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def step(self, operation, payload):
            if operation == "membership_candidates":
                return {
                    "candidates": [
                        {
                            "backend_id": backend_id,
                            "state": "active",
                            "membership_epoch": 1,
                        }
                    ]
                }
            assert operation == "installation_terminal"
            if payload["phase"] in {"reserve", "authorize"}:
                return {"status": "prepared", "preparation": preparation.copy()}
            # Native verification still requires the originally persisted intent.
            assert payload["ledger_source_generation"] == "7"
            assert payload["preparation"]["source_generation"] == "7"
            assert self.terminal
            self.committed = payload["phase"] == "commit"
            return {"status": "verified"}

        async def open_owned(self, *args, **kwargs):
            return endpoint

        async def open_installation_terminal(
            self, registry_id, *, request_id, request, preparation, endpoint_sha256
        ):
            assert registry_id == endpoint.registry_id
            assert endpoint_sha256 == endpoint.endpoint_sha256
            return SimpleNamespace(
                registry_id=registry_id,
                request_id=request_id,
                purpose="terminal_drain",
                authority=None,
                url=endpoint.url,
                gpu_resource_id=endpoint.gpu_resource_id,
                endpoint_sha256=endpoint_sha256,
            )

        async def health_meta(self, endpoint, *, gpu_health_challenge, **kwargs):
            return True, {
                GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
                "residency": {"generation": "8" if self.terminal else "7"},
            }

        @asynccontextmanager
        async def control_binding(self, *args, **kwargs):
            yield object()

        async def lifecycle_terminal_drain(self, endpoint, grant, **kwargs):
            claims = verify_admission_token(
                grant.admission_token, keyring={"recovery": key.public_key()}
            )
            assert claims.generation == "8"
            pending = await observer.snapshot(resource)
            assert pending.committed_mb == 60
            assert pending.transition["installation_source_generation"] is None
            self.terminal = True
            return {
                "registry_id": backend_id,
                "gpu_resource_id": resource,
                "boot_id": request.boot_id,
                "operation_id": str(request.operation_id),
                "control_epoch": "3",
                "generation": "8",
                "pool_ids": ["models"],
                "unloaded": True,
            }

    async def lock_domain(transaction, resource_id):
        return SimpleNamespace(memberships=domain)

    async def demote(resource_id):
        await observer.mark_card_not_ready(resource_id, 100, reason="terminal-recovery")

    monkeypatch.setattr(
        installation_domain, "_lock_gpu_resource_proof_domain", lock_domain
    )
    monkeypatch.setattr(
        installation_domain, "_gpu_domain_members", lambda members: members
    )
    boundary = Boundary()
    result = await quiesce_gpu_installation(
        request,
        data=boundary,
        store_factory=lambda: first,
        signer_factory=lambda: signer,
        endpoint_source=boundary,
        backend_transport=boundary,
        readiness_demoter=demote,
        clock=time,
    )
    assert result.unloaded and boundary.committed
    final = await observer.snapshot(resource)
    assert final.committed_mb == 0
    assert not final.transition_present
    assert final.allocations[0].state is GPUAllocationState.UNLOADED
    assert final.allocations[0].generation == "8"
