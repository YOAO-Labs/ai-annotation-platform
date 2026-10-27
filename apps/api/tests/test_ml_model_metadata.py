"""Optional checkpoint observations preserve health and authentication fences."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from sqlalchemy import text

from app.services.ml_backend import MLBackendService
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_ml_backend_auth_rotation import _backend
from tests.test_ml_client_metrics import _transport


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
        id="00000000-0000-0000-0000-000000000099",
        url="https://metadata.test",
        auth_method="token",
        auth_token="secret",
        extra_params={},
        health_meta=None,
    )
    client = _transport(backend)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        return real(*args, **kwargs)

    monkeypatch.setattr(
        "aap_inference.backend_transport.httpx.AsyncClient",
        factory,
    )
    result = await client.model_metadata()
    assert seen[0].headers["Authorization"] == "Bearer secret"
    assert (result is not None) == (
        status == 200 and isinstance(payload.get("models"), list)
    )


def _observation_factory(monkeypatch, *, advertises, model_metadata=None, rotate=None):
    setup_value = {
        **_setup(["polygon", "mask"]),
        "model_metadata_endpoint": advertises,
    }

    class FakeHealthObservation:
        async def health_meta(self, **_kwargs):
            return True, {}

        async def setup(self):
            return setup_value

        async def model_metadata(self):
            if rotate is not None:
                await rotate()
            if model_metadata is not None:
                return await model_metadata()
            return {"schema_version": 1, "models": []}

    def client_factory(_db):
        return lambda _backend_row: FakeHealthObservation()

    monkeypatch.setattr(
        "app.services.inference_backend_control.health_observation_client_factory",
        client_factory,
        raising=True,
    )


@pytest.mark.parametrize("advertises", [False, True])
async def test_health_only_collects_opted_in_metadata(
    db_session, monkeypatch, advertises
):
    backend = await _backend(db_session, False)
    probe = AsyncMock(return_value={"schema_version": 1, "models": []})
    _observation_factory(monkeypatch, advertises=advertises, model_metadata=probe)
    assert await MLBackendService(db_session).check_health(backend.id)
    await db_session.refresh(backend)
    assert ("model_metadata" in backend.health_meta) == advertises
    assert probe.await_count == int(advertises)


async def test_auth_rotation_during_metadata_read_cannot_publish_old_observation(
    db_session, monkeypatch
):
    backend = await _backend(db_session, False)

    async def rotate():
        await db_session.execute(
            text(
                "UPDATE ml_backend_registry SET auth_method='token',auth_token='rotated' WHERE id=:id"
            ),
            {"id": backend.id},
        )

    _observation_factory(monkeypatch, advertises=True, rotate=rotate)
    assert not await MLBackendService(db_session).check_health(backend.id)
    await db_session.refresh(backend)
    assert backend.health_meta is None
