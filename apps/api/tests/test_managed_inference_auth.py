import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import httpx
import pytest
from aap_inference.topology import BackendTopologyCoordinator
from aap_protocol_v2.managed_credential import read_credential
from fastapi import HTTPException
from sqlalchemy import text

from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.inference_topology_data import InferenceTopologyData
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


@pytest.mark.parametrize("change", ["url", "auth", "auth_restored"])
async def test_managed_save_cas_rejects_concurrent_endpoint_or_auth_change(
    db_session, change
):
    registry_id = uuid.uuid4()
    db_session.add(
        MLBackendRegistry(id=registry_id, name="auth-cas", url="http://yolo.test:8003")
    )
    await db_session.flush()
    data = InferenceTopologyData(db_session)
    snapshot = await data.registry_auth_snapshot(registry_id)
    if change == "url":
        await db_session.execute(
            text("UPDATE ml_backend_registry SET url=:url WHERE id=:id"),
            {"url": "http://other.test", "id": registry_id},
        )
    else:
        await db_session.execute(
            text("UPDATE ml_backend_registry SET auth_method='token', auth_token='manual' WHERE id=:id"),
            {"id": registry_id},
        )
        if change == "auth_restored":
            await db_session.execute(
                text("UPDATE ml_backend_registry SET auth_method='none', auth_token=NULL WHERE id=:id"),
                {"id": registry_id},
            )
    with pytest.raises(HTTPException) as error:
        async with db_session.begin_nested():
            await data.patch_registry_auth_cas(
                registry_id, {"auth_method": "managed:yolo", "auth_token": "a" * 64},
                snapshot,
            )
    assert error.value.status_code == 409
    row = (
        await db_session.execute(
            text("SELECT url, auth_method, auth_token FROM ml_backend_registry WHERE id=:id"),
            {"id": registry_id},
        )
    ).one()
    assert row == (
        "http://other.test" if change == "url" else "http://yolo.test:8003",
        "token" if change == "auth" else "none",
        "manual" if change == "auth" else None,
    )


async def test_switching_managed_to_manual_cannot_reuse_local_secret(
    store, monkeypatch
):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        url="http://yolo.test:8003",
        extra_params=None,
        auth_method="managed:yolo",
    )

    class Data:
        async def registry_facts(self, registry_id):
            return row if registry_id == row.id else None

        async def registry_auth_snapshot(self, registry_id):
            return {"url": row.url, "auth_method": row.auth_method, "auth_revision": 1}

        async def patch_registry_auth_cas(self, *_):
            pytest.fail("rejected update must not change the registry")

    monkeypatch.setattr(
        MLBackendService, "_topology", lambda _: BackendTopologyCoordinator(Data())
    )
    with pytest.raises(HTTPException) as error:
        await MLBackendService(AsyncMock()).update(row.id, auth_method="token")
    assert error.value.status_code == 422
    assert row.auth_method == "managed:yolo"


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


async def test_batch_sync_preserves_manual_auth_saved_after_status_snapshot(
    store, monkeypatch
):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        url="http://yolo.test:8003",
        extra_params=None,
        auth_method="token",
    )

    class Data:
        async def registry_facts(self, registry_id):
            return row if registry_id == row.id else None

        async def registry_auth_snapshot(self, registry_id):
            return {"url": row.url, "auth_method": row.auth_method, "auth_revision": 2}

        async def patch_registry_auth_cas(self, *_):
            pytest.fail("manual credentials must not be overwritten")

    monkeypatch.setattr(
        MLBackendService, "_topology", lambda _: BackendTopologyCoordinator(Data())
    )
    with pytest.raises(HTTPException) as error:
        await MLBackendService(AsyncMock()).update(
            row.id, preserve_manual_auth=True, auth_method="managed:yolo"
        )
    assert error.value.detail["error_code"] == "manual_auth_preserved"
    assert row.auth_method == "token"
    assert not store.exists()


@pytest.mark.parametrize("explicit_none", [False, True])
async def test_new_trusted_registry_defaults_to_managed_but_explicit_none_is_preserved(
    store, monkeypatch, explicit_none
):
    class Data:
        def __init__(self):
            self.rows = {}

        async def registry_url_owner(self, url):
            return None

        async def flush(self):
            pass

        async def create_manual_registry(self, registry_id, values):
            self.rows[registry_id] = dict(values)
            return SimpleNamespace(
                id=registry_id,
                name=values["name"],
                auth_method=values["auth_method"],
                credential_configured=bool(values.get("auth_token")),
                health_meta=None,
            )

        async def singleton_pool(self, _registry_id):
            return None

        async def create_singleton_pool(self, _registry_id, pool_id, *_):
            return SimpleNamespace(id=pool_id)

        async def create_singleton_member(self, *_):
            pass

    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"provider": "yolo", "mode": "managed"})

    monkeypatch.setattr(
        auth.httpx, "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    data = Data()
    monkeypatch.setattr(
        MLBackendService, "_topology", lambda _: BackendTopologyCoordinator(data)
    )
    kwargs = {"auth_method": "none"} if explicit_none else {}
    created = await MLBackendService(AsyncMock()).create_registry(
        "YOLO", "http://yolo.test:8003", **kwargs
    )
    stored = data.rows[created.id]
    if explicit_none:
        assert created.auth_method == "none"
        assert not created.credential_configured
        assert stored.get("auth_token") is None
        assert requests == [] and not store.exists()
    else:
        assert created.auth_method == "managed:yolo"
        assert created.credential_configured
        assert stored["auth_token"] == read_credential(store)
        assert len(requests) == 1
        assert requests[0].headers["Authorization"] == f"Bearer {stored['auth_token']}"


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
