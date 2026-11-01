"""Credential routing and cleanup through real, isolated loopback HTTP listeners."""

from contextlib import asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aap_inference_contract.backend_control import (
    BACKEND_CONTROL_PROTOCOL,
    BackendRequestOnlyEndpoint,
    BackendRequestOnlyProbeRequest,
    BackendSmokeRequest,
    BackendUnregisteredSmokeOptions,
)

from aap_inference.backend_control_manager import BackendControlManager
from aap_inference.backend_transport import BackendTransport


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        self.server.calls.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        redirect = self.path in self.server.redirect_paths
        body = (
            b"not-json"
            if self.path in self.server.invalid_paths
            else json.dumps({"ok": True, "reloaded": True}).encode()
        )
        self.send_response(307 if redirect else 200)
        if redirect:
            self.send_header("Location", self.server.redirect_url)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    do_GET = handle_request
    do_POST = handle_request


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.calls = []
    server.redirect_paths = set()
    server.invalid_paths = set()
    server.redirect_url = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@asynccontextmanager
async def _dispatch(*_args):
    yield None


def _manager():
    async def ready():
        return True

    async def open_endpoint(*_args):
        raise AssertionError("unexpected registered endpoint")

    opener = SimpleNamespace(open=open_endpoint, readiness=ready)
    stats = SimpleNamespace(snapshots=ready, readiness=ready)
    return BackendControlManager(
        {
            purpose: opener
            for purpose in (
                "setup",
                "health_observation",
                "catalog_fallback",
                "stats",
                "backend_read",
                "lifecycle",
                "execution",
            )
        },
        SimpleNamespace(ml_health_timeout=2, ml_predict_timeout=2),
        request_context_factory=lambda *_args: None,
        dispatch_context_factory=lambda *_args: _dispatch(),
        unregistered_smoke_dispatch=_dispatch,
        stats_data=stats,
    )


def _probe(operation, endpoint):
    return BackendRequestOnlyProbeRequest(
        kind="request_only",
        schema=BACKEND_CONTROL_PROTOCOL,
        operation=operation,
        request_id=uuid4(),
        endpoints=(endpoint,),
        timeout_seconds=2,
    )


def _smoke(endpoint):
    return BackendSmokeRequest(
        kind="smoke_unregistered",
        schema=BACKEND_CONTROL_PROTOCOL,
        request_id=uuid4(),
        endpoint=endpoint,
        body=BackendUnregisteredSmokeOptions(),
    )


@pytest.mark.asyncio
async def test_owner_requests_ignore_proxy_and_never_follow_credentialed_redirects(
    monkeypatch,
):
    with (
        _server() as (backend, url),
        _server() as (proxy, proxy_url),
        _server() as (attacker, attacker_url),
    ):
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            monkeypatch.setenv(key, proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        endpoint = BackendRequestOnlyEndpoint(
            url=url, auth_method="token", auth_token="synthetic-secret"
        )
        manager = _manager()
        fact = await manager.execute(_probe("manual_probe", endpoint))
        assert fact.result["results"][0]["ok"]
        observed = await manager.execute(
            _probe("environment_observation", endpoint)
        )
        assert observed.result["results"][0]["ok"]
        smoke = await manager.execute(_smoke(endpoint))
        assert smoke.result["auto_unloaded"] is True
        assert backend.calls == [
            ("GET", "/health", "Bearer synthetic-secret"),
            ("GET", "/health", None),
            ("GET", "/setup", "Bearer synthetic-secret"),
            ("POST", "/reload", "Bearer synthetic-secret"),
            ("POST", "/unload", "Bearer synthetic-secret"),
        ]
        assert proxy.calls == []
        backend.invalid_paths = {"/reload"}
        malformed = await manager.execute(_smoke(endpoint))
        assert malformed.error is not None
        assert backend.calls[-2:] == [
            ("POST", "/reload", "Bearer synthetic-secret"),
            ("POST", "/unload", "Bearer synthetic-secret"),
        ]
        backend.invalid_paths.clear()
        backend.redirect_url = attacker_url + "/stolen"
        backend.redirect_paths = {"/setup"}
        setup_redirect = await manager.execute(_probe("environment_observation", endpoint))
        assert setup_redirect.result["results"][0]["setup"] is None
        backend.redirect_paths = {"/health", "/reload"}
        redirected = await manager.execute(_probe("manual_probe", endpoint))
        assert not redirected.result["results"][0]["ok"]
        failed_smoke = await manager.execute(_smoke(endpoint))
        assert failed_smoke.error is not None
        assert attacker.calls == [] and proxy.calls == []


@pytest.mark.asyncio
async def test_registered_transport_ignores_proxy_and_redirects(monkeypatch):
    with (
        _server() as (backend, url),
        _server() as (proxy, proxy_url),
        _server() as (attacker, attacker_url),
    ):
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            monkeypatch.setenv(key, proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        backend_config = SimpleNamespace(
            url=url,
            auth_method="token",
            auth_token="synthetic-secret",
            id=None,
            extra_params={},
        )
        transport = BackendTransport(
            backend_config,
            options=SimpleNamespace(ml_health_timeout=2),
            dispatch_context=_dispatch,
            observe=lambda *_args: None,
        )
        assert await transport.health()
        backend.redirect_url = attacker_url + "/stolen"
        backend.redirect_paths = {"/health"}
        assert not await transport.health()
        assert backend.calls == [("GET", "/health", "Bearer synthetic-secret")] * 2
        assert proxy.calls == [] and attacker.calls == []
