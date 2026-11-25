"""SSRF host guard, IP pinning, D1 GPU dispatch/diagnostics, and managed_evaluation checks."""

from __future__ import annotations

import http.server
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from yoao_inference.backend_transport import BackendTransport
from yoao_inference.capabilities import extract_capabilities
from yoao_inference.gpu.backend_dispatch import BackendGPUDispatchContextFactory
from yoao_inference.gpu.contracts import GPUArbiterDispatchError, GPUArbiterErrorCode
from yoao_inference.gpu.diagnostics import (
    GPUBackendDiagnosticFacts,
    build_backend_gpu_config_status,
)
from yoao_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    GPUResourceSettings,
)
from yoao_inference.host_guard import (
    INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV,
    INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST_ENV,
    InferenceHostDenied,
    build_pinned_network_backend,
    resolve_pinned_inference_ips,
)
from yoao_protocol_v2.lifecycle import AdmissionScope


def _start_test_server(handler_cls):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_host_guard_hard_blocks_metadata_multicast_reserved_and_rebinding():
    for forbidden in (
        "http://169.254.169.254",
        "http://[::ffff:169.254.169.254]",
        "http://[fe80::1]",
        "http://0.0.0.0",
        "http://0.0.0.1",
        "http://224.0.0.1",
        "http://240.0.0.1",
    ):
        with pytest.raises(InferenceHostDenied):
            resolve_pinned_inference_ips(
                forbidden,
                private_host_allowlist=("169.254.0.0/16", "0.0.0.0/0"),
                loopback_host_allowlist=("127.0.0.1", "::1", "localhost"),
            )

    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips(
            "http://rebind.example.test",
            private_host_allowlist=("rebind.example.test",),
            resolver=lambda _host: ["93.184.216.34", "169.254.169.254"],
        )


def test_host_guard_supports_ipv6_loopback_and_dual_stack_localhost():
    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips("http://[::1]:8000")

    _, v6_ips = resolve_pinned_inference_ips(
        "http://[::1]:8000",
        loopback_host_allowlist=("::1",),
    )
    assert v6_ips == ["::1"]

    _, dual_ips = resolve_pinned_inference_ips(
        "http://localhost:8000",
        loopback_host_allowlist=("localhost",),
        resolver=lambda _: ["127.0.0.1", "::1"],
    )
    assert dual_ips == ["127.0.0.1", "::1"]

    _, container_dual = resolve_pinned_inference_ips(
        "http://localhost:8000",
        resolver=lambda _: ["127.0.0.1", "::1"],
        enforce_private_and_loopback=False,
    )
    assert container_dual == ["127.0.0.1", "::1"]


def test_host_guard_separates_private_and_loopback_allowlists():
    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips("http://10.0.0.5:8000")
    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips("http://127.0.0.1:8000")
    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips(
            "http://localhost:8000", resolver=lambda _: ["127.0.0.1"]
        )

    with pytest.raises(InferenceHostDenied):
        resolve_pinned_inference_ips(
            "http://127.0.0.1:8000",
            private_host_allowlist=("127.0.0.1", "10.0.0.0/8"),
        )

    _, private_ips = resolve_pinned_inference_ips(
        "http://10.0.0.5:8000",
        private_host_allowlist=("10.0.0.0/8",),
    )
    assert private_ips == ["10.0.0.5"]

    _, loopback_ips = resolve_pinned_inference_ips(
        "http://127.0.0.1:8000",
        loopback_host_allowlist=("127.0.0.1",),
    )
    assert loopback_ips == ["127.0.0.1"]

    _, localhost_ips = resolve_pinned_inference_ips(
        "http://localhost:8000",
        loopback_host_allowlist=("localhost",),
        resolver=lambda _: ["127.0.0.1"],
    )
    assert localhost_ips == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_pinned_network_backend_forbids_unix_sockets():
    backend = build_pinned_network_backend()
    with pytest.raises(InferenceHostDenied):
        await backend.connect_unix_socket("/var/run/docker.sock")


@pytest.mark.asyncio
async def test_direct_backend_transport_enforces_loopback_allowlist_and_blocks_redirects(
    monkeypatch,
):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/health"):
                payload = json.dumps(
                    {"ok": True, "compute": {"configured_device": "cpu"}}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path.startswith("/setup"):
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()

        def log_message(self, *_args):
            pass

    server, url = _start_test_server(Handler)
    try:
        direct_backend = SimpleNamespace(
            id=uuid4(),
            url=url,
            auth_method="token",
            auth_token="secret-" + "x" * 40,
            gpu_resource_id=None,
            vram_budget_mb=None,
            extra_params={
                "plugin": {
                    "execution_mode": "direct",
                    "manifest": {"execution_mode": "direct"},
                }
            },
            health_meta=None,
        )
        options = SimpleNamespace(ml_health_timeout=5.0, ml_predict_timeout=5.0)

        async def _noop_dispatch(_op, _scope):
            yield None

        from contextlib import asynccontextmanager

        transport = BackendTransport(
            direct_backend,
            options=options,
            dispatch_context=asynccontextmanager(_noop_dispatch),
            observe=lambda *_: None,
        )

        monkeypatch.delenv(INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV, raising=False)
        monkeypatch.delenv(INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST_ENV, raising=False)
        with pytest.raises(InferenceHostDenied):
            await transport.health_meta()

        monkeypatch.setenv(INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV, "127.0.0.1")
        healthy, meta = await transport.health_meta()
        assert healthy is True
        assert meta == {"compute": {"configured_device": "cpu"}}

        with pytest.raises(httpx.HTTPStatusError) as redirect_err:
            await transport.setup()
        assert redirect_err.value.response.status_code == 302
    finally:
        server.shutdown()
        server.server_close()


def test_extract_capabilities_reads_strict_boolean_managed_evaluation():
    enabled = extract_capabilities({"managed_evaluation": True})
    assert enabled["managed_evaluation"] is True
    assert not any(w["field"] == "managed_evaluation" for w in enabled["warnings"])

    defaulted = extract_capabilities({"name": "plain"})
    assert defaulted["managed_evaluation"] is False

    malformed = extract_capabilities({"managed_evaluation": "true"})
    assert malformed["managed_evaluation"] is False
    assert any(w["field"] == "managed_evaluation" for w in malformed["warnings"])


def _gpu_settings(local_uuid: str) -> GPUAuthoritySettings:
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.ENFORCE,
        gpu_arbiter_rollout_enabled=True,
        gpu_arbiter_resources={
            "node-a/gpu-0": GPUResourceSettings(
                resource_id="node-a/gpu-0",
                node_id="node-a",
                physical_device_token=local_uuid,
                allocatable_mb=16000,
                mode=GPUArbiterMode.ENFORCE,
            )
        },
    )


def test_gpu_diagnostics_distinguishes_remote_d1_gpu_from_local_collision():
    local_uuid = "GPU-11111111-1111-1111-1111-111111111111"
    remote_uuid = "GPU-22222222-2222-2222-2222-222222222222"
    cfg = _gpu_settings(local_uuid)
    now = datetime.now(UTC)

    remote_fact = GPUBackendDiagnosticFacts(
        backend_id=uuid4(),
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        last_checked_at=now,
        health_meta={
            "compute": {"configured_device": "cuda:0", "effective_device": "cuda:0"},
            "gpu_info": {"device_uuid": remote_uuid},
        },
        direct_unarbitrated=True,
    )
    remote_status = build_backend_gpu_config_status(remote_fact, {}, config=cfg)
    assert remote_status.status == "info"
    assert [d.code for d in remote_status.diagnostics] == [
        "direct_unarbitrated_gpu_backend"
    ]

    colliding_fact = GPUBackendDiagnosticFacts(
        backend_id=uuid4(),
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        last_checked_at=now,
        health_meta={
            "compute": {"configured_device": "cuda:0", "effective_device": "cuda:0"},
            "gpu_info": {"device_uuid": local_uuid},
        },
        direct_unarbitrated=True,
    )
    colliding_status = build_backend_gpu_config_status(colliding_fact, {}, config=cfg)
    assert colliding_status.status == "blocker"
    assert colliding_status.diagnostics[0].code == "direct_gpu_local_collision"


@pytest.mark.asyncio
async def test_backend_gpu_dispatch_allows_non_colliding_d1_gpu_and_blocks_local_collision():
    local_uuid = "GPU-11111111-1111-1111-1111-111111111111"
    remote_uuid = "GPU-22222222-2222-2222-2222-222222222222"
    cfg = _gpu_settings(local_uuid)
    now_iso = datetime.now(UTC).isoformat()

    def make_provider(registry_id, observed_uuid):
        class _Data:
            async def step(self, operation, arguments):
                if operation == "shadow_snapshot":
                    return {
                        "backends": [
                            {
                                "backend_id": str(registry_id),
                                "gpu_resource_id": None,
                                "vram_budget_mb": None,
                                "eviction_priority": 0,
                                "extra_params": {
                                    "plugin": {
                                        "execution_mode": "direct",
                                        "manifest": {"execution_mode": "direct"},
                                        "resource": {
                                            "gpu_resource_id": None,
                                            "physical_gpu_uuid": None,
                                            "logical_device": "cuda:0",
                                        },
                                    }
                                },
                                "state": "connected",
                                "last_checked_at": now_iso,
                                "health_meta": {
                                    "compute": {
                                        "configured_device": "cuda:0",
                                        "effective_device": "cuda:0",
                                    },
                                    "gpu_info": {"device_uuid": observed_uuid},
                                },
                            }
                        ]
                    }
                if operation == "list_rollouts":
                    return {
                        "rollouts": [
                            {
                                "resource_id": "node-a/gpu-0",
                                "state": "enforce",
                                "effective_mode": "enforce",
                                "target_mode": "enforce",
                                "revision": 1,
                                "blocker_reason": None,
                            }
                        ]
                    }
                raise AssertionError(f"unexpected step {operation}")

        class _Provider:
            def transaction(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield _Data()

                return _ctx()

        return _Provider()

    for observed_uuid, should_pass in ((remote_uuid, True), (local_uuid, False)):
        registry_id = uuid4()
        request_id = uuid4()
        endpoint = SimpleNamespace(
            request_id=request_id,
            registry_id=registry_id,
            gpu_resource_id=None,
            vram_budget_mb=None,
            eviction_priority=0,
            extra_params={
                "plugin": {
                    "execution_mode": "direct",
                    "manifest": {"execution_mode": "direct"},
                    "resource": {
                        "gpu_resource_id": None,
                        "physical_gpu_uuid": None,
                        "logical_device": "cuda:0",
                    },
                }
            },
        )
        request = SimpleNamespace(
            request_id=request_id,
            registry_id=registry_id,
            operation="warmup",
        )
        factory = BackendGPUDispatchContextFactory(
            make_provider(registry_id, observed_uuid),
            config=cfg,
            authority_dispatch_factory=None,
        )
        dispatch_ctx = factory(request, endpoint)
        if should_pass:
            async with dispatch_ctx("warmup", AdmissionScope.WARMUP) as grant:
                assert grant is None
        else:
            with pytest.raises(GPUArbiterDispatchError) as exc:
                async with dispatch_ctx("warmup", AdmissionScope.WARMUP):
                    pass
            assert exc.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value


@pytest.mark.asyncio
async def test_pinned_transport_maps_httpcore_errors_and_honors_options_allowlist(
    monkeypatch,
):
    import socket
    from contextlib import asynccontextmanager

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    backend = SimpleNamespace(
        id=uuid4(),
        url=f"http://127.0.0.1:{closed_port}",
        auth_method="token",
        auth_token="secret-" + "x" * 40,
        gpu_resource_id=None,
        vram_budget_mb=None,
        extra_params={
            "plugin": {
                "execution_mode": "direct",
                "manifest": {"execution_mode": "direct"},
            }
        },
        health_meta=None,
    )
    monkeypatch.delenv(INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST_ENV, raising=False)
    monkeypatch.delenv(INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST_ENV, raising=False)
    options = SimpleNamespace(
        ml_health_timeout=2.0,
        ml_predict_timeout=2.0,
        inference_plugin_private_host_allowlist=[],
        inference_plugin_loopback_host_allowlist=["127.0.0.1"],
    )

    async def _noop_dispatch(_op, _scope):
        yield None

    transport = BackendTransport(
        backend,
        options=options,
        dispatch_context=asynccontextmanager(_noop_dispatch),
        observe=lambda *_: None,
    )
    healthy, meta = await transport.health_meta()
    assert healthy is False
    assert meta is None


@pytest.mark.asyncio
async def test_routing_control_and_scope_retirement_block_metadata_and_no_auth_preserves_extra_params():
    import base64

    from yoao_inference.backend_control_manager import _NoAuthConfiguration
    from yoao_inference.backend_endpoint import BackendEndpointSnapshot
    from yoao_inference.routing_backend_control import (
        BackendRoutingControl,
        BackendRoutingControlUnavailable,
    )
    from yoao_inference.scope_retirement_transport import (
        BackendScopeRetirementUnavailable,
        execute_backend_scope_retirement,
    )

    direct_extra = {
        "plugin": {
            "execution_mode": "direct",
            "manifest": {"execution_mode": "direct"},
        }
    }
    snap = BackendEndpointSnapshot(
        registry_id=uuid4(),
        request_id=uuid4(),
        purpose="catalog_fallback",
        authority=None,
        endpoint_sha256="a" * 64,
        url="http://127.0.0.1:9999",
        auth_method="token",
        auth_token="tok",
        gpu_resource_id=None,
        vram_budget_mb=None,
        eviction_priority=0,
        extra_params=direct_extra,
    )
    assert _NoAuthConfiguration(snap).extra_params == direct_extra

    signing_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
    routing_ctx = {
        "control_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "phase": "draining",
        "new_epoch": str(uuid4()),
        "challenge": "a" * 64,
        "retired_epoch": None,
        "backend_incarnation": str(uuid4()),
        "endpoint": {
            "url": "http://169.254.169.254",
            "auth_method": "token",
            "auth_token": "tok",
        },
        "signing_key": signing_key,
    }
    with pytest.raises(BackendRoutingControlUnavailable):
        await BackendRoutingControl(timeout_seconds=2.0)(routing_ctx)
    with pytest.raises(BackendRoutingControlUnavailable):
        await BackendRoutingControl(timeout_seconds=2.0).status(routing_ctx)

    retire_ctx = {
        "retirement_id": str(uuid4()),
        "scope_id": str(uuid4()),
        "registry_id": str(uuid4()),
        "authority_epoch": str(uuid4()),
        "backend_incarnation": str(uuid4()),
        "challenge": "b" * 64,
        "fence_until": 1_800_000_000,
        "endpoint": {
            "url": "http://169.254.169.254",
            "auth_method": "token",
            "auth_token": "tok",
        },
    }
    with pytest.raises(BackendScopeRetirementUnavailable):
        await execute_backend_scope_retirement(retire_ctx)


def test_gpu_collision_normalizes_uuid_casing_and_fails_closed_on_index_resources():
    local_uuid = "GPU-11111111-1111-1111-1111-111111111111"
    cfg_uuid = _gpu_settings(local_uuid)
    now = datetime.now(UTC)

    lowercase_fact = GPUBackendDiagnosticFacts(
        backend_id=uuid4(),
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        last_checked_at=now,
        health_meta={
            "compute": {"configured_device": "cuda:0", "effective_device": "cuda:0"},
            "gpu_info": {"device_uuid": f"  {local_uuid.lower()}  "},
        },
        direct_unarbitrated=True,
    )
    lowercase_status = build_backend_gpu_config_status(
        lowercase_fact, {}, config=cfg_uuid
    )
    assert lowercase_status.status == "blocker"
    assert lowercase_status.diagnostics[0].code == "direct_gpu_local_collision"

    cfg_index = _gpu_settings("index:0")
    cuda_on_index_host = GPUBackendDiagnosticFacts(
        backend_id=uuid4(),
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        last_checked_at=now,
        health_meta={
            "compute": {"configured_device": "cuda:0", "effective_device": "cuda:0"},
            "gpu_info": {"device_uuid": "GPU-22222222-2222-2222-2222-222222222222"},
        },
        direct_unarbitrated=True,
    )
    index_status = build_backend_gpu_config_status(
        cuda_on_index_host, {}, config=cfg_index
    )
    assert index_status.status == "blocker"
    assert index_status.diagnostics[0].code == "direct_gpu_local_collision"

    cpu_on_index_host = GPUBackendDiagnosticFacts(
        backend_id=uuid4(),
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        last_checked_at=now,
        health_meta={
            "compute": {"configured_device": "cpu", "effective_device": None},
            "gpu_info": None,
        },
        direct_unarbitrated=True,
    )
    cpu_status = build_backend_gpu_config_status(
        cpu_on_index_host, {}, config=cfg_index
    )
    assert cpu_status.status == "info"
    assert cpu_status.diagnostics[0].code == "explicit_cpu_backend"


@pytest.mark.asyncio
async def test_backend_gpu_dispatch_allows_cpu_direct_and_blocks_cuda_direct_on_index_host():
    cfg_index = _gpu_settings("index:0")
    now_iso = datetime.now(UTC).isoformat()

    def make_provider(registry_id, *, device: str):
        health_meta = (
            {
                "compute": {"configured_device": "cpu", "effective_device": None},
                "gpu_info": None,
            }
            if device == "cpu"
            else {
                "compute": {
                    "configured_device": "cuda:0",
                    "effective_device": "cuda:0",
                },
                "gpu_info": {"device_uuid": "GPU-22222222-2222-2222-2222-222222222222"},
            }
        )

        class _Data:
            async def step(self, operation, arguments):
                if operation == "shadow_snapshot":
                    return {
                        "backends": [
                            {
                                "backend_id": str(registry_id),
                                "gpu_resource_id": None,
                                "vram_budget_mb": None,
                                "eviction_priority": 0,
                                "extra_params": {
                                    "plugin": {
                                        "execution_mode": "direct",
                                        "manifest": {"execution_mode": "direct"},
                                        "resource": {
                                            "gpu_resource_id": None,
                                            "physical_gpu_uuid": None,
                                            "logical_device": (
                                                "cpu" if device == "cpu" else "cuda:0"
                                            ),
                                        },
                                    }
                                },
                                "state": "connected",
                                "last_checked_at": now_iso,
                                "health_meta": health_meta,
                            }
                        ]
                    }
                if operation == "list_rollouts":
                    return {
                        "rollouts": [
                            {
                                "resource_id": "node-a/gpu-0",
                                "state": "enforce",
                                "effective_mode": "enforce",
                                "target_mode": "enforce",
                                "revision": 1,
                                "blocker_reason": None,
                            }
                        ]
                    }
                raise AssertionError(f"unexpected step {operation}")

        class _Provider:
            def transaction(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield _Data()

                return _ctx()

        return _Provider()

    for device, should_pass in (("cpu", True), ("cuda", False)):
        registry_id = uuid4()
        request_id = uuid4()
        endpoint = SimpleNamespace(
            request_id=request_id,
            registry_id=registry_id,
            gpu_resource_id=None,
            vram_budget_mb=None,
            eviction_priority=0,
            extra_params={
                "plugin": {
                    "execution_mode": "direct",
                    "manifest": {"execution_mode": "direct"},
                    "resource": {
                        "gpu_resource_id": None,
                        "physical_gpu_uuid": None,
                        "logical_device": "cpu" if device == "cpu" else "cuda:0",
                    },
                }
            },
        )
        request = SimpleNamespace(
            request_id=request_id,
            registry_id=registry_id,
            operation="warmup",
        )
        factory = BackendGPUDispatchContextFactory(
            make_provider(registry_id, device=device),
            config=cfg_index,
            authority_dispatch_factory=None,
        )
        dispatch_ctx = factory(request, endpoint)
        if should_pass:
            async with dispatch_ctx("warmup", AdmissionScope.WARMUP) as grant:
                assert grant is None
        else:
            with pytest.raises(GPUArbiterDispatchError) as exc:
                async with dispatch_ctx("warmup", AdmissionScope.WARMUP):
                    pass
            assert exc.value.error_code == GPUArbiterErrorCode.CONFIG_INVALID.value


@pytest.mark.asyncio
async def test_health_observation_manager_prefixes_inference_host_denied():
    from yoao_inference.health_observation_manager import (
        BackendHealthObservationManager,
    )
    from yoao_inference_contract.health_observation import (
        make_health_observation_request,
    )

    registry_id = uuid4()
    request_id = uuid4()
    sealed: list[dict] = []

    class _Data:
        async def open_subject(self, _subject, _registry_id, _request_id):
            backend_json = json.dumps(
                {
                    "id": str(registry_id),
                    "url": "http://127.0.0.1:9999",
                    "auth_method": "token",
                    "auth_token": "tok",
                    "gpu_resource_id": None,
                    "vram_budget_mb": None,
                    "extra_params": {
                        "plugin": {
                            "execution_mode": "direct",
                            "manifest": {"execution_mode": "direct"},
                        }
                    },
                }
            )
            return json.dumps(
                {
                    "backend_json": backend_json,
                    "gpu_health_challenge": "a" * 64,
                    "timeout_seconds": 5.0,
                }
            )

        async def seal_observation(
            self, _subject, _registry_id, _request_id, observation_json
        ):
            sealed.append(json.loads(observation_json))
            return "v1.c2VhbGVk"

        async def readiness(self):
            return True

    class _DeniedClient:
        async def health_meta(self, *, gpu_health_challenge=None):
            raise InferenceHostDenied("loopback denied")

        async def setup(self):
            raise AssertionError("setup should not run")

    manager = BackendHealthObservationManager(
        _Data(), client_factory=lambda _backend: _DeniedClient()
    )
    req = make_health_observation_request(
        registry_id, "v1.c3ViamVjdA==", request_id=request_id
    )
    await manager.execute(req)
    assert sealed == [
        {
            "kind": "legacy_health_error",
            "error_type": "RuntimeError",
            "error_message": "inference_host_denied: loopback denied",
        }
    ]
