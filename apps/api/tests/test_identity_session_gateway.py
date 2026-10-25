"""Session transport stays value-only and commits validated failed-login audits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from types import SimpleNamespace

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.session_contract import SessionFailure, decode_session_request
from app.api.v1.auth import _execute_session, _session_error
from app.config import settings
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_session_commands import IdentitySessionCommands


def _login():
    return decode_session_request(
        {
            "schema": "aap-identity-sessions/v1",
            "operation": "login",
            "operation_id": str(uuid.uuid4()),
            "token": None,
            "payload": {
                "email": "session@example.test",
                "password": "SecretFixture123",
                "captcha_verified": True,
                "client_ip": "127.0.0.1",
            },
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/auth/login",
                "ip": "127.0.0.1",
                "request_id": None,
            },
            "user_agent": "Session fixture",
        }
    )


@pytest.mark.asyncio
async def test_remote_login_failure_preserves_count_without_local_table_calls(
    monkeypatch,
):
    calls = []

    def respond(request):
        assert request.url.path == "/v1/identity/sessions/execute"
        assert request.headers["authorization"] == "Bearer control"
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "schema": "aap-identity-sessions/v1",
                "ok": False,
                "error": {"code": "invalid_credentials", "failed_count": 3},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.identity_session_commands.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    gateway = IdentitySessionCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
    )
    with pytest.raises(SessionFailure) as error:
        await gateway.execute(_login())
    assert error.value.code == "invalid_credentials" and error.value.failed_count == 3
    assert calls == ["/v1/identity/sessions/execute"]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_embedded_failure_commits_only_validated_business_events(
    monkeypatch, malformed
):
    class Database:
        commits = 0

        async def commit(self):
            self.commits += 1

    class Manager:
        def __init__(self, *_args, **_kwargs):
            pass

        async def execute(self, _request):
            if malformed:
                raise IdentityUnavailable()
            raise SessionFailure("invalid_credentials", failed_count=1)

    db = Database()
    monkeypatch.setattr(settings, "identity_session_database_url", "")
    monkeypatch.setattr(settings, "identity_session_database_url_file", "")
    monkeypatch.setattr(
        "app.services.identity_session_commands.SessionManager", Manager
    )
    gateway = IdentitySessionCommands(
        db, IdentityGateway(signing_secret="fixture-only")
    )
    with pytest.raises(IdentityGatewayUnavailable if malformed else SessionFailure):
        await gateway.execute(_login())
    assert db.commits == (0 if malformed else 1)


@pytest.mark.asyncio
async def test_session_reply_cannot_replace_the_admitted_actor():
    principal = Principal(
        actor_id=uuid.uuid4(),
        name="Session owner",
        email="session@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/logout", "headers": []}
    )
    request.state.identity_principal = principal

    class Owner:
        async def execute(self, _command):
            return SimpleNamespace(
                principal=principal.model_copy(update={"actor_id": uuid.uuid4()})
            )

    with pytest.raises(HTTPException) as error:
        await _execute_session(request, Owner(), "logout", {}, token="fixture-bearer")
    assert error.value.status_code == 503
    assert request.state.identity_principal is principal


@pytest.mark.asyncio
async def test_invalid_session_request_does_not_echo_credentials_or_call_owner():
    request = Request(
        {"type": "http", "method": "POST", "path": "/api/v1/auth/logout", "headers": []}
    )

    class Owner:
        async def execute(self, _command):
            pytest.fail("Invalid request reached session owner")

    token = "private-fixture-" * 2000
    with pytest.raises(HTTPException) as error:
        await _execute_session(request, Owner(), "logout", {}, token=token)
    assert error.value.status_code == 422
    assert "private-fixture" not in str(error.value.detail)


@pytest.mark.parametrize(
    ("operation", "code", "status", "detail"),
    [
        ("logout", "account_busy", 409, "账号正在更新，请刷新后重试"),
        ("refresh", "account_busy", 409, "账号正在更新，请刷新后重试"),
        ("refresh", "credential_busy", 503, "身份认证服务暂不可用"),
        ("refresh", "invalid_token", 401, "invalid_token"),
        ("refresh", "malformed_token", 401, "malformed_token"),
        ("refresh", "credential_invalid", 401, "user_inactive"),
        ("refresh", "credential_revoked", 401, "token_revoked"),
        ("refresh", "token_expired", 401, "grace_expired"),
        ("login", "user_inactive", 403, "账户已被停用"),
    ],
)
def test_session_error_mapping_preserves_public_semantics(
    operation, code, status, detail
):
    with pytest.raises(HTTPException) as error:
        _session_error(operation, SessionFailure(code))
    assert error.value.status_code == status and error.value.detail == detail
    if operation == "refresh":
        assert (error.value.headers or {}).get("WWW-Authenticate") == (
            "Bearer" if code == "invalid_token" else None
        )
