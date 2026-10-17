"""Input and error guards for ML probes; tests make no database/network calls."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import HTTPException

from app.api.v1 import admin_ml_integrations, ml_backends
from app.api.v1.admin_ml_integrations import ProbeRequest, SmokeTestRequest


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000",
        "http://172.17.0.1:8001/path",
        "https://ml.internal.example:9443",
        "http://[::1]:8001",
    ],
)
def test_probe_base_url_allows_private_and_internal_hosts(url):
    admin_ml_integrations._validate_probe_base_url(url)
    assert ProbeRequest(url=url).url == url
    assert SmokeTestRequest(url=url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://backend.internal",
        "http:///missing-host",
        "http://host:",
        "http://host:0",
        "http://host:65536",
        "http://host:not-a-port",
        "http://user:secret@backend.internal",
        "http://backend.internal?",
        "http://backend.internal#",
        "http://backend.internal?token=secret",
        "http://backend.internal#secret",
        "http://backend.internal\n/path",
        "http://backend.internal\t/path",
        "http://backend internal",
    ],
)
def test_probe_base_url_rejects_malformed_or_sensitive_parts(url):
    with pytest.raises(HTTPException) as exc:
        admin_ml_integrations._validate_probe_base_url(url)
    assert exc.value.status_code == 422
    assert "secret" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_probe_rejects_userinfo_before_constructing_http_client(monkeypatch):
    class NoNetworkClient:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("invalid URLs must be rejected before creating an HTTP client")

    monkeypatch.setattr(admin_ml_integrations.httpx, "AsyncClient", NoNetworkClient)
    with pytest.raises(HTTPException) as exc:
        await admin_ml_integrations.probe_backend(
            ProbeRequest(url="http://user:secret@backend.internal"), _admin=object()
        )
    assert exc.value.status_code == 422
    assert "secret" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_smoke_test_rejects_query_before_database_access(monkeypatch):
    class NoDatabase:
        async def execute(self, *_args, **_kwargs):
            pytest.fail("invalid URLs must be rejected before database access")

    class NoNetworkClient:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("invalid URLs must be rejected before creating an HTTP client")

    monkeypatch.setattr(admin_ml_integrations.httpx, "AsyncClient", NoNetworkClient)
    with pytest.raises(HTTPException) as exc:
        await admin_ml_integrations.smoke_test_backend(
            SmokeTestRequest(url="http://backend.internal?token=secret"),
            request=None,
            db=NoDatabase(),
            shadow_session_factory=None,
            dispatch_context_factory=None,
            admin=object(),
        )
    assert exc.value.status_code == 422
    assert "secret" not in str(exc.value.detail)


@pytest.mark.asyncio
async def test_probe_and_smoke_errors_do_not_echo_urls_or_credentials(monkeypatch):
    requested_url = "http://user:secret@backend.internal/health"

    class FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            raise httpx.ConnectError(
                f"failed to connect to {requested_url}",
                request=httpx.Request("GET", requested_url),
            )

    monkeypatch.setattr(admin_ml_integrations.httpx, "AsyncClient", FailingClient)
    probe = await admin_ml_integrations.probe_backend(
        ProbeRequest(url="http://backend.internal"), _admin=object()
    )
    assert probe.error == "连接失败"
    assert "secret" not in probe.error
    assert "backend.internal" not in probe.error

    class QueryResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Database:
        async def execute(self, *_args, **_kwargs):
            return QueryResult()

        async def commit(self):
            return None

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin_ml_integrations.AuditService, "log", no_audit)
    smoke = await admin_ml_integrations.smoke_test_backend(
        SmokeTestRequest(url="http://backend.internal"),
        request=None,
        db=Database(),
        shadow_session_factory=None,
        dispatch_context_factory=None,
        admin=object(),
    )
    assert smoke.error == "连接失败"
    assert "secret" not in smoke.error
    assert "backend.internal" not in smoke.error


@pytest.mark.asyncio
async def test_warmup_checks_project_enablement_before_any_backend_call(monkeypatch):
    project_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    calls = []

    class Service:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get(self, requested_backend_id):
            calls.append(("get", requested_backend_id))
            return object()

        async def is_enabled(self, requested_project_id, requested_backend_id):
            calls.append(("is_enabled", requested_project_id, requested_backend_id))
            return False

        async def warmup(self, *_args, **_kwargs):
            calls.append(("warmup",))
            pytest.fail("disabled backends must not receive warmup requests")

    monkeypatch.setattr(ml_backends, "MLBackendService", Service)
    with pytest.raises(HTTPException) as exc:
        await ml_backends.warmup_ml_backend(
            project_id=project_id,
            backend_id=backend_id,
            request=None,
            body={"task": "detection"},
            db=object(),
            shadow_session_factory=None,
            dispatch_context_factory=None,
            current_user=object(),
        )
    assert exc.value.status_code == 404
    assert exc.value.detail == "ML Backend not found"
    assert calls == [
        ("get", backend_id),
        ("is_enabled", project_id, backend_id),
    ]
