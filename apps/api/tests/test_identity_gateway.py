from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager

import pytest

from aap_identity.contract import FailureCode, Principal, failure_reply, principal_reply
from app.services.identity_contracts import (
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import IdentityGateway


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

    async def aiter_bytes(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


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

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        assert method == "POST"
        yield await self.post(url, **kwargs)


@pytest.mark.asyncio
async def test_remote_gateway_uses_versioned_control_authenticated_protocol(
    monkeypatch,
):
    principal = _principal()
    client = _Client(_Response(200, principal_reply(principal)))
    monkeypatch.setattr(
        "aap_identity_admission.client.httpx.AsyncClient",
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
        "aap_identity_admission.client.httpx.AsyncClient",
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
        "aap_identity_admission.client.httpx.AsyncClient",
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


@pytest.mark.asyncio
async def test_secret_file_is_bounded_and_configuration_never_falls_back(tmp_path):
    path = tmp_path / "dsn"
    path.write_bytes(b"x" * 4097)
    for kwargs in (
        {"auth_database_url_file": str(path)},
        {
            "auth_database_url_file": str(path),
            "auth_database_url": "postgresql://unused/db",
        },
        {"signing_secret": ""},
    ):
        with pytest.raises(IdentityGatewayUnavailable):
            await IdentityGateway(**kwargs).authenticate("not-a-token")


@pytest.mark.asyncio
async def test_remote_response_budget_is_enforced_during_stream(monkeypatch):
    client = _Client(_Response(200, principal_reply(_principal())))
    monkeypatch.setattr(
        "aap_identity_admission.client.httpx.AsyncClient", lambda **kwargs: client
    )
    monkeypatch.setattr("aap_identity_admission.client.MAX_RESPONSE_BYTES", 128)
    with pytest.raises(IdentityGatewayUnavailable):
        await IdentityGateway(
            identity_url="https://identity.internal", identity_token="control"
        ).authenticate("token")


@pytest.mark.asyncio
async def test_profile_role_changed_after_admission_is_not_restored():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials
    from starlette.requests import Request
    from app.deps import get_current_principal, get_current_user

    value = _principal()
    user = SimpleNamespace(id=value.actor_id, role="employee", is_active=True)

    class Database:
        async def execute(self, statement):
            return SimpleNamespace(scalar_one_or_none=lambda: user)

    class Gateway:
        async def authenticate(self, token, db):
            return value

        async def actor_context(self, principal, db):
            assert principal is value and user.role != principal.role
            raise IdentityCredentialRejected("credential_invalid")

    request = Request({"type": "http"})
    with pytest.raises(HTTPException) as error:
        principal = await get_current_principal(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"),
            Database(),
            Gateway(),
        )
        await get_current_user(request, principal, Database(), Gateway())
    assert error.value.status_code == 401 and user.role == "employee"
    assert not hasattr(request.state, "identity_principal")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, None])
async def test_generation_reply_requires_a_real_integer(value):
    from app.services.identity_session_data import IdentitySessionData

    class Database:
        async def scalar(self, statement, params):
            return value

    data = IdentitySessionData(Database())
    if value is None:
        with pytest.raises(IdentityGatewayUnavailable):
            await data.get_credential_generation(uuid.uuid4())
    else:
        assert await data.get_credential_generation(uuid.uuid4()) == 0
