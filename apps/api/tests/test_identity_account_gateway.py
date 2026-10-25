"""Account transport binds owner replies without loading identity tables."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from aap_identity.account_contract import AccountFailure, decode_account_request
from aap_identity.contract import Principal
from app.api.v1.account_commands import _reject_account_error, execute_account
from app.config import settings
from app.services.identity_account_commands import (
    IdentityAccountCommands,
    IdentityPresenceCommands,
)
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway


def _principal(generation=2**63 - 1):
    return Principal(
        actor_id=uuid.uuid4(),
        name="Account fixture",
        email="account@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=generation,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        scopes=None,
    )


def _command(operation="get_preferences", payload=None):
    return decode_account_request(
        {
            "schema": "aap-identity-accounts/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": "credential-fixture",
            "audit_context": {
                "method": "GET",
                "path": "/api/v1/auth/me/preferences",
                "ip": "127.0.0.1",
                "request_id": None,
            },
            "payload": payload or {},
        }
    )


def _reply(command, principal):
    return {
        "schema": "aap-identity-accounts/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": {
            "account": None,
            "preferences": {"ui": {"theme": "dark"}},
            "changed": None,
            "previous_avatar_ref": None,
        },
    }


def _remote(monkeypatch, respond, principal):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_account_commands.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    return IdentityAccountCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=principal,
    )


async def test_remote_preferences_preserve_exact_generation_without_native_fallback(
    monkeypatch,
):
    principal, command = _principal(), _command()

    def respond(request):
        assert request.url.path == "/v1/identity/accounts/execute"
        assert request.headers["authorization"] == "Bearer control"
        assert json.loads(request.content)["token"] == "credential-fixture"
        return httpx.Response(200, json=_reply(command, principal))

    result = await _remote(monkeypatch, respond, principal).execute(command)
    assert result.principal.generation == 2**63 - 1
    assert result.data.preferences["ui"]["theme"] == "dark"


@pytest.mark.parametrize(
    "fault",
    [
        "actor",
        "credential",
        "operation",
        "operation_id",
        "boolean_generation",
        "float_generation",
        "extra_secret",
    ],
)
async def test_remote_account_reply_rejects_wrong_binding_and_numeric_alias(
    monkeypatch, fault
):
    principal, command = _principal(1), _command()
    body = _reply(command, principal)
    if fault == "actor":
        body["principal"]["actor_id"] = str(uuid.uuid4())
    elif fault == "credential":
        body["principal"]["credential_id"] = str(uuid.uuid4())
    elif fault == "operation":
        body["operation"] = "heartbeat"
    elif fault == "operation_id":
        body["operation_id"] = str(uuid.uuid4())
    elif fault == "boolean_generation":
        body["principal"]["generation"] = True
    elif fault == "float_generation":
        body["principal"]["generation"] = 1.0
    else:
        body["data"]["password_hash"] = "must-not-escape"
    gateway = _remote(
        monkeypatch, lambda _request: httpx.Response(200, json=body), principal
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(command)


async def test_remote_capability_absence_does_not_open_local_data(monkeypatch):
    principal, command = _principal(), _command()

    def forbidden(*args, **kwargs):
        raise AssertionError("Remote failure must not select a local owner")

    monkeypatch.setattr(
        "app.services.identity_account_commands.IdentityAccountCommandData", forbidden
    )
    gateway = _remote(monkeypatch, lambda _request: httpx.Response(503), principal)
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(command)


async def test_explicit_account_dsn_file_failure_has_no_caller_fallback(monkeypatch):
    monkeypatch.setattr(settings, "identity_account_database_url", "")
    monkeypatch.setattr(
        settings, "identity_account_database_url_file", "/not-an-account-dsn-fixture"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit binding failure must not select caller SQL")

    monkeypatch.setattr(
        "app.services.identity_account_commands.IdentityAccountCommandData", forbidden
    )
    gateway = IdentityAccountCommands(
        object(), IdentityGateway(signing_secret="fixture-only")
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(_command())


async def test_preferences_above_admission_reply_limit_still_round_trip(monkeypatch):
    principal, command = _principal(), _command()
    body = _reply(command, principal)
    value = "x" * (16 * 1024 * 1024 + 1)
    body["data"]["preferences"] = {
        "ai": {"params_by_backend": {"fixture": {"future": value}}}
    }
    gateway = _remote(
        monkeypatch, lambda _request: httpx.Response(200, json=body), principal
    )
    result = await gateway.execute(command)
    assert (
        result.data.preferences["ai"]["params_by_backend"]["fixture"]["future"] == value
    )


async def test_remote_request_does_not_convert_nonfinite_preference_to_null(
    monkeypatch,
):
    principal = _principal()
    command = _command(
        "patch_preferences",
        {"patch": {"ai": {"params_by_backend": {"fixture": {"bad": 0}}}}},
    )
    command.payload.patch["ai"]["params_by_backend"]["fixture"]["bad"] = float("inf")
    calls = []
    gateway = _remote(monkeypatch, lambda _request: calls.append(True), principal)
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.execute(command)
    assert calls == []


@pytest.mark.parametrize("affected", [True, -1, 2**63])
async def test_presence_rejects_invalid_owner_count(monkeypatch, affected):
    original = httpx.AsyncClient

    def respond(request):
        assert request.url.path == "/v1/identity/presence/mark-inactive"
        return httpx.Response(
            200, json={"schema": "aap-identity-presence/v1", "affected": affected}
        )

    monkeypatch.setattr(
        "app.services.identity_account_commands.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    gateway = IdentityPresenceCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
    )
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.mark_inactive_accounts_offline(datetime.now(UTC))


def test_account_error_keeps_public_status_and_safe_policy_detail():
    with pytest.raises(HTTPException) as caught:
        _reject_account_error(
            AccountFailure(
                "policy_rejected",
                status_code=409,
                detail={
                    "code": "named_presets_conflict",
                    "message": "命名布局预设已在其他会话更新，请刷新后重试",
                    "currentRevision": "0",
                },
            )
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "named_presets_conflict",
        "message": "命名布局预设已在其他会话更新，请刷新后重试",
        "currentRevision": "0",
    }


async def test_http_composer_cannot_replace_the_admitted_actor():
    principal, unrelated = _principal(), _principal()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/me/preferences",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "state": {"identity_principal": principal},
        }
    )

    class Commands:
        async def execute(self, command):
            from aap_identity.account_contract import decode_account_reply

            return decode_account_reply(command, _reply(command, unrelated))

    with pytest.raises(HTTPException) as caught:
        await execute_account(
            request,
            SimpleNamespace(credentials="fixture"),
            Commands(),
            "get_preferences",
            {},
        )
    assert caught.value.status_code == 503
    assert request.state.identity_principal == principal
