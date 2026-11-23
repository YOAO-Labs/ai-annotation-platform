from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import uuid

import pytest

from yoao_identity_admission.contract import (
    IdentityFailure,
    IdentityUnavailable,
    Principal,
)
from app.services.identity_contracts import (
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import IdentityGateway


def _principal() -> Principal:
    return Principal(
        actor_id=uuid.uuid4(),
        name="Remote admission actor",
        email="admission@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=7,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_gateway_authentication_delegates_to_shared_client(monkeypatch):
    principal = _principal()
    observed = {}

    class Client:
        def __init__(self, *, origin, service_token, timeout_seconds):
            observed.update(
                origin=origin,
                service_token=service_token,
                timeout_seconds=timeout_seconds,
            )

        async def authenticate(self, token):
            observed["token"] = token
            return principal

    monkeypatch.setattr("app.services.identity_gateway.RemoteAdmissionClient", Client)
    gateway = IdentityGateway(
        identity_url="https://identity.internal/",
        identity_token="scoped-control",
        timeout_seconds=1.75,
    )

    assert await gateway.authenticate("user-credential") == principal
    assert observed == {
        "origin": "https://identity.internal",
        "service_token": "scoped-control",
        "timeout_seconds": 1.75,
        "token": "user-credential",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected",
    [
        (IdentityFailure("credential_revoked"), IdentityCredentialRejected),
        (IdentityFailure("invalid_request"), IdentityGatewayUnavailable),
        (IdentityUnavailable(), IdentityGatewayUnavailable),
    ],
)
async def test_gateway_preserves_remote_failure_mapping(monkeypatch, failure, expected):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def authenticate(self, _token):
            raise failure

    monkeypatch.setattr("app.services.identity_gateway.RemoteAdmissionClient", Client)
    gateway = IdentityGateway(
        identity_url="https://identity.internal", identity_token="scoped-control"
    )

    with pytest.raises(expected) as error:
        await gateway.authenticate("user-credential")
    if isinstance(failure, IdentityFailure) and expected is IdentityCredentialRejected:
        assert error.value.code == "credential_revoked"


@pytest.mark.asyncio
async def test_embedded_authentication_path_is_not_replaced_by_remote_client(
    monkeypatch,
):
    principal = _principal()
    gateway = IdentityGateway(signing_secret="fixture-signing-secret")
    calls = []

    async def embedded(token, *, db=None):
        calls.append((token, db))
        return principal

    monkeypatch.setattr(gateway, "_authenticate_embedded", embedded)
    assert (
        await gateway.authenticate("embedded-user-token", db="caller-session")
        == principal
    )
    assert calls == [("embedded-user-token", "caller-session")]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_key", [False, True])
async def test_socket_credentials_use_short_embedded_session(monkeypatch, is_key):
    principal = _principal()
    if is_key:
        principal = principal.model_copy(
            update={"credential_type": "api_key", "generation": None, "scopes": ["*"]}
        )
    gateway = IdentityGateway()
    events = []

    class Session:
        async def commit(self):
            events.append("commit")

    @asynccontextmanager
    async def session_factory():
        events.append("open")
        try:
            yield Session()
        finally:
            events.append("close")

    async def embedded(token, *, db):
        assert token == "socket-credential" and isinstance(db, Session)
        events.append("authenticate")
        return principal

    monkeypatch.setattr("app.db.base.async_session", session_factory)
    monkeypatch.setattr(gateway, "_authenticate_embedded", embedded)
    assert await gateway.authenticate("socket-credential") == principal
    assert events == [
        "open",
        "authenticate",
        *(["commit"] if is_key else []),
        "close",
    ]
