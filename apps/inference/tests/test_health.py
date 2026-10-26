"""Pure contract cases for the borrowed backend-health coordinator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aap_inference.health import (
    GPU_HEALTH_CHALLENGE_ECHO_MARKER,
    BackendHealthCoordinator,
    reconcile_pool_capability,
)
from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
)
from aap_inference_contract.capabilities import CapabilityLimits

_ENDPOINT_SHA256 = "a" * 64


class _Data:
    def __init__(
        self,
        *,
        backend=None,
        membership=None,
        clocks=(),
        current_backend=None,
        member=None,
        pool=None,
    ):
        self.trace = []
        self.backend = backend
        self.membership = membership
        self.clocks = list(clocks)
        self.current_backend = current_backend or backend
        self.member = member
        self.pool = pool
        self.values = None
        self.loaded_pool_registry_ids = []
        self.staged = []

    async def get_backend(self, registry_id):
        self.trace.append("backend")
        return self.backend

    async def read_membership(self, registry_id, gpu_resource_id, *, for_update=False):
        self.trace.append("membership_lock" if for_update else "membership")
        return self.membership

    async def clock_timestamp(self):
        self.trace.append("clock")
        return self.clocks.pop(0)

    async def lock_current_backend(self, registry_id):
        self.trace.append("backend_lock")
        return self.current_backend

    async def lock_gpu_resource(self, resource_id):
        self.trace.append("resource_lock")

    async def try_membership_promotion(self):
        self.trace.append("promotion_try")
        return True

    async def write_health(self, registry_id, values):
        self.trace.append("write")
        self.values = values
        return 1

    async def refresh_backend(self, backend):
        self.trace.append("refresh")

    async def read_member_pool(self, registry_id):
        self.trace.append("member_read")
        return self.member.pool_id if self.member is not None else None

    async def load_pool_member(self, registry_id):
        self.trace.append("member_lock")
        return self.member

    async def load_pool(self, pool_id, *, registry_id):
        self.trace.append("pool_lock")
        self.loaded_pool_registry_ids.append(registry_id)
        return self.pool

    async def stage_pool_reconciliation(
        self,
        registry_id,
        pool,
        member,
        *,
        operation,
        snapshot,
        fingerprint,
        expected_generation,
        expected_pool_fingerprint,
        expected_member_state,
    ):
        self.staged.append(
            {
                "registry_id": registry_id,
                "pool_id": pool.id,
                "member_id": member.id,
                "operation": operation,
                "snapshot": snapshot,
                "fingerprint": fingerprint,
                "expected_generation": expected_generation,
                "expected_pool_fingerprint": expected_pool_fingerprint,
                "expected_member_state": expected_member_state,
            }
        )

    async def flush(self):
        self.trace.append("flush")


class _Client:
    def __init__(self, data, *, meta=None, setup=None):
        self.data = data
        self.meta = meta
        self.setup_response = setup

    async def health_meta(self, *, gpu_health_challenge=None):
        self.data.trace.append("health")
        return True, self.meta

    async def setup(self):
        self.data.trace.append("setup")
        return self.setup_response


def _coordinator(data, *, client_factory=None, limits_factory=None):
    return BackendHealthCoordinator(
        data,
        client_factory=client_factory or (lambda _backend: _Client(data)),
        capability_limits_factory=limits_factory or CapabilityLimits,
    )


@pytest.mark.asyncio
async def test_invalid_challenge_is_rejected_before_reading_data():
    data = _Data(backend=SimpleNamespace())

    with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
        await _coordinator(data).check_health(uuid.uuid4(), gpu_health_challenge="bad")

    assert data.trace == []


@pytest.mark.asyncio
async def test_gpu_probe_requires_membership_and_start_clock_before_client():
    backend = SimpleNamespace(
        id=uuid.uuid4(),
        gpu_resource_id="gpu/0",
        endpoint_sha256=_ENDPOINT_SHA256,
        last_checked_at=None,
    )
    data = _Data(backend=backend, clocks=[None])
    coordinator = _coordinator(data, client_factory=lambda _backend: pytest.fail())

    assert await coordinator.check_health(uuid.uuid4()) is False
    assert data.trace == ["backend", "membership"]

    data.trace.clear()
    data.membership = SimpleNamespace(membership_epoch=3, state="active")
    assert await coordinator.check_health(uuid.uuid4()) is False
    assert data.trace == ["backend", "membership", "clock"]


@pytest.mark.asyncio
async def test_gpu_health_phases_keep_io_before_locks_and_proof_after_observation():
    registry_id = uuid.uuid4()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_at = started_at + timedelta(seconds=2)
    backend = SimpleNamespace(
        id=registry_id,
        gpu_resource_id="gpu/0",
        endpoint_sha256=_ENDPOINT_SHA256,
        last_checked_at=None,
    )
    pool_id = uuid.uuid4()
    member = SimpleNamespace(id=uuid.uuid4(), pool_id=pool_id, traffic_state="active")
    pool = SimpleNamespace(
        id=pool_id,
        capability_snapshot=None,
        capability_fingerprint=None,
        routing_generation=0,
        legacy_instance_id=registry_id,
        enabled=True,
    )
    data = _Data(
        backend=backend,
        membership=SimpleNamespace(membership_epoch=8, state="active"),
        clocks=[started_at, observed_at],
        member=member,
        pool=pool,
    )
    client = _Client(
        data,
        meta={GPU_HEALTH_CHALLENGE_ECHO_MARKER: "a" * 64, "compute": {}},
        setup={"name": "controlled", "supported_prompts": ["bbox"]},
    )

    def make_client(_backend):
        data.trace.append("client_factory")
        return client

    def limits():
        data.trace.append("limits")
        return CapabilityLimits()

    assert await _coordinator(
        data, client_factory=make_client, limits_factory=limits
    ).check_health(registry_id, gpu_health_challenge="a" * 64)

    assert data.trace == [
        "backend",
        "membership",
        "clock",
        "client_factory",
        "health",
        "setup",
        "limits",
        "clock",
        "backend_lock",
        "resource_lock",
        "promotion_try",
        "membership_lock",
        "write",
        "refresh",
        "member_read",
        "pool_lock",
        "member_lock",
        "flush",
    ]
    proof = data.values["health_meta"]["gpu_arbiter_probe"]
    assert proof["probe_started_at"] == "2026-01-01T00:00:00Z"
    assert proof["observed_at"] == "2026-01-01T00:00:02Z"
    assert proof["membership_epoch"] == "8"
    assert data.loaded_pool_registry_ids == [registry_id]
    assert len(data.staged) == 1
    assert data.staged[0]["operation"] == "seed"
    assert data.staged[0]["expected_generation"] == 0
    assert data.staged[0]["expected_pool_fingerprint"] is None
    assert data.staged[0]["expected_member_state"] == "active"
    assert data.staged[0]["snapshot"] == pool.capability_snapshot
    assert data.staged[0]["fingerprint"] == pool.capability_fingerprint


@pytest.mark.asyncio
async def test_drift_stages_native_disable_with_pre_mutation_fences():
    registry_id = uuid.uuid4()
    pool_id = uuid.uuid4()
    member = SimpleNamespace(id=uuid.uuid4(), pool_id=pool_id, traffic_state="active")
    pool = SimpleNamespace(
        id=pool_id,
        capability_snapshot={"model_version": "old"},
        capability_fingerprint="0" * 64,
        routing_generation=5,
        legacy_instance_id=registry_id,
        enabled=True,
    )
    capabilities = {"name": "controlled", "supported_prompts": ["bbox"]}
    canonical_snapshot = canonicalize_capability(capabilities)
    candidate_fingerprint = capability_fingerprint(canonical_snapshot)
    data = _Data(member=member, pool=pool)

    await reconcile_pool_capability(data, registry_id, capabilities)

    assert member.traffic_state == "disabled"
    assert pool.enabled is False
    assert pool.routing_generation == 6
    assert data.trace == ["member_read", "pool_lock", "member_lock"]
    assert data.loaded_pool_registry_ids == [registry_id]
    assert data.staged == [
        {
            "registry_id": registry_id,
            "pool_id": pool_id,
            "member_id": member.id,
            "operation": "disable",
            "snapshot": canonical_snapshot,
            "fingerprint": candidate_fingerprint,
            "expected_generation": 5,
            "expected_pool_fingerprint": "0" * 64,
            "expected_member_state": "active",
        }
    ]
    assert "flush" not in data.trace


@pytest.mark.asyncio
async def test_missing_observation_clock_drops_response_before_any_write():
    backend = SimpleNamespace(
        id=uuid.uuid4(),
        gpu_resource_id=None,
        endpoint_sha256=_ENDPOINT_SHA256,
        last_checked_at=None,
    )
    data = _Data(backend=backend, clocks=[None])
    client = _Client(data, meta={"compute": {}})

    assert (
        await _coordinator(data, client_factory=lambda _backend: client).check_health(
            uuid.uuid4()
        )
        is False
    )
    assert data.trace == ["backend", "health", "setup", "clock"]
    assert data.values is None


@pytest.mark.asyncio
async def test_rotated_endpoint_after_observation_drops_the_stale_write():
    registry_id = uuid.uuid4()
    observed = SimpleNamespace(
        id=registry_id,
        gpu_resource_id=None,
        endpoint_sha256=_ENDPOINT_SHA256,
        last_checked_at=None,
    )
    rotated = SimpleNamespace(
        id=registry_id,
        gpu_resource_id=None,
        endpoint_sha256="b" * 64,
        last_checked_at=None,
    )
    data = _Data(
        backend=observed,
        current_backend=rotated,
        clocks=[datetime(2026, 1, 1, tzinfo=UTC)],
    )
    client = _Client(data, meta={"compute": {}})

    assert (
        await _coordinator(data, client_factory=lambda _backend: client).check_health(
            registry_id
        )
        is False
    )
    assert data.trace == ["backend", "health", "setup", "clock", "backend_lock"]
    assert data.values is None
