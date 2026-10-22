"""Selected Recovery bindings own registration reads; API defaults never replace them."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import httpx
import pytest

from aap_identity.registration_contract import REGISTRATION_STATUS_PROTOCOL
from app.api.v1 import auth
from app.api.v1.recovery_commands import get_recovery_commands
from app.config import settings
from app.deps import get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_recovery_commands import IdentityRecoveryCommands
from app.services.identity_recovery_data import IdentityRecoveryCommandData


def _reply(operation="read_status", enabled=True):
    return {
        "schema": REGISTRATION_STATUS_PROTOCOL,
        "operation": operation,
        "ok": True,
        "data": {"allow_open_registration": enabled},
    }


class UnavailableCaller:
    def __getattr__(self, name):
        raise AssertionError(f"Remote registration read accessed caller {name}")


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment_default", [False, True])
async def test_remote_read_uses_owner_result_and_control_token_without_api_fallback(
    monkeypatch, deployment_default
):
    monkeypatch.setattr(settings, "allow_open_registration", False)
    original = httpx.AsyncClient
    operation = "read_deployment_default" if deployment_default else "read_status"

    def respond(request):
        assert request.url.path == "/v1/identity/recovery/registration-status"
        assert request.headers["authorization"] == "Bearer owner-control"
        assert json.loads(request.content) == {
            "schema": REGISTRATION_STATUS_PROTOCOL,
            "operation": operation,
        }
        return httpx.Response(200, json=_reply(operation))

    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    commands = IdentityRecoveryCommands(
        UnavailableCaller(),
        IdentityGateway(
            identity_url="http://owner.test", identity_token="owner-control"
        ),
    )
    assert (
        await commands.registration_status(deployment_default=deployment_default)
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["unavailable", "invalid_bool", "wrong_operation", "extra"]
)
async def test_remote_read_failure_never_uses_the_api_default_or_caller(
    monkeypatch, fault
):
    monkeypatch.setattr(settings, "allow_open_registration", True)
    original = httpx.AsyncClient
    value = _reply()
    if fault == "invalid_bool":
        value["data"]["allow_open_registration"] = 1
    elif fault == "wrong_operation":
        value["operation"] = "read_deployment_default"
    elif fault == "extra":
        value["fallback"] = True

    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda _req: (
                    httpx.Response(503)
                    if fault == "unavailable"
                    else httpx.Response(200, json=value)
                )
            ),
            **kwargs,
        ),
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityRecoveryCommands(
            UnavailableCaller(),
            IdentityGateway(identity_url="http://owner.test", identity_token="control"),
        ).registration_status()


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment_default", [False, True])
async def test_explicit_missing_recovery_dsn_file_does_not_select_embedded_reader(
    monkeypatch, tmp_path, deployment_default
):
    monkeypatch.setattr(settings, "identity_recovery_database_url", None)
    monkeypatch.setattr(
        settings, "identity_recovery_database_url_file", str(tmp_path / "missing")
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityRecoveryCommands(
            UnavailableCaller(), IdentityGateway()
        ).registration_status(deployment_default=deployment_default)


@pytest.mark.asyncio
async def test_direct_database_binding_uses_its_own_reader_with_the_manager_defaults(
    monkeypatch,
):
    calls = []

    class DedicatedData:
        def __init__(self, dsn):
            assert dsn == "postgresql://owner@localhost/disposable_test"

        async def read_registration_status(self, fallbacks):
            calls.append(fallbacks)
            return _reply(enabled=False)

    monkeypatch.setattr(
        settings,
        "identity_recovery_database_url",
        "postgresql+asyncpg://owner@localhost/disposable_test",
    )
    monkeypatch.setattr(settings, "identity_recovery_database_url_file", None)
    monkeypatch.setattr(settings, "allow_open_registration", True)
    monkeypatch.setattr(settings, "frontend_base_url", "http://api-owner.test")
    monkeypatch.setattr(
        "app.services.identity_recovery_commands.RecoveryPostgresData", DedicatedData
    )
    commands = IdentityRecoveryCommands(UnavailableCaller(), IdentityGateway())
    assert await commands.registration_status() is False
    assert await commands.registration_status(deployment_default=True) is True
    assert calls == [
        {"allow_open_registration": True, "frontend_base_url": "http://api-owner.test"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [False, True])
async def test_caller_read_honors_autoflush_without_committing_or_rolling_back(
    autoflush,
):
    db = SimpleNamespace(
        autoflush=autoflush,
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=_reply()),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    result = await IdentityRecoveryCommandData(db).read_registration_status(
        {"allow_open_registration": False, "frontend_base_url": "http://owner.test"}
    )
    assert result.data.allow_open_registration is True
    assert db.flush.await_count == int(autoflush)
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value,code", [(True, 200), (False, 200), (1, 503)])
async def test_public_status_keeps_its_shape_and_rejects_non_boolean_owner_values(
    value, code
):
    class Commands:
        async def registration_status(self, *, deployment_default=False):
            assert not deployment_default
            return value

    application = FastAPI()
    application.include_router(auth.router, prefix="/auth")
    application.dependency_overrides[get_recovery_commands] = Commands
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://api.test"
    ) as client:
        response = await client.get("/auth/registration-status")
    assert response.status_code == code
    if code == 200:
        assert response.json() == {"open_registration_enabled": value}


@pytest.mark.asyncio
async def test_public_status_ignores_a_conflicting_api_settings_cache(monkeypatch):
    import time
    from app.services import system_settings_service

    monkeypatch.setattr(settings, "allow_open_registration", False)
    monkeypatch.setitem(
        system_settings_service._cache,
        "allow_open_registration",
        (time.monotonic(), False),
    )

    class Owner:
        async def registration_status(self, *, deployment_default=False):
            return True

    application = FastAPI()
    application.include_router(auth.router, prefix="/auth")
    application.dependency_overrides[get_recovery_commands] = Owner
    application.dependency_overrides[get_db] = UnavailableCaller
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://api.test"
    ) as client:
        response = await client.get("/auth/registration-status")
    assert response.status_code == 200
    assert response.json() == {"open_registration_enabled": True}
