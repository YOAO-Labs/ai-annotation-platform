from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from aap_inference.backend_endpoint import BackendEndpointSnapshot
from aap_inference.gpu.endpoint_source import (
    GPUOwnedBackendEndpointSource,
    GPUOwnedEndpointUnavailable,
)
from aap_inference.owner_transport import (
    BackendTransportGPUControlAdapter,
    _ActiveGPUControl,
)
from aap_inference_contract.backend_endpoint import BackendEndpointBinding
from aap_inference_contract.routing import RoutingUnavailable


class Connection:
    def __init__(self, response):
        self.response = response
        self.calls = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return json.dumps(self.response)


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def snapshot_document(registry_id, request_id):
    return {
        "schema": "aap-inference-backend-endpoint-snapshot/v1",
        "registry_id": str(registry_id),
        "request_id": str(request_id),
        "purpose": "terminal_drain",
        "authority": None,
        "endpoint_sha256": "a" * 64,
        "url": "http://backend/base",
        "auth_method": "none",
        "auth_token": None,
        "auth_revision": 1,
        "gpu_resource_id": "gpu-1",
        "vram_budget_mb": 100,
        "eviction_priority": 0,
        "extra_params": None,
    }


@pytest.mark.asyncio
async def test_terminal_opener_binds_exact_native_inputs_and_snapshot(monkeypatch):
    registry_id, request_id = uuid4(), uuid4()
    response = snapshot_document(registry_id, request_id)
    connection = Connection(response)
    pool = Pool(connection)

    async def verify_profile(connection, purpose):
        assert purpose == "gpu_control"

    monkeypatch.setattr(
        "aap_inference.gpu.endpoint_source._verify_endpoint_profile", verify_profile
    )
    monkeypatch.setattr(
        "aap_inference.gpu.endpoint_source.backend_endpoint_policy_ready",
        lambda policy: True,
    )
    source = GPUOwnedBackendEndpointSource(
        {"health_observation": pool, "gpu_control": pool}
    )
    request = {"registry_id": str(registry_id), "operation_id": str(uuid4())}
    preparation = {"generation": "9", "membership_epoch": 3, "settled": False}
    result = await source.open_installation_terminal(
        registry_id,
        request_id=request_id,
        request=request,
        preparation=preparation,
        endpoint_sha256="a" * 64,
    )
    sql, arguments = connection.calls[-1]
    assert "open_installation_terminal_endpoint" in sql
    assert arguments[:2] == (registry_id, request_id)
    assert json.loads(arguments[2]) == request
    assert json.loads(arguments[3]) == preparation
    assert arguments[4] == result.endpoint_sha256 == "a" * 64

    for field, bad in (
        ("registry_id", str(uuid4())),
        ("request_id", str(uuid4())),
        ("purpose", "gpu_control"),
        ("authority", {}),
        ("endpoint_sha256", "b" * 64),
    ):
        connection.response = {**response, field: bad}
        with pytest.raises(GPUOwnedEndpointUnavailable):
            await source.open_installation_terminal(
                registry_id,
                request_id=request_id,
                request=request,
                preparation=preparation,
                endpoint_sha256="a" * 64,
            )


@pytest.mark.asyncio
async def test_terminal_capability_only_sends_exact_signed_target(monkeypatch):
    registry_id, request_id = uuid4(), uuid4()
    endpoint = BackendEndpointSnapshot(
        registry_id=registry_id,
        request_id=request_id,
        purpose="terminal_drain",
        authority=None,
        endpoint_sha256="a" * 64,
        url="http://backend/base",
        auth_method="none",
        auth_token=None,
        gpu_resource_id="gpu-1",
        vram_budget_mb=100,
        eviction_priority=0,
        extra_params=None,
    )
    seen = []

    class Transport:
        def __init__(self, configuration, **kwargs):
            self.context = kwargs["request_context"]

        async def lifecycle_terminal_drain(self, grant):
            async with self.context(
                httpx.Request(
                    "POST", "http://backend/base/lifecycle/terminal-drain-signed"
                )
            ) as request_grant:
                assert type(request_grant).__name__ == "_TerminalDrainGrant"
                seen.append(grant)
            for method, url in (
                ("GET", "http://backend/base/lifecycle/terminal-drain-signed"),
                ("POST", "http://backend/base/drain"),
                ("POST", "http://backend/base/lifecycle/terminal-drain-signed?x=1"),
                ("POST", "http://other/base/lifecycle/terminal-drain-signed"),
            ):
                with pytest.raises(RoutingUnavailable):
                    async with self.context(httpx.Request(method, url)):
                        pass
            return {"ok": True}

    monkeypatch.setattr("aap_inference.owner_transport.BackendTransport", Transport)
    adapter = BackendTransportGPUControlAdapter(object())
    grant = object()
    assert await adapter.lifecycle_terminal_drain(
        endpoint,
        grant,
        registry_id=registry_id,
        request_id=request_id,
        timeout_seconds=1,
    ) == {"ok": True}
    assert seen == [grant]
    for changed, target_registry, target_request in (
        (endpoint, uuid4(), request_id),
        (endpoint, registry_id, uuid4()),
        (replace(endpoint, purpose="gpu_control"), registry_id, request_id),
    ):
        with pytest.raises(RoutingUnavailable):
            await adapter.lifecycle_terminal_drain(
                changed,
                grant,
                registry_id=target_registry,
                request_id=target_request,
                timeout_seconds=1,
            )
    for operation in ("mode", "reset", "drain", "cancel_drain", "unload"):
        binding = BackendEndpointBinding(
            kind="scope",
            scope_id=uuid4(),
            control_id=None,
            attempt_id=request_id,
            capability="a" * 64,
        )
        token = adapter._active.set(
            _ActiveGPUControl(registry_id, request_id, operation, binding)
        )
        try:
            with pytest.raises(RoutingUnavailable):
                adapter._bound_transport(
                    replace(endpoint, authority=binding),
                    request_id=request_id,
                    operation=operation,
                    timeout_seconds=1,
                )
        finally:
            adapter._active.reset(token)
    with pytest.raises(RoutingUnavailable):
        await adapter.health_meta(
            endpoint,
            request_id=request_id,
            gpu_health_challenge="x",
            timeout_seconds=1,
        )
    with pytest.raises(RoutingUnavailable):
        async with adapter.control_binding(
            registry_id, request_id=request_id, operation="terminal_drain"
        ):
            pass
