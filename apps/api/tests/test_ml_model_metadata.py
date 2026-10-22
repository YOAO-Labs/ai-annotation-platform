"""Optional checkpoint observations preserve health and authentication fences."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import text
from app.services.ml_client import MLBackendClient
from app.services.ml_backend import MLBackendService
from tests.test_ml_backend_auth_rotation import _backend


@pytest.mark.parametrize(
    "status,payload",
    [
        (200, {"schema_version": 1, "models": []}),
        (404, {}),
        (200, {"models": "broken"}),
    ],
)
async def test_metadata_transport_is_authenticated_and_optional(
    monkeypatch, status, payload
):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, json=payload)

    backend = SimpleNamespace(
        url="https://metadata.test",
        auth_method="token",
        auth_token="secret",
        extra_params={},
    )
    client = MLBackendClient(backend)
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        client,
        "_http_client",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = await client.model_metadata()
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert (result is not None) == (
        status == 200 and isinstance(payload.get("models"), list)
    )


@pytest.mark.parametrize("advertises", [False, True])
async def test_health_only_collects_opted_in_metadata(
    db_session, monkeypatch, advertises
):
    backend = await _backend(db_session, False)
    monkeypatch.setattr(
        MLBackendClient, "health_meta", AsyncMock(return_value=(True, {}))
    )
    monkeypatch.setattr(
        MLBackendClient,
        "setup",
        AsyncMock(return_value={"model_metadata_endpoint": advertises, "models": []}),
    )
    probe = AsyncMock(return_value={"schema_version": 1, "models": []})
    monkeypatch.setattr(MLBackendClient, "model_metadata", probe)
    assert await MLBackendService(db_session).check_health(backend.id)
    await db_session.refresh(backend)
    assert ("model_metadata" in backend.health_meta) == advertises
    assert probe.await_count == int(advertises)


async def test_auth_rotation_during_metadata_read_cannot_publish_old_observation(
    db_session, monkeypatch
):
    backend = await _backend(db_session, False)
    monkeypatch.setattr(
        MLBackendClient, "health_meta", AsyncMock(return_value=(True, {}))
    )
    monkeypatch.setattr(
        MLBackendClient,
        "setup",
        AsyncMock(return_value={"model_metadata_endpoint": True, "models": []}),
    )

    async def rotate(_client):
        await db_session.execute(
            text(
                "UPDATE ml_backend_registry SET auth_method='token',auth_token='rotated' WHERE id=:id"
            ),
            {"id": backend.id},
        )
        return {"schema_version": 1, "models": []}

    monkeypatch.setattr(MLBackendClient, "model_metadata", rotate)
    assert not await MLBackendService(db_session).check_health(backend.id)
    await db_session.refresh(backend)
    assert backend.health_meta is None
