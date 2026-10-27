import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import httpx
import pytest
from aap_protocol_v2.managed_credential import read_credential
from fastapi import HTTPException

from app.config import settings
from app.services import managed_inference_auth as auth
from app.services.ml_backend import MLBackendService
from tests.test_ml_client_metrics import _transport


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "ml_managed_backend_urls", {"yolo": "http://yolo.test:8003"}
    )
    monkeypatch.setattr(settings, "ml_managed_auth_dir", str(tmp_path / "auth"))
    return tmp_path / "auth" / "yolo"


@pytest.mark.parametrize(
    "url",
    [
        "http://other.test:8003",
        "https://yolo.test:8003",
        "http://yolo.test:8004",
        "http://yolo.test:8003/private",
        "http://user@yolo.test:8003",
        "http://yolo.test:8003?next=1",
        "http://yolo.test:8003#fragment",
    ],
)
async def test_untrusted_url_cannot_read_generate_or_send_credentials(
    store, url, monkeypatch
):
    client = AsyncMock()
    monkeypatch.setattr(auth.httpx, "AsyncClient", client)
    with pytest.raises(HTTPException):
        await auth.initialize_managed_auth(url, None, "managed:yolo")
    assert not store.exists()
    client.assert_not_called()


async def test_handshake_proves_managed_source_without_proxy_or_redirect(
    store, monkeypatch
):
    options = []
    requests = []

    async def handle(request):
        requests.append(request)
        assert request.url.path == "/managed-auth"
        assert request.headers["Authorization"] == f"Bearer {read_credential(store)}"
        return httpx.Response(200, json={"provider": "yolo", "mode": "managed"})

    original = httpx.AsyncClient

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(auth.httpx, "AsyncClient", client)
    token = await auth.initialize_managed_auth(
        "http://YOLO.test:8003/", None, "managed:yolo"
    )
    assert (
        await auth.initialize_managed_auth(
            "http://yolo.test:8003", None, "managed:yolo"
        )
        == token
    )
    assert all(
        option["trust_env"] is False and option["follow_redirects"] is False
        for option in options
    )
    assert len(requests) == 2


@pytest.mark.parametrize(
    "status,body", [(403, {}), (200, {"provider": "yolo", "mode": "manual"}), (302, {})]
)
async def test_failure_keeps_private_file_for_retry_and_never_succeeds(
    store, monkeypatch, status, body
):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        auth.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    status, json=body, headers={"Location": "http://attacker.test"}
                )
            ),
            **kw,
        ),
    )
    with pytest.raises(HTTPException) as error:
        await auth.initialize_managed_auth(
            "http://yolo.test:8003", None, "managed:yolo"
        )
    token = read_credential(store)
    assert token not in str(error.value.detail)
    assert "手动 Token" in error.value.detail
    with pytest.raises(HTTPException):
        await auth.initialize_managed_auth(
            "http://yolo.test:8003", None, "managed:yolo"
        )
    assert read_credential(store) == token


async def test_managed_save_cas_rejects_concurrent_endpoint_change(store, monkeypatch):
    row = SimpleNamespace(
        url="http://yolo.test:8003",
        auth_revision=0,
        auth_method="none",
        auth_token=None,
    )

    async def initialize(*_):
        row.url = "http://third-party.test"
        return "a" * 64

    monkeypatch.setattr(auth, "initialize_managed_auth", initialize)
    db = AsyncMock()
    db.scalar.return_value = row
    svc = MLBackendService(db)
    monkeypatch.setattr(svc, "get", AsyncMock(return_value=row))
    with pytest.raises(HTTPException) as error:
        await svc.update(uuid.uuid4(), auth_method="managed:yolo")
    assert error.value.status_code == 409
    assert row.auth_token is None and row.auth_method == "none"
    db.flush.assert_not_called()


async def test_switching_managed_to_manual_cannot_reuse_local_secret(
    store, monkeypatch
):
    row = SimpleNamespace(
        url="http://yolo.test:8003",
        auth_revision=1,
        auth_method="managed:yolo",
        auth_token="a" * 64,
    )
    db = AsyncMock()
    db.scalar.return_value = row
    svc = MLBackendService(db)
    monkeypatch.setattr(svc, "get", AsyncMock(return_value=row))
    with pytest.raises(HTTPException):
        await svc.update(uuid.uuid4(), auth_method="token")
    assert row.auth_method == "managed:yolo"
    db.flush.assert_not_called()


def test_client_none_stays_public_and_managed_requires_materialized_value(store):
    row = SimpleNamespace(
        id=None,
        url="http://other.test",
        auth_method="none",
        auth_token="a" * 64,
        extra_params={},
        health_meta=None,
    )
    assert "Authorization" not in _transport(row)._headers()
    row.auth_method = "managed:yolo"
    row.auth_token = None
    with pytest.raises(ValueError, match="credentials were not materialized"):
        asyncio.run(_transport(row).health_meta())
    row.auth_token = "a" * 64
    assert _transport(row)._headers()["Authorization"] == f"Bearer {row.auth_token}"
    client = _transport(row)._http_client(timeout=1)
    assert client._trust_env is False and client.follow_redirects is False


async def test_batch_sync_preserves_manual_auth_saved_after_status_snapshot(
    store, monkeypatch
):
    row = SimpleNamespace(
        url="http://yolo.test:8003",
        auth_revision=2,
        auth_method="token",
        auth_token="manual-existing",
    )
    initialize = AsyncMock()
    monkeypatch.setattr(auth, "initialize_managed_auth", initialize)
    db = AsyncMock()
    svc = MLBackendService(db)
    monkeypatch.setattr(svc, "get", AsyncMock(return_value=row))
    with pytest.raises(HTTPException) as error:
        await svc.update(
            uuid.uuid4(), preserve_manual_auth=True, auth_method="managed:yolo"
        )
    assert error.value.detail["error_code"] == "manual_auth_preserved"
    assert row.auth_token == "manual-existing" and row.auth_method == "token"
    initialize.assert_not_called()
    db.flush.assert_not_called()


async def test_new_trusted_registry_defaults_to_managed_but_explicit_none_is_preserved(
    store, monkeypatch
):
    initialize = AsyncMock(return_value="b" * 64)
    monkeypatch.setattr(auth, "initialize_managed_auth", initialize)
    db = AsyncMock()
    db.add = MagicMock()
    svc = MLBackendService(db)
    monkeypatch.setattr(svc, "_create_singleton_pool", AsyncMock())
    created = await svc.create_registry("YOLO", "http://yolo.test:8003")
    assert created.auth_method == "managed:yolo" and created.auth_token == "b" * 64
    initialize.assert_awaited_once_with("http://yolo.test:8003", None, "managed:yolo")
    initialize.reset_mock()
    public = await svc.create_registry(
        "Explicit public", "http://yolo.test:8003", auth_method="none"
    )
    assert public.auth_method == "none" and public.auth_token is None
    initialize.assert_not_called()


def test_default_credentials_are_independent_persistent_and_checkout_local(
    store, monkeypatch
):
    from aap_protocol_v2.backend_auth import BACKEND_SERVICES

    monkeypatch.setattr(
        settings,
        "ml_managed_backend_urls",
        {provider: f"http://{provider}.test:8000" for provider in BACKEND_SERVICES},
    )
    assert set(auth.initialize_default_credentials().values()) == {"ready"}
    tokens = {
        provider: read_credential(auth.credential_directory(provider))
        for provider in BACKEND_SERVICES
    }
    assert len(set(tokens.values())) == 5
    assert set(auth.initialize_default_credentials().values()) == {"ready"}
    assert {
        provider: read_credential(auth.credential_directory(provider))
        for provider in BACKEND_SERVICES
    } == tokens
