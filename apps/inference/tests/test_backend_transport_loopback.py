"""Exercise the installed transport through a real local TCP backend."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from aap_protocol_v2.lifecycle import AdmissionScope

from aap_inference.backend_transport import (
    BackendTransport,
    BackendTransportError,
    PredictionResult,
)


_HEALTH_CHALLENGE = "c3" * 32
_GPU_GENERATION_HEADER = "X-AAP-GPU-Generation"
_GPU_ADMISSION_TOKEN_HEADER = "X-AAP-GPU-Admission-Token"
_GPU_HEALTH_CHALLENGE_HEADER = "X-AAP-GPU-Health-Challenge"


@dataclass
class _Options:
    ml_health_timeout: float = 3.0
    ml_predict_timeout: float = 8.0


class _Grant:
    generation = "23"
    admission_token = "loopback-signed-grant"

    def __init__(self, trace: list[tuple]) -> None:
        self.trace = trace
        self.response_reported = False

    def report_response(self, status_code: int) -> None:
        self.response_reported = True
        self.trace.append(("grant.response", status_code))

    def report_uncertain_if_missing(self, reason: str) -> None:
        if not self.response_reported:
            self.trace.append(("grant.uncertain", reason))


class _BackendHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _BackendRequestHandler)
        self.observations: list[dict] = []
        self.failures: list[str] = []
        self.predict_count = 0
        self.warmup_count = 0
        self.observation_lock = threading.Lock()

    def record_request(self, observation: dict) -> int:
        with self.observation_lock:
            self.observations.append(observation)
            if observation["path"] == "/predict":
                self.predict_count += 1
                return self.predict_count
            if observation["path"] == "/warmup":
                self.warmup_count += 1
                return self.warmup_count
            return 0


class _BackendRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *args) -> None:
        return None

    def _send(self, status: int, body: dict, *, headers: dict[str, str] | None = None):
        content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def _observe(self) -> tuple[dict, dict]:
        split = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b""
        body = json.loads(raw_body) if raw_body else None
        interesting_headers = {
            name.lower(): self.headers.get(name)
            for name in (
                "Authorization",
                "Cache-Control",
                _GPU_GENERATION_HEADER,
                _GPU_ADMISSION_TOKEN_HEADER,
                _GPU_HEALTH_CHALLENGE_HEADER,
                "Content-Type",
            )
            if self.headers.get(name) is not None
        }
        observation = {
            "method": self.command,
            "path": split.path,
            "query": parse_qs(split.query, keep_blank_values=True),
            "headers": interesting_headers,
            "body": body,
        }
        request_number = self.server.record_request(observation)
        return observation, {"request_number": request_number}

    def _handle(self) -> None:
        try:
            observation, context = self._observe()
            if observation["method"] == "GET" and observation["path"] == "/health":
                headers = observation["headers"]
                query = observation["query"].get("aap_gpu_health_challenge", [])
                challenge = headers.get(_GPU_HEALTH_CHALLENGE_HEADER.lower())
                response_headers = {}
                if challenge == _HEALTH_CHALLENGE and query == [_HEALTH_CHALLENGE]:
                    response_headers[_GPU_HEALTH_CHALLENGE_HEADER] = challenge
                self._send(
                    200,
                    {
                        "gpu_info": {"device": "loopback-cpu"},
                        "host": {"name": "synthetic-backend"},
                    },
                    headers=response_headers,
                )
                return
            if observation["method"] == "GET" and observation["path"] == "/setup":
                self._send(200, {"capabilities": {"model_ids": ["sam3"]}})
                return
            if observation["method"] == "POST" and observation["path"] == "/predict":
                if context["request_number"] == 1:
                    self._send(
                        200,
                        {
                            "results": [
                                {
                                    "task": "task-loopback-1",
                                    "result": [{"label": "synthetic-object"}],
                                    "score": 0.91,
                                    "model_version": "synthetic-model",
                                    "inference_time_ms": 19,
                                    "meta": {"source": "loopback"},
                                }
                            ]
                        },
                    )
                    return
                self._send(
                    503,
                    {"detail": "synthetic overload"},
                    headers={"Retry-After": "2"},
                )
                return
            if observation["method"] == "POST" and observation["path"] == "/warmup":
                self._send(
                    503,
                    {"detail": "synthetic warmup overload"},
                    headers={"Retry-After": "2"},
                )
                return
            self._send(404, {"detail": "unknown synthetic route"})
        except Exception as exc:
            self.server.failures.append(type(exc).__name__)
            try:
                self._send(500, {"detail": "synthetic handler failed"})
            except OSError:
                pass

    do_GET = _handle
    do_POST = _handle


@contextmanager
def _owned_loopback_backend():
    server = _BackendHTTPServer()
    thread = threading.Thread(
        target=server.serve_forever,
        name="backend-transport-loopback",
        daemon=True,
    )
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "owned loopback HTTP server did not stop"


@pytest.mark.asyncio
async def test_backend_transport_uses_real_loopback_tcp_for_health_setup_and_predict(
    monkeypatch,
):
    # Keep the request on its owned loopback listener even when CI configures a proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    events: list[tuple] = []
    options = _Options()

    @asynccontextmanager
    async def dispatch_context(operation, scope):
        events.append(("dispatch.enter", operation, scope))
        grant = _Grant(events)
        try:
            yield grant
        except BaseException:
            grant.report_uncertain_if_missing("request_aborted")
            events.append(("dispatch.exit_error", operation))
            raise
        else:
            grant.report_uncertain_if_missing("response_not_reported")
            events.append(("dispatch.exit", operation))

    def observe(backend_id, outcome, seconds):
        events.append(("observe", backend_id, outcome, seconds))

    with _owned_loopback_backend() as (server, base_url):
        backend = SimpleNamespace(
            url=f"{base_url}/",
            auth_method="token",
            auth_token="synthetic-only-token",
            id="loopback-backend-23",
            gpu_resource_id=None,
            vram_budget_mb=None,
            extra_params={"max_concurrency": 2},
            health_meta={
                "capabilities": {
                    "task_contract_revision": 1,
                    "models": [{"id": "sam3", "task": "segment"}],
                }
            },
        )
        original_backend_url = backend.url
        original_backend_id = backend.id
        transport = BackendTransport(
            backend,
            options=options,
            dispatch_context=dispatch_context,
            observe=observe,
        )

        healthy, health_meta = await transport.health_meta(
            gpu_health_challenge=_HEALTH_CHALLENGE
        )
        assert healthy is True
        assert health_meta == {
            "gpu_info": {"device": "loopback-cpu"},
            "host": {"name": "synthetic-backend"},
            "_aap_gpu_health_challenge_echo": _HEALTH_CHALLENGE,
        }
        setup = await transport.setup()
        assert setup == {"capabilities": {"model_ids": ["sam3"]}}

        results = await transport.predict(
            [{"id": "task-loopback-1"}],
            {"model_id": "sam3", "task": "segmentation"},
            max_response_bytes=8192,
        )
        assert results == [
            PredictionResult(
                task_id="task-loopback-1",
                result=[{"label": "synthetic-object"}],
                score=0.91,
                model_version="synthetic-model",
                inference_time_ms=19,
                meta={"source": "loopback"},
            )
        ]

        with pytest.raises(BackendTransportError) as unavailable:
            await transport.predict(
                [{"id": "task-loopback-2"}], max_response_bytes=8192
            )
        assert unavailable.value.status_code == 503
        assert unavailable.value.headers == {"Retry-After": "2"}

        with pytest.raises(httpx.HTTPStatusError) as warmup_error:
            await transport.warmup({"task": "segmentation"})
        assert warmup_error.value.response.status_code == 503
        assert warmup_error.value.response.headers["retry-after"] == "2"

        assert server.failures == []
        assert backend.url == original_backend_url
        assert backend.id == original_backend_id
        assert transport.base_url == base_url
        assert server.predict_count == 2, "the failed prediction must not be retried"
        assert server.warmup_count == 1, "the failed warmup must not be retried"

        observations = server.observations
        assert [item["method"] + " " + item["path"] for item in observations] == [
            "GET /health",
            "GET /setup",
            "POST /predict",
            "POST /predict",
            "POST /warmup",
        ]
        (
            challenge_request,
            setup_request,
            predict_request,
            failed_predict,
            failed_warmup,
        ) = observations
        assert challenge_request["query"] == {
            "aap_gpu_health_challenge": [_HEALTH_CHALLENGE]
        }
        assert challenge_request["headers"]["x-aap-gpu-health-challenge"] == (
            _HEALTH_CHALLENGE
        )
        assert challenge_request["headers"]["cache-control"] == "no-cache"
        assert setup_request["headers"]["cache-control"] == "no-cache"
        assert predict_request["headers"]["authorization"] == (
            "Bearer synthetic-only-token"
        )
        assert predict_request["headers"]["x-aap-gpu-generation"] == "23"
        assert predict_request["headers"]["x-aap-gpu-admission-token"] == (
            "loopback-signed-grant"
        )
        assert predict_request["body"] == {
            "tasks": [{"id": "task-loopback-1"}],
            "context": {"model_id": "sam3", "task": "segment"},
        }
        assert failed_predict["body"] == {"tasks": [{"id": "task-loopback-2"}]}
        assert failed_warmup["body"] == {"task": "segment"}
        assert failed_warmup["headers"]["authorization"] == (
            "Bearer synthetic-only-token"
        )
        assert failed_warmup["headers"]["x-aap-gpu-generation"] == "23"
        assert failed_warmup["headers"]["x-aap-gpu-admission-token"] == (
            "loopback-signed-grant"
        )

    assert [event[:3] for event in events] == [
        ("dispatch.enter", "predict", AdmissionScope.PREDICT),
        ("grant.response", 200),
        ("dispatch.exit", "predict"),
        ("observe", "loopback-backend-23", "success"),
        ("dispatch.enter", "predict", AdmissionScope.PREDICT),
        ("grant.response", 503),
        ("dispatch.exit_error", "predict"),
        ("observe", "loopback-backend-23", "error"),
        ("dispatch.enter", "warmup", AdmissionScope.WARMUP),
        ("grant.response", 503),
        ("dispatch.exit_error", "warmup"),
    ]
    assert events[3][3] >= 0 and events[7][3] >= 0
