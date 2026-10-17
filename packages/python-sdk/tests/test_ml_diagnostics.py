from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from ai_annotation.ml_diagnostic_models import (
    GPUArbiterResourcesResponse,
    MLIntegrationsOverview,
    ObserveResponse,
    ProbeRequest,
    ProbeResponse,
    RuntimeHints,
    SmokeTestRequest,
    SmokeTestResponse,
)
from ai_annotation.ml_diagnostics import MLDiagnostics

from .conftest import API


def _resource(client) -> MLDiagnostics:
    return MLDiagnostics(client._http)


def test_global_diagnostics_return_typed_snapshots(client, respx_mock):
    overview = respx_mock.get(f"{API}/admin/ml-integrations/overview").mock(
        return_value=httpx.Response(
            200,
            json={
                "storage": {
                    "items": [
                        {
                            "name": "datasets",
                            "status": "ok",
                            "role": "datasets",
                            "object_count": 3,
                            "total_size_bytes": 42,
                        }
                    ],
                    "total_object_count": 3,
                    "total_size_bytes": 42,
                },
                "projects": [],
                "total_backends": 2,
                "connected_backends": 1,
            },
        )
    )
    gpu = respx_mock.get(f"{API}/admin/ml-integrations/gpu-resources").mock(
        return_value=httpx.Response(
            200,
            json={
                "global_desired_mode": "observe",
                "rollout_enabled": True,
                "runtime_ready": True,
                "observe_runtime_ready": True,
                "enforce_runtime_ready": False,
                "resources": [],
                "diagnostics": [],
            },
        )
    )
    observe = respx_mock.get(f"{API}/admin/ml-integrations/observe").mock(
        return_value=httpx.Response(200, json={"targets": [], "configured_count": 0})
    )
    hints = respx_mock.get(f"{API}/admin/ml-integrations/runtime-hints").mock(
        return_value=httpx.Response(200, json={"ml_backend_default_url": "http://ml"})
    )

    resource = _resource(client)
    overview_result = resource.overview()
    gpu_result = resource.gpu_resources()
    observe_result = resource.observe()
    hints_result = resource.runtime_hints()

    assert isinstance(overview_result, MLIntegrationsOverview)
    assert overview_result.storage.items[0].object_count == 3
    assert isinstance(gpu_result, GPUArbiterResourcesResponse)
    assert gpu_result.global_desired_mode == "observe"
    assert isinstance(observe_result, ObserveResponse)
    assert observe_result.configured_count == 0
    assert isinstance(hints_result, RuntimeHints)
    assert hints_result.ml_backend_default_url == "http://ml"
    assert all(route.called for route in (overview, gpu, observe, hints))


def test_probe_and_smoke_test_preserve_runtime_outcomes(client, respx_mock):
    probe = respx_mock.post(f"{API}/admin/ml-integrations/probe").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": False,
                "latency_ms": 17,
                "status_code": 503,
                "error": "HTTP 503",
            },
        )
    )
    smoke = respx_mock.post(f"{API}/admin/ml-integrations/observe/smoke-test").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "skipped": True, "message": "model already loaded"},
        )
    )

    resource = _resource(client)
    probe_result = resource.probe(
        ProbeRequest(
            url="https://backend.example", auth_method="token", auth_token="secret"
        )
    )
    smoke_result = resource.smoke_test(
        SmokeTestRequest(url="https://backend.example", sam_variant="tiny")
    )

    assert isinstance(probe_result, ProbeResponse)
    assert probe_result.ok is False
    assert probe_result.status_code == 503
    assert json.loads(probe.calls.last.request.content)["auth_token"] == "secret"
    assert isinstance(smoke_result, SmokeTestResponse)
    assert smoke_result.skipped is True
    assert smoke.called


def test_probe_secret_repr_errors_and_unused_none_token(client, respx_mock):
    secret = "probe-secret-value"
    token_request = ProbeRequest(
        url="https://backend.example", auth_method="token", auth_token=secret
    )
    assert secret not in repr(token_request)
    assert token_request.model_dump(mode="json")["auth_token"] == secret

    with pytest.raises(ValidationError) as error:
        ProbeRequest(
            url="https://backend.example",
            auth_method="token",
            auth_token={"token": secret},
        )
    assert secret not in str(error.value)

    route = respx_mock.post(f"{API}/admin/ml-integrations/probe").mock(
        return_value=httpx.Response(200, json={"ok": True, "latency_ms": 5})
    )
    _resource(client).probe(
        ProbeRequest(
            url="https://backend.example", auth_method="none", auth_token=secret
        )
    )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"url": "https://backend.example", "auth_method": "none"}
