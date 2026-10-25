from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID

import pytest
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BACKEND_PROBE_PROTOCOL,
    BackendProbeFailure,
    BackendProbeSuccess,
    BackendProbeUnavailable,
    backend_probe_request_bytes,
    decode_backend_probe_reply,
    make_backend_probe_request,
)

from aap_inference.pipeline_server import PipelineHttpServer


_REGISTRY_ID = UUID("764c1509-a323-4b89-8cc3-991cb3a0f72f")
_SUBJECT = "v1." + base64.b64encode(b"opaque probe subject").decode("ascii")


def _request():
    return make_backend_probe_request(
        _REGISTRY_ID,
        _SUBJECT,
        timeout=2,
        limits=CapabilityLimits(),
        request_id=UUID("1079f90d-b6d8-420d-87f5-4f8f244093be"),
    )


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


class _ProbeManager:
    def __init__(self, *, ready=True, error=None, failure=False):
        self.ready = ready
        self.error = error
        self.failure = failure
        self.calls = []

    async def readiness(self):
        return self.ready

    async def execute(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        subject_sha256 = hashlib.sha256(
            request.probe_subject.encode("ascii")
        ).hexdigest()
        if self.failure:
            return BackendProbeFailure(
                schema=BACKEND_PROBE_PROTOCOL,
                ok=False,
                request_id=request.request_id,
                registry_id=request.registry_id,
                subject_sha256=subject_sha256,
                error="legacy_materialization_failure",
            )
        return BackendProbeSuccess(
            schema=BACKEND_PROBE_PROTOCOL,
            ok=True,
            request_id=request.request_id,
            registry_id=request.registry_id,
            subject_sha256=subject_sha256,
            modalities=["image", "video"],
        )


def _server(*, probe_manager=None, probe_token="probe-token"):
    pipeline_manager = _PipelineManager()
    pipeline_data = _PipelineData()
    server = PipelineHttpServer(
        pipeline_manager,
        pipeline_data,
        "pipeline-token",
        backend_probe_manager=probe_manager,
        backend_probe_control_token=probe_token,
    )
    return server, pipeline_manager


@pytest.mark.asyncio
async def test_probe_and_pipeline_routes_have_separate_control_tokens():
    probe_manager = _ProbeManager()
    server, pipeline_manager = _server(probe_manager=probe_manager)
    request = _request()
    body = backend_probe_request_bytes(request)

    pipeline_token_status, _ = await server._dispatch(
        "POST",
        "/v1/backends/probe",
        {"authorization": "Bearer pipeline-token"},
        body,
    )
    probe_token_pipeline_status, _ = await server._dispatch(
        "POST",
        "/v1/pipelines/query",
        {"authorization": "Bearer probe-token"},
        b"{}",
    )
    assert pipeline_token_status == 401
    assert probe_token_pipeline_status == 401
    assert probe_manager.calls == []
    assert pipeline_manager.calls == []

    status, response = await server._dispatch(
        "POST",
        "/v1/backends/probe",
        {"authorization": "Bearer probe-token"},
        body,
    )
    reply = decode_backend_probe_reply(request, response)
    assert status == 200
    assert reply.ok is True
    assert reply.modalities == ["image", "video"]
    assert len(probe_manager.calls) == 1


def test_server_rejects_reusing_pipeline_control_secret_for_probe():
    with pytest.raises(
        ValueError, match="Invalid Inference pipeline HTTP server settings"
    ):
        PipelineHttpServer(
            _PipelineManager(),
            _PipelineData(True),
            "same-control-secret",
            backend_probe_manager=_ProbeManager(),
            backend_probe_control_token="same-control-secret",
        )


@pytest.mark.asyncio
async def test_probe_route_maps_invalid_oversized_unavailable_and_materialization():
    probe_manager = _ProbeManager()
    server, _pipeline_manager = _server(probe_manager=probe_manager)
    auth = {"authorization": "Bearer probe-token"}

    invalid_status, invalid_body = await server._dispatch(
        "POST", "/v1/backends/probe", auth, b"{}"
    )
    assert invalid_status == 400
    assert json.loads(invalid_body) == {
        "detail": "Invalid Inference backend probe request"
    }
    assert probe_manager.calls == []

    valid_body = backend_probe_request_bytes(_request())
    server.max_request_bytes = len(valid_body) - 1
    oversized_status, oversized_body = await server._dispatch(
        "POST", "/v1/backends/probe", auth, valid_body
    )
    assert oversized_status == 413
    assert json.loads(oversized_body) == {"detail": "Request body too large"}
    assert probe_manager.calls == []
    server.max_request_bytes = len(valid_body)

    unavailable_manager = _ProbeManager(error=BackendProbeUnavailable())
    unavailable_server, _ = _server(probe_manager=unavailable_manager)
    unavailable_status, unavailable_body = await unavailable_server._dispatch(
        "POST", "/v1/backends/probe", auth, valid_body
    )
    assert unavailable_status == 503
    assert json.loads(unavailable_body) == {
        "detail": "Inference backend probe owner unavailable"
    }

    failure_manager = _ProbeManager(failure=True)
    failure_server, _ = _server(probe_manager=failure_manager)
    failure_status, failure_body = await failure_server._dispatch(
        "POST", "/v1/backends/probe", auth, valid_body
    )
    failure = decode_backend_probe_reply(_request(), failure_body)
    assert failure_status == 200
    assert isinstance(failure, BackendProbeFailure)
    assert failure.error == "legacy_materialization_failure"
    assert "synthetic-endpoint-token" not in failure_body.decode("ascii")


@pytest.mark.asyncio
async def test_probe_only_readiness_and_pipeline_only_health_body_compatibility():
    pipeline_manager = _PipelineManager()
    pipeline_server = PipelineHttpServer(
        pipeline_manager,
        _PipelineData(True),
        "pipeline-token",
    )
    status, body = await pipeline_server._dispatch("GET", "/health", {}, b"")
    assert (status, json.loads(body)) == (200, {"status": "ready"})

    probe_only = PipelineHttpServer(
        None,
        None,
        None,
        backend_probe_manager=_ProbeManager(ready=True),
        backend_probe_control_token="probe-token",
    )
    status, body = await probe_only._dispatch("GET", "/health", {}, b"")
    assert (status, json.loads(body)) == (200, {"status": "ready"})
    pipeline_status, _ = await probe_only._dispatch(
        "POST",
        "/v1/pipelines/query",
        {"authorization": "Bearer probe-token"},
        b"{}",
    )
    assert pipeline_status == 503

    probe_not_ready = PipelineHttpServer(
        None,
        None,
        None,
        backend_probe_manager=_ProbeManager(ready=False),
        backend_probe_control_token="probe-token",
    )
    status, body = await probe_not_ready._dispatch("GET", "/health", {}, b"")
    assert (status, json.loads(body)) == (503, {"status": "not_ready"})

    combined = PipelineHttpServer(
        _PipelineManager(),
        _PipelineData(False),
        "pipeline-token",
        backend_probe_manager=_ProbeManager(ready=True),
        backend_probe_control_token="probe-token",
    )
    status, body = await combined._dispatch("GET", "/health", {}, b"")
    assert (status, json.loads(body)) == (200, {"status": "ready"})
