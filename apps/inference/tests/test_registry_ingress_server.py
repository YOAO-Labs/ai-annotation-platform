from __future__ import annotations

import json
from uuid import UUID

import pytest
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncResult,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_result,
    decode_registry_env_sync_status_result,
    make_registry_env_sync_request,
    registry_env_sync_command_sha256,
    registry_env_sync_request_bytes,
    registry_env_sync_status_request,
    registry_env_sync_status_request_bytes,
)

from aap_inference.pipeline_server import PipelineHttpServer

_REQUEST_ID = UUID("b9894ce1-f23c-4624-b8c7-7316c386f275")
_URL = "http://trusted-backend.test"


class _PipelineManager:
    async def execute(self, _body):
        raise AssertionError("pipeline manager should not receive Registry ingress")

    async def query(self, _body):
        raise AssertionError("pipeline manager should not receive Registry ingress")

    async def status(self, _body):
        raise AssertionError("pipeline manager should not receive Registry ingress")


class _PipelineData:
    async def readiness(self):
        return True


class _RegistryManager:
    def __init__(self, *, state="committed", error=None, wrong_binding=False):
        self.state = state
        self.error = error
        self.wrong_binding = wrong_binding
        self.ready_calls = 0
        self.sync_calls = []
        self.status_calls = []

    async def readiness(self):
        self.ready_calls += 1
        return True

    def _result(self, request_id, command_sha256, observed_urls):
        if self.wrong_binding:
            command_sha256 = "b" * 64
        return RegistryEnvSyncResult(
            schema=REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
            request_id=request_id,
            command_sha256=command_sha256,
            state=self.state,
            observed_urls=observed_urls,
        )

    async def sync(self, request):
        self.sync_calls.append(request)
        if self.error is not None:
            raise self.error
        return self._result(
            request.request_id,
            registry_env_sync_command_sha256(request),
            len(request.urls),
        )

    async def status(self, request):
        self.status_calls.append(request)
        if self.error is not None:
            raise self.error
        return self._result(request.request_id, request.command_sha256, None)


def _request():
    return make_registry_env_sync_request(
        [_URL],
        timeout_seconds=4,
        capability_limits=CapabilityLimits(),
        request_id=_REQUEST_ID,
    )


def _server(manager, *, token="registry-token", other_tokens=False):
    kwargs = {}
    if other_tokens:
        kwargs.update(
            manager=_PipelineManager(),
            data=_PipelineData(),
            control_token="pipeline-token",
            backend_probe_manager=object(),
            backend_probe_control_token="probe-token",
            health_observation_manager=object(),
            health_observation_control_token="health-token",
        )
    else:
        kwargs.update(manager=None, data=None, control_token=None)
    return PipelineHttpServer(
        **kwargs,
        registry_ingress_manager=manager,
        registry_ingress_control_token=token,
    )


@pytest.mark.asyncio
async def test_sync_route_has_its_own_token_and_returns_only_committed_results():
    manager = _RegistryManager()
    server = _server(manager, other_tokens=True)
    body = registry_env_sync_request_bytes(_request())

    wrong_pipeline, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        {"authorization": "Bearer pipeline-token"},
        body,
    )
    wrong_probe, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        {"authorization": "Bearer probe-token"},
        body,
    )
    wrong_health, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        {"authorization": "Bearer health-token"},
        body,
    )
    assert (wrong_pipeline, wrong_probe, wrong_health) == (401, 401, 401)
    assert manager.sync_calls == []

    status, encoded = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        {"authorization": "Bearer registry-token"},
        body,
    )
    result = decode_registry_env_sync_result(_request(), encoded)
    assert status == 200
    assert result.state == "committed"
    assert result.observed_urls == 1
    assert len(manager.sync_calls) == 1


@pytest.mark.asyncio
async def test_status_route_returns_unknown_and_is_bound_to_its_status_request():
    manager = _RegistryManager(state="unknown")
    server = _server(manager)
    status_request = registry_env_sync_status_request(_request())

    status, encoded = await server._dispatch(
        "POST",
        "/v1/registry/env-sync-status",
        {"authorization": "Bearer registry-token"},
        registry_env_sync_status_request_bytes(status_request),
    )

    result = decode_registry_env_sync_status_result(status_request, encoded)
    assert status == 200
    assert result.state == "unknown"
    assert result.observed_urls is None
    assert manager.status_calls == [status_request]


@pytest.mark.asyncio
async def test_routes_fail_closed_on_bad_requests_owner_errors_and_misbinding():
    manager = _RegistryManager()
    server = _server(manager)
    auth = {"authorization": "Bearer registry-token"}

    invalid_status, invalid_body = await server._dispatch(
        "POST", "/v1/registry/env-sync", auth, b"{}"
    )
    assert invalid_status == 400
    assert json.loads(invalid_body) == {
        "detail": "Invalid Registry environment sync request"
    }

    manager.error = RegistryEnvSyncUnavailable()
    unavailable_status, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        auth,
        registry_env_sync_request_bytes(_request()),
    )
    assert unavailable_status == 503

    manager.error = None
    manager.wrong_binding = True
    misbound_status, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        auth,
        registry_env_sync_request_bytes(_request()),
    )
    assert misbound_status == 503

    manager.wrong_binding = False
    manager.state = "caller_pending"
    uncommitted_status, _ = await server._dispatch(
        "POST",
        "/v1/registry/env-sync",
        auth,
        registry_env_sync_request_bytes(_request()),
    )
    assert uncommitted_status == 503


@pytest.mark.asyncio
async def test_registry_ingress_contributes_to_readiness_without_changing_pipeline_only():
    manager = _RegistryManager()
    registry_only = _server(manager)
    ready_status, ready_body = await registry_only._dispatch("GET", "/health", {}, b"")
    assert ready_status == 200
    assert json.loads(ready_body) == {"status": "ready"}
    assert manager.ready_calls == 1

    pipeline_only = PipelineHttpServer(
        _PipelineManager(), _PipelineData(), "pipeline-token"
    )
    pipeline_status, pipeline_body = await pipeline_only._dispatch(
        "GET", "/health", {}, b""
    )
    assert pipeline_status == 200
    assert pipeline_body == b'{"status":"ready"}'


def test_registry_ingress_requires_a_distinct_token_and_uses_24_mib_limits():
    for pipeline_token, probe_token, health_token, registry_token in (
        ("same", None, None, "same"),
        (None, "same", None, "same"),
        (None, None, "same", "same"),
    ):
        with pytest.raises(
            ValueError, match="Invalid Inference pipeline HTTP server settings"
        ):
            PipelineHttpServer(
                _PipelineManager() if pipeline_token is not None else None,
                _PipelineData() if pipeline_token is not None else None,
                pipeline_token,
                backend_probe_manager=object() if probe_token is not None else None,
                backend_probe_control_token=probe_token,
                health_observation_manager=(
                    object() if health_token is not None else None
                ),
                health_observation_control_token=health_token,
                registry_ingress_manager=_RegistryManager(),
                registry_ingress_control_token=registry_token,
            )

    server = _server(_RegistryManager())
    assert server._request_limit("/v1/registry/env-sync") == 24 * 1024 * 1024
    assert server._request_limit("/v1/registry/env-sync-status") == 24 * 1024 * 1024
    assert server._response_limit("/v1/registry/env-sync") == 24 * 1024 * 1024
    assert server._response_limit("/v1/registry/env-sync-status") == 24 * 1024 * 1024
