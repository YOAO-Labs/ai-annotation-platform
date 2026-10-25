"""Framework-neutral backend HTTP transport behavior."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from types import SimpleNamespace

import httpx
import pytest
from aap_protocol_v2.lifecycle import AdmissionScope

import aap_inference.backend_transport as backend_transport
from aap_inference.backend_transport import (
    BackendTransport,
    BackendTransportError,
    PredictionResult,
    bounded_stream_response,
    raise_for_backend_status,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@dataclass
class _Options:
    ml_health_timeout: float = 2.0
    ml_predict_timeout: float = 7.0


class _Grant:
    generation = "17"
    admission_token = "signed-admission-token"

    def __init__(self, trace: list[tuple]) -> None:
        self.trace = trace
        self.has_response = False

    def report_response(self, status_code: int) -> None:
        self.has_response = True
        self.trace.append(("grant.response", status_code))

    def report_uncertain_if_missing(self, reason: str) -> None:
        if not self.has_response:
            self.trace.append(("grant.uncertain", reason))


def _backend(**overrides):
    values = {
        "url": "http://backend.test/",
        "auth_method": "token",
        "auth_token": "secret-token",
        "id": "backend-1",
        "gpu_resource_id": None,
        "vram_budget_mb": None,
        "extra_params": {"max_concurrency": 2},
        "health_meta": {
            "capabilities": {
                "task_contract_revision": 1,
                "models": [{"id": "sam3", "task": "segment"}],
            }
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _transport(backend=None, *, options=None, trace=None):
    trace = trace if trace is not None else []
    options = options if options is not None else _Options()

    @asynccontextmanager
    async def dispatch_context(operation, scope):
        trace.append(("dispatch.enter", operation, scope))
        grant = _Grant(trace)
        try:
            yield grant
        except BaseException:
            grant.report_uncertain_if_missing("request_aborted")
            trace.append(("dispatch.exit_error", operation))
            raise
        else:
            grant.report_uncertain_if_missing("response_not_reported")
            trace.append(("dispatch.exit", operation))

    def observe(backend_id, outcome, seconds):
        trace.append(("observe", backend_id, outcome, seconds))

    return (
        BackendTransport(
            backend or _backend(),
            options=options,
            dispatch_context=dispatch_context,
            observe=observe,
        ),
        trace,
        options,
    )


def _install_mock_transport(monkeypatch, handler):
    timeouts = []

    def make_client(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return _REAL_ASYNC_CLIENT(
            *args,
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    monkeypatch.setattr(backend_transport.httpx, "AsyncClient", make_client)
    return timeouts


@pytest.mark.asyncio
async def test_predict_uses_mock_http_dispatch_headers_result_and_live_timeouts(
    monkeypatch,
):
    trace = []
    requests = []

    async def handler(request):
        requests.append(request)
        trace.append(("http", request.method, request.url.path))
        if request.url.path == "/predict":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "task": "task-9",
                            "result": [{"label": "car"}],
                            "score": 0.75,
                            "model_version": "model-4",
                            "inference_time_ms": 31,
                            "meta": {"tokens": 8},
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"healthy": True})

    timeouts = _install_mock_transport(monkeypatch, handler)
    transport, trace, options = _transport(trace=trace)
    results = await transport.predict(
        [{"id": "task-9"}],
        {"model_id": "sam3", "task": "segmentation"},
    )

    assert len(results) == 1
    assert isinstance(results[0], PredictionResult)
    assert results[0].task_id == "task-9"
    assert results[0].result == [{"label": "car"}]
    assert results[0].score == 0.75
    assert results[0].model_version == "model-4"
    assert results[0].inference_time_ms == 31
    assert results[0].meta == {"tokens": 8}
    predict_request = requests[0]
    assert predict_request.headers["authorization"] == "Bearer secret-token"
    assert predict_request.headers[backend_transport.GPU_GENERATION_HEADER] == "17"
    assert (
        predict_request.headers[backend_transport.GPU_ADMISSION_TOKEN_HEADER]
        == "signed-admission-token"
    )
    assert json.loads(predict_request.content) == {
        "tasks": [{"id": "task-9"}],
        "context": {"model_id": "sam3", "task": "segment"},
    }
    assert trace[:4] == [
        ("dispatch.enter", "predict", AdmissionScope.PREDICT),
        ("http", "POST", "/predict"),
        ("grant.response", 200),
        ("dispatch.exit", "predict"),
    ]
    assert trace[-1][0:3] == ("observe", "backend-1", "success")
    assert trace[-1][3] >= 0
    assert timeouts == [7.0]

    options.ml_health_timeout = 13.5
    assert await transport.health() is True
    assert timeouts == [7.0, 13.5]
    assert requests[1].headers["authorization"] == "Bearer secret-token"
    assert backend_transport.get_semaphore("backend-1", 2) is transport._semaphore


@pytest.mark.asyncio
async def test_dispatch_reports_response_before_backend_error_and_observes_error(
    monkeypatch,
):
    trace = []

    async def handler(request):
        trace.append(("http", request.method, request.url.path))
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    _install_mock_transport(monkeypatch, handler)
    transport, trace, _ = _transport(trace=trace)

    with pytest.raises(httpx.HTTPStatusError):
        await transport.predict([{"id": "task-1"}])

    assert trace[:4] == [
        ("dispatch.enter", "predict", AdmissionScope.PREDICT),
        ("http", "POST", "/predict"),
        ("grant.response", 503),
        ("dispatch.exit_error", "predict"),
    ]
    assert trace[-1][0:3] == ("observe", "backend-1", "error")


@pytest.mark.asyncio
async def test_dispatch_marks_cancelled_request_uncertain_without_observing(
    monkeypatch,
):
    trace = []

    async def handler(request):
        trace.append(("http", request.method, request.url.path))
        raise asyncio.CancelledError

    _install_mock_transport(monkeypatch, handler)
    transport, trace, _ = _transport(trace=trace)

    with pytest.raises(asyncio.CancelledError):
        await transport.predict([{"id": "task-1"}])

    assert trace == [
        ("dispatch.enter", "predict", AdmissionScope.PREDICT),
        ("http", "POST", "/predict"),
        ("grant.uncertain", "request_aborted"),
        ("dispatch.exit_error", "predict"),
    ]


@pytest.mark.asyncio
async def test_health_challenge_fallback_and_response_echo(monkeypatch):
    requests = []
    challenge = "ab" * 32

    async def fallback_handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(422)
        return httpx.Response(
            200,
            json={
                "gpu_info": {"name": "gpu"},
                "host": {"name": "worker"},
                backend_transport.GPU_HEALTH_CHALLENGE_ECHO_MARKER: challenge,
                "unselected": "ignored",
            },
            headers={backend_transport.GPU_HEALTH_CHALLENGE_HEADER: challenge},
        )

    _install_mock_transport(monkeypatch, fallback_handler)
    transport, _, _ = _transport()
    healthy, meta = await transport.health_meta(gpu_health_challenge=challenge)

    assert healthy is True
    assert meta == {"gpu_info": {"name": "gpu"}, "host": {"name": "worker"}}
    assert len(requests) == 2
    first, fallback = requests
    assert (
        first.url.params[backend_transport.GPU_HEALTH_CHALLENGE_QUERY_PARAM]
        == challenge
    )
    assert first.headers.get_list(backend_transport.GPU_HEALTH_CHALLENGE_HEADER) == [
        challenge
    ]
    assert first.headers["cache-control"] == "no-cache"
    assert (
        fallback.url.params.get(backend_transport.GPU_HEALTH_CHALLENGE_QUERY_PARAM)
        is None
    )
    assert (
        fallback.headers.get_list(backend_transport.GPU_HEALTH_CHALLENGE_HEADER) == []
    )
    assert fallback.headers["authorization"] == "Bearer secret-token"

    echo_requests = []

    async def echo_handler(request):
        echo_requests.append(request)
        return httpx.Response(
            200,
            json={"gpu_info": {"name": "gpu"}},
            headers={backend_transport.GPU_HEALTH_CHALLENGE_HEADER: challenge},
        )

    _install_mock_transport(monkeypatch, echo_handler)
    echoed, echo_meta = await transport.health_meta(gpu_health_challenge=challenge)
    assert echoed is True
    assert echo_meta == {
        "gpu_info": {"name": "gpu"},
        backend_transport.GPU_HEALTH_CHALLENGE_ECHO_MARKER: challenge,
    }
    assert len(echo_requests) == 1


@pytest.mark.asyncio
async def test_bounded_reply_error_shape_and_upstream_retry_header():
    statuses = []

    def public_error(*, status_code, detail, headers=None):
        statuses.append(status_code)
        return BackendTransportError(status_code, detail, headers)

    oversized = httpx.Response(
        200,
        headers={"content-length": "12"},
        content=b"ignored body",
    )
    with pytest.raises(BackendTransportError) as limit_error:
        await bounded_stream_response(
            oversized,
            max_bytes=8,
            reason="evaluation_response_too_large",
            error_factory=public_error,
        )
    assert limit_error.value.status_code == 413
    assert limit_error.value.detail == {
        "reason": "evaluation_response_too_large",
        "message": "ML backend response exceeds the byte budget",
        "max_bytes": 8,
        "declared_bytes": 12,
    }

    upstream = httpx.Response(
        503,
        json={
            "detail": {
                "reason": "model_busy",
                "message": "queue full",
                "model_id": "sam3",
                "private_path": "/srv/model",
            }
        },
        headers={"Retry-After": "4"},
    )
    with pytest.raises(BackendTransportError) as upstream_error:
        raise_for_backend_status(upstream, error_factory=public_error)
    assert upstream_error.value.status_code == 503
    assert upstream_error.value.headers == {"Retry-After": "4"}
    assert upstream_error.value.detail == {
        "reason": "model_busy",
        "message": "queue full",
        "model_id": "sam3",
    }
    assert statuses == [413, 503]


@pytest.mark.asyncio
async def test_generation_transition_is_restricted_and_reports_response(monkeypatch):
    requests = []
    trace = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(monkeypatch, handler)
    transport, trace, _ = _transport(trace=trace)
    grant = _Grant(trace)

    response = await transport._managed_generation_transition("/drain", grant)
    assert response.status_code == 200
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/drain"
    assert request.headers[backend_transport.GPU_GENERATION_HEADER] == "17"
    assert (
        request.headers[backend_transport.GPU_ADMISSION_TOKEN_HEADER]
        == "signed-admission-token"
    )
    assert json.loads(request.content) == {"generation": "17"}
    assert trace == [("grant.response", 200)]

    with pytest.raises(ValueError, match="managed GPU transition endpoint is invalid"):
        await transport._managed_generation_transition("/reload", grant)
    assert len(requests) == 1
