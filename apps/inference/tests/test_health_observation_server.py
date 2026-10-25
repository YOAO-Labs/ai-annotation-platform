from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

import pytest
from aap_inference_contract.health_observation import (
    HEALTH_OBSERVATION_PROTOCOL,
    BackendHealthObservationReply,
    BackendHealthObservationUnavailable,
    decode_health_observation_reply,
    health_observation_request_bytes,
    make_health_observation_request,
)

from aap_inference.pipeline_server import PipelineHttpServer

_REGISTRY_ID = UUID("9ad8377b-f969-4135-8f48-22eb45c80bbd")
_OTHER_REGISTRY_ID = UUID("f6b0124e-872a-4ae8-8244-7cb8fe69bc1f")
_REQUEST_ID = UUID("6a8382ce-7330-4779-8515-1fa84689f15f")
_SUBJECT = "v1." + base64.b64encode(b"opaque subject").decode()
_EVIDENCE = "v1." + base64.b64encode(b"sealed outcome").decode()


class _PipelineManager:
    def __init__(self):
        self.calls = []

    async def execute(self, body):
        self.calls.append(("execute", body))

    async def query(self, body):
        self.calls.append(("query", body))

    async def status(self, body):
        self.calls.append(("status", body))


class _PipelineData:
    def __init__(self, ready=True):
        self.ready = ready

    async def readiness(self):
        return self.ready


class _HealthObservationManager:
    def __init__(self, *, ready=True, failure=None, wrong_binding=False):
        self.ready = ready
        self.failure = failure
        self.wrong_binding = wrong_binding
        self.calls = []

    async def readiness(self):
        return self.ready

    async def execute(self, request):
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        registry_id = _OTHER_REGISTRY_ID if self.wrong_binding else request.registry_id
        return BackendHealthObservationReply(
            schema=HEALTH_OBSERVATION_PROTOCOL,
            request_id=request.request_id,
            registry_id=registry_id,
            subject_sha256=hashlib.sha256(request.subject.encode("ascii")).hexdigest(),
            evidence=_EVIDENCE,
        )


def _request():
    return make_health_observation_request(
        _REGISTRY_ID,
        _SUBJECT,
        request_id=_REQUEST_ID,
    )


def _server(*, observation_manager=None, observation_token="observation-token"):
    pipeline_manager = _PipelineManager()
    pipeline_data = _PipelineData()
    server = PipelineHttpServer(
        pipeline_manager,
        pipeline_data,
        "pipeline-token",
        backend_probe_manager=object(),
        backend_probe_control_token="probe-token",
        health_observation_manager=observation_manager,
        health_observation_control_token=observation_token,
    )
    return server, pipeline_manager


@pytest.mark.asyncio
async def test_observation_route_has_an_independent_token_and_bound_reply():
    manager = _HealthObservationManager()
    server, pipeline_manager = _server(observation_manager=manager)
    request = _request()
    body = health_observation_request_bytes(request)

    wrong_pipeline_status, _ = await server._dispatch(
        "POST",
        "/v1/backends/health-observation",
        {"authorization": "Bearer pipeline-token"},
        body,
    )
    wrong_probe_status, _ = await server._dispatch(
        "POST",
        "/v1/backends/health-observation",
        {"authorization": "Bearer probe-token"},
        body,
    )
    assert wrong_pipeline_status == 401
    assert wrong_probe_status == 401
    assert manager.calls == []

    status, response = await server._dispatch(
        "POST",
        "/v1/backends/health-observation",
        {"authorization": "Bearer observation-token"},
        body,
    )

    reply = decode_health_observation_reply(request, response)
    assert status == 200
    assert reply.registry_id == _REGISTRY_ID
    assert reply.evidence == _EVIDENCE
    assert len(manager.calls) == 1
    assert pipeline_manager.calls == []


def test_server_rejects_reusing_any_control_token():
    for pipeline_token, probe_token, observation_token in (
        ("same", "same", "observation"),
        ("same", "probe", "same"),
        ("pipeline", "same", "same"),
    ):
        with pytest.raises(
            ValueError, match="Invalid Inference pipeline HTTP server settings"
        ):
            PipelineHttpServer(
                _PipelineManager(),
                _PipelineData(),
                pipeline_token,
                backend_probe_manager=object(),
                backend_probe_control_token=probe_token,
                health_observation_manager=object(),
                health_observation_control_token=observation_token,
            )


@pytest.mark.asyncio
async def test_observation_route_maps_invalid_and_unavailable_without_detail_leaks():
    manager = _HealthObservationManager()
    server, _pipeline_manager = _server(observation_manager=manager)
    auth = {"authorization": "Bearer observation-token"}

    invalid_status, invalid_body = await server._dispatch(
        "POST", "/v1/backends/health-observation", auth, b"{}"
    )
    assert invalid_status == 400
    assert json.loads(invalid_body) == {
        "detail": "Invalid Inference health observation request"
    }
    assert manager.calls == []

    manager.failure = BackendHealthObservationUnavailable()
    unavailable_status, unavailable_body = await server._dispatch(
        "POST",
        "/v1/backends/health-observation",
        auth,
        health_observation_request_bytes(_request()),
    )
    assert unavailable_status == 503
    assert json.loads(unavailable_body) == {
        "detail": "Inference health observation owner unavailable"
    }
    assert "synthetic" not in unavailable_body.decode()


@pytest.mark.asyncio
async def test_observation_route_rejects_reply_bound_to_another_registry():
    manager = _HealthObservationManager(wrong_binding=True)
    server, _pipeline_manager = _server(observation_manager=manager)

    status, body = await server._dispatch(
        "POST",
        "/v1/backends/health-observation",
        {"authorization": "Bearer observation-token"},
        health_observation_request_bytes(_request()),
    )

    assert status == 503
    assert json.loads(body) == {
        "detail": "Inference health observation owner unavailable"
    }


@pytest.mark.asyncio
async def test_readiness_includes_selected_owner_and_keeps_pipeline_only_body():
    observation_manager = _HealthObservationManager(ready=True)
    observation_server = PipelineHttpServer(
        None,
        None,
        None,
        health_observation_manager=observation_manager,
        health_observation_control_token="observation-only-token",
    )
    status, body = await observation_server._dispatch("GET", "/health", {}, b"")
    assert status == 200
    assert json.loads(body) == {"status": "ready"}

    pipeline_server = PipelineHttpServer(
        _PipelineManager(), _PipelineData(ready=True), "pipeline-only-token"
    )
    status, body = await pipeline_server._dispatch("GET", "/health", {}, b"")
    assert status == 200
    assert body == b'{"status":"ready"}'


def test_health_observation_uses_its_wire_budget_without_changing_pipeline_budget():
    server, _pipeline_manager = _server(observation_manager=_HealthObservationManager())
    assert server._request_limit("/v1/backends/health-observation") == 24 * 1024 * 1024
    assert server._response_limit("/v1/backends/health-observation") == 24 * 1024 * 1024
    assert server._request_limit("/v1/pipelines/execute") == server.max_request_bytes
    assert server._response_limit("/v1/pipelines/execute") == server.max_response_bytes
