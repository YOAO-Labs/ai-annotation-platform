from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from aap_identity.contract import FailureCode, Principal, failure_reply, principal_reply
from app.services.identity_gateway import (
    IdentityCredentialRejected,
    IdentityGateway,
    IdentityGatewayUnavailable,
)


def _principal() -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        name="Identity Gateway Test",
        email="identity-gateway@test.local",
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode()


class _Client:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.call: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url: str, *, json: dict, headers: dict):
        self.call = {"url": url, "json": json, "headers": headers}
        return self.response


@pytest.mark.asyncio
async def test_remote_gateway_uses_versioned_control_authenticated_protocol(
    monkeypatch,
):
    principal = _principal()
    client = _Client(_Response(200, principal_reply(principal)))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    gateway = IdentityGateway(
        identity_url="https://identity.internal/",
        identity_token="control-secret",
    )

    result = await gateway.authenticate("opaque-user-token")

    assert result == principal
    assert client.call == {
        "url": "https://identity.internal/v1/identity/authenticate",
        "json": {"token": "opaque-user-token"},
        "headers": {"Authorization": "Bearer control-secret"},
    }


@pytest.mark.asyncio
async def test_remote_credential_failure_never_falls_back_to_embedded(monkeypatch):
    code: FailureCode = "credential_revoked"
    client = _Client(_Response(200, failure_reply(code)))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    gateway = IdentityGateway(
        identity_url="https://identity.internal", identity_token="control"
    )

    async def forbidden_fallback(_token, *, db=None):
        raise AssertionError("remote mode must not fall back to local credentials")

    monkeypatch.setattr(gateway, "_authenticate_embedded", forbidden_fallback)
    with pytest.raises(IdentityCredentialRejected) as error:
        await gateway.authenticate("revoked-token")
    assert error.value.code == code


@pytest.mark.asyncio
async def test_remote_outage_and_bad_control_credentials_fail_closed(monkeypatch):
    client = _Client(_Response(503, {"error": "unavailable"}))
    monkeypatch.setattr(
        "app.services.identity_gateway.httpx.AsyncClient",
        lambda **_kwargs: client,
    )
    gateway = IdentityGateway(
        identity_url="https://identity.internal", identity_token="bad"
    )

    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.authenticate("user-token")

    client.response = _Response(401, {"error": "invalid control bearer"})
    with pytest.raises(IdentityGatewayUnavailable):
        await gateway.authenticate("user-token")


def test_embedded_default_uses_caller_transaction_when_available():
    assert IdentityGateway().uses_caller_session(object())
    assert not IdentityGateway().uses_caller_session(None)
    assert not IdentityGateway(
        auth_database_url="postgresql://identity/db"
    ).uses_caller_session(object())
    assert not IdentityGateway(identity_url="http://identity").uses_caller_session(
        object()
    )
