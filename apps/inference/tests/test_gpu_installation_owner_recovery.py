from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_inference.backend_transport import GPU_HEALTH_CHALLENGE_ECHO_MARKER
from aap_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from aap_inference.gpu.installation_quiescence import quiesce_gpu_installation
from aap_inference_contract.gpu import (
    GPUAuthorityUnavailable,
    GPUInstallationControlRequest,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class Native:
    def __init__(self, clock, registry_id, *, horizon=1004.0):
        self.clock = clock
        self.registry_id = registry_id
        self.horizon = horizon
        self.phases = []
        self.preparation = {
            "membership_epoch": 1,
            "revision": 3,
            "generation": "2",
            "source_generation": "1",
            "control_epoch": "1",
            "runtime_epoch": "1",
            "token_expires_at": "",
            "settled": False,
        }

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def step(self, action, args):
        if action == "membership_candidates":
            return {
                "candidates": [
                    {
                        "backend_id": str(self.registry_id),
                        "state": "active",
                        "membership_epoch": 1,
                    }
                ]
            }
        phase = args["phase"]
        self.phases.append(phase)
        if phase in {"reserve", "authorize"}:
            self.preparation["token_expires_at"] = args["token_expires_at"]
            if phase == "authorize":
                self.horizon = datetime.fromisoformat(
                    args["token_expires_at"]
                ).timestamp()
            return {"status": "prepared", "preparation": dict(self.preparation)}
        if phase == "prove_idle":
            started = datetime.fromisoformat(args["probe_started_at"]).timestamp()
            if started <= self.horizon or args["residency"]["active_requests"]:
                return {"status": "blocked", "reason": "terminal_idle_unproven"}
        return {"status": "verified"}


class Store:
    def __init__(self, registry_id, clock):
        self.registry_id = registry_id
        self.clock = clock
        self.lease = True
        self.conclusions = []
        self.closed = False

    async def snapshot(self, resource):
        leases = (
            [
                SimpleNamespace(
                    backend_id=str(self.registry_id), hard_deadline_ms=1002000
                )
            ]
            if self.lease
            else []
        )
        return SimpleNamespace(
            transition={"owner_id": self.owner, "installation_source_generation": "1"},
            allocations=(),
            leases=leases,
            ledger_incarnation="inc",
            ledger_revision=5,
        )

    async def begin_installation_drain(self, resource, **kwargs):
        self.owner = kwargs["owner_id"]
        return SimpleNamespace(status="active_leases" if self.lease else "owned")

    async def conclude_installation_leases(self, resource, **kwargs):
        self.conclusions.append(kwargs)
        self.lease = False
        return SimpleNamespace(status="owned")

    async def finish_installation_drain(self, resource, **kwargs):
        assert not self.lease
        return SimpleNamespace(status="settled")

    async def release_installation_drain(self, resource, **kwargs):
        return SimpleNamespace(status="released")

    async def aclose(self):
        self.closed = True


class Transport:
    def __init__(self, request, clock):
        self.request = request
        self.clock = clock
        self.terminal = False

    async def health_meta(
        self, endpoint, *, request_id, gpu_health_challenge, timeout_seconds
    ):
        residency = {
            "active_requests": int(self.clock.time() <= 1004),
            "generation": "1",
        }
        return True, {
            GPU_HEALTH_CHALLENGE_ECHO_MARKER: gpu_health_challenge,
            "residency": residency,
        }

    async def lifecycle_terminal_drain(self, endpoint, grant, **kwargs):
        assert endpoint.purpose == "terminal_drain"
        assert endpoint.registry_id == self.request.registry_id
        assert kwargs["registry_id"] == self.request.registry_id
        assert kwargs["request_id"] == endpoint.request_id
        assert grant.generation == "2"
        assert grant.admission_token == "signed"
        self.terminal = True
        return {
            "registry_id": str(self.request.registry_id),
            "gpu_resource_id": self.request.resource_id,
            "boot_id": self.request.boot_id,
            "operation_id": str(self.request.operation_id),
            "control_epoch": "1",
            "generation": "2",
            "pool_ids": self.request.pool_ids,
            "unloaded": True,
        }


async def run_quiescence(monkeypatch, *, horizon, domain_ready=True):
    request = GPUInstallationControlRequest(
        registry_id=uuid4(),
        resource_id="gpu-1",
        endpoint="http://backend",
        boot_id="boot",
        operation_id=uuid4(),
        container_id="a" * 64,
        pool_ids=["pool"],
    )
    native_request = request.model_dump(mode="json", exclude={"protocol"})
    native_request["pool_ids"] = sorted(native_request["pool_ids"])
    clock = Clock()
    native = Native(clock, request.registry_id, horizon=horizon)
    store = Store(request.registry_id, clock)
    store.owner = f"installation:{request.operation_id}"
    transport = Transport(request, clock)
    endpoint = SimpleNamespace(
        registry_id=request.registry_id,
        url=request.endpoint,
        gpu_resource_id=request.resource_id,
        endpoint_sha256="same",
    )

    class Endpoints:
        async def open_owned(self, *args, **kwargs):
            return endpoint

        async def open_installation_terminal(
            self, registry_id, *, request_id, request, preparation, endpoint_sha256
        ):
            assert registry_id == native.registry_id
            assert request == native_request
            assert preparation == native.preparation
            assert native.phases[-1] == "authorize"
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

    async def no_op(*args, **kwargs):
        pass

    async def ensure_domain(*args, **kwargs):
        if not domain_ready:
            raise GPUAuthorityUnavailable()

    monkeypatch.setattr(
        "aap_inference.gpu.installation_quiescence.ensure_installation_domain",
        ensure_domain,
    )
    try:
        result = await quiesce_gpu_installation(
            request,
            data=native,
            store_factory=lambda: store,
            signer_factory=lambda: SimpleNamespace(sign=lambda claims: "signed"),
            endpoint_source=Endpoints(),
            backend_transport=transport,
            readiness_demoter=no_op,
            clock=clock,
        )
    except GPUArbiterDispatchError as exc:
        result = exc
    return result, native, store, transport


@pytest.mark.asyncio
async def test_recovery_waits_for_post_horizon_idle_before_authorizing(monkeypatch):
    result, native, store, transport = await run_quiescence(monkeypatch, horizon=1004)
    assert result.unloaded is True
    assert native.phases.index("prove_idle") < native.phases.index("authorize")
    assert len(store.conclusions) == 1
    assert store.conclusions[0]["probe_started_at_ms"] > 1004000
    assert native.phases[-2:] == ["verify", "commit"]
    assert transport.terminal and store.closed


@pytest.mark.asyncio
async def test_active_leases_timeout_does_not_authorize_terminal_token(monkeypatch):
    result, native, store, transport = await run_quiescence(monkeypatch, horizon=2000)
    assert isinstance(result, GPUArbiterDispatchError)
    assert result.error_code == GPUArbiterErrorCode.DRAIN_TIMEOUT.value
    assert result.headers == {"Retry-After": "1"}
    assert "authorize" not in native.phases
    assert not store.conclusions and not transport.terminal
    assert store.lease and store.closed


@pytest.mark.asyncio
async def test_unavailable_domain_preserves_lease_and_returns_retryable_error(
    monkeypatch,
):
    result, native, store, transport = await run_quiescence(
        monkeypatch,
        horizon=1004,
        domain_ready=False,
    )
    assert isinstance(result, GPUArbiterDispatchError)
    assert result.error_code == GPUArbiterErrorCode.UNAVAILABLE.value
    assert result.headers == {"Retry-After": "1"}
    assert "authorize" not in native.phases
    assert not store.conclusions and not transport.terminal
    assert store.lease and store.closed
