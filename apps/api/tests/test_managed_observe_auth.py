"""Exercise discovery and unregistered smoke-test transport without a DB or GPU."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from yoao_inference.backend_control_manager import BackendControlManager
from yoao_protocol_v2.managed_credential import ensure_credential

from app.api.v1 import admin_ml_integrations as routes
from app.config import settings
from app.services.inference_backend_control import BackendControlService
from app.services.managed_inference_auth import local_managed_headers


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ml_managed_auth_dir", str(tmp_path / "auth"))
    monkeypatch.setattr(
        settings, "ml_managed_backend_urls", {"yolo": "http://yolo.test:8003"}
    )
    secret = ensure_credential(tmp_path / "auth" / "yolo")
    calls = []
    options = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"ok": True, "loaded": False})
        if request.url.path.endswith("/setup"):
            if request.headers.get("Authorization") != f"Bearer {secret}":
                return httpx.Response(401)
            return httpx.Response(
                200,
                json={
                    "supported_variants": [
                        {"key": "size", "variants": [{"value": "n"}]}
                    ]
                },
            )
        if request.url.path == "/reload":
            return httpx.Response(200, json={"reloaded": True})
        if request.url.path == "/unload":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"Unexpected request: {request.url}")

    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(routes.httpx, "AsyncClient", client)
    db = AsyncMock()
    rows = []

    async def catalog(_self):
        return rows

    monkeypatch.setattr(routes.InferenceTopologyData, "registry_catalog", catalog)

    @asynccontextmanager
    async def dispatch(*_args):
        yield

    opener = SimpleNamespace(open=AsyncMock(), readiness=AsyncMock(return_value=True))
    stats = SimpleNamespace(
        snapshots=AsyncMock(), readiness=AsyncMock(return_value=True)
    )
    manager = BackendControlManager(
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
        SimpleNamespace(ml_health_timeout=1, ml_predict_timeout=1),
        request_context_factory=lambda *_: None,
        dispatch_context_factory=lambda *_: dispatch(),
        unregistered_smoke_dispatch=dispatch,
        stats_data=stats,
    )
    monkeypatch.setattr(
        routes,
        "backend_control_service",
        lambda session: BackendControlService(session, request_only_client=manager),
    )
    return db, rows, calls, options, secret


async def test_observe_authenticates_only_the_fixed_unregistered_target(
    transport, monkeypatch
):
    db, _, calls, options, secret = transport
    monkeypatch.setattr(
        settings,
        "ml_backend_observe_urls",
        ["http://yolo.test:8003", "http://external.test:8003"],
    )
    result = await routes.observe_backends(db=db, _admin=None)
    assert result.targets[0].supports_variants
    assert not result.targets[1].supports_variants
    for request in calls:
        expected = (
            f"Bearer {secret}"
            if request.url.host == "yolo.test" and request.url.path == "/setup"
            else None
        )
        assert request.headers.get("Authorization") == expected
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False
    assert secret not in result.model_dump_json()


async def test_observe_preserves_explicit_none_even_for_a_same_origin_alias(
    transport, monkeypatch
):
    db, rows, calls, _, _ = transport
    rows.append(
        SimpleNamespace(
            url="http://YOLO.test:8003/",
            auth_method="none",
            auth_token=None,
            source="manual",
        )
    )
    monkeypatch.setattr(settings, "ml_backend_observe_urls", ["http://yolo.test:8003"])
    result = await routes.observe_backends(db=db, _admin=None)
    assert result.targets[0].registered
    assert not result.targets[0].supports_variants
    assert all("Authorization" not in request.headers for request in calls)


async def test_observe_never_equates_unrecognized_origins(transport, monkeypatch):
    db, rows, calls, _, _ = transport
    rows.append(
        SimpleNamespace(
            url="http://other.test:9000/private",
            auth_method="token",
            auth_token="manual-test-token-for-another-service",
            source="manual",
        )
    )
    monkeypatch.setattr(
        settings, "ml_backend_observe_urls", ["http://external.test:8003/api"]
    )
    result = await routes.observe_backends(db=db, _admin=None)
    assert len(calls) == 2
    assert all("Authorization" not in request.headers for request in calls)
    assert not result.targets[0].registered


async def test_ambiguous_same_origin_registrations_never_send_credentials(
    transport, monkeypatch
):
    db, rows, calls, _, _ = transport
    rows.extend(
        [
            SimpleNamespace(url="http://YOLO.test:8003/", source="manual"),
            SimpleNamespace(url="http://yolo.test:8003", source="env"),
        ]
    )
    monkeypatch.setattr(settings, "ml_backend_observe_urls", ["http://yolo.test:8003"])
    with pytest.raises(routes.HTTPException) as error:
        await routes.observe_backends(db=db, _admin=None)
    assert error.value.status_code == 409
    assert calls == []


async def test_unregistered_smoke_test_keeps_auth_on_its_fixed_target(
    transport, monkeypatch
):
    db, _, calls, options, secret = transport
    monkeypatch.setattr(routes.AuditService, "log", AsyncMock())
    result = await routes.smoke_test_backend(
        routes.SmokeTestRequest(url="http://yolo.test:8003"),
        request=SimpleNamespace(),
        db=db,
        admin=SimpleNamespace(),
    )
    assert result.ok and result.auto_unloaded
    assert [request.url.path for request in calls] == ["/health", "/reload", "/unload"]
    assert all(
        request.headers.get("Authorization") == f"Bearer {secret}" for request in calls
    )
    assert options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False


def test_read_only_defaults_never_initialize_or_enroll_other_targets(
    tmp_path, monkeypatch
):
    directory = tmp_path / "auth"
    monkeypatch.setattr(settings, "ml_managed_auth_dir", str(directory))
    monkeypatch.setattr(
        settings, "ml_managed_backend_urls", {"yolo": "http://yolo.test:8003"}
    )
    assert local_managed_headers("http://yolo.test:8003") == {}
    assert not directory.exists()
    ensure_credential(directory / "yolo")
    for url in (
        "http://external.test:8003",
        "http://yolo.test:8003/private",
        "http://yolo.test:8003?redirect=1",
    ):
        assert local_managed_headers(url) == {}
    assert not (directory / "sam3").exists()
