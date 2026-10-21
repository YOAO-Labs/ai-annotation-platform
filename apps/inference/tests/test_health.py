"""Pure contract cases for the borrowed backend-health coordinator."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aap_inference.health import (
    GPU_HEALTH_CHALLENGE_ECHO_MARKER,
    BackendHealthCoordinator,
)
from aap_inference_contract.capabilities import CapabilityLimits


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

    async def load_pool_member(self, registry_id):
        self.trace.append("member_lock")
        return self.member

    async def load_pool(self, pool_id):
        self.trace.append("pool_lock")
        return self.pool

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
        url="http://backend",
        auth_method=None,
        auth_token=None,
        gpu_resource_id="gpu/0",
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
        url="http://backend",
        auth_method="token",
        auth_token="synthetic",
        gpu_resource_id="gpu/0",
        last_checked_at=None,
    )
    member = SimpleNamespace(pool_id=uuid.uuid4(), traffic_state="active")
    pool = SimpleNamespace(
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
        "member_lock",
        "pool_lock",
        "flush",
    ]
    proof = data.values["health_meta"]["gpu_arbiter_probe"]
    assert proof["probe_started_at"] == "2026-01-01T00:00:00Z"
    assert proof["observed_at"] == "2026-01-01T00:00:02Z"
    assert proof["membership_epoch"] == "8"


@pytest.mark.asyncio
async def test_missing_observation_clock_drops_response_before_any_write():
    backend = SimpleNamespace(
        url="http://backend",
        auth_method=None,
        auth_token=None,
        gpu_resource_id=None,
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
