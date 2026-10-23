from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import bcrypt
import httpx
import pytest

from aap_identity.contract import Principal
from aap_identity.security import ISSUER
from aap_identity.session_contract import (
    NATIVE_SESSION_PROTOCOL,
    SessionClaims,
)
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

from session_helpers import ACTOR_ID, CONTROL, SECRET


def settings(**changes):
    return Settings(
        database_url="postgresql://auth-unused",
        token=CONTROL,
        signing_secret=SECRET,
        **changes,
    )


def login_body(*, password="Password!2026", captcha_verified=True):
    return {
        "schema": "aap-identity-sessions/v1",
        "operation": "login",
        "operation_id": str(uuid.uuid4()),
        "token": None,
        "payload": {
            "email": "person@example.test",
            "password": password,
            "captcha_verified": captcha_verified,
            "client_ip": "192.0.2.10",
        },
        "audit_context": {
            "method": "POST",
            "path": "/api/v1/auth/login",
            "ip": "192.0.2.10",
            "request_id": str(uuid.uuid4()),
        },
        "user_agent": "pytest",
    }


class IdentityData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class Counter:
    def __init__(self, value=0):
        self.value = value
        self.calls = []

    async def get_count(self, ip):
        self.calls.append("get")
        return self.value

    async def increment(self, ip):
        self.calls.append("increment")
        self.value += 1
        return self.value

    async def reset(self, ip):
        self.calls.append("reset")
        self.value = 0


class SessionData:
    def __init__(self, *, candidate=None, failure=None, policy=None):
        self.candidate = candidate
        self.failure = failure
        self.policy = policy or {
            "schema": "aap-identity-sessions-policy/v1",
            "issuer": ISSUER,
            "algorithm": "HS256",
            "refresh_grace_seconds": 604800,
        }
        self.calls = []
        self.command = None

    async def session_policy(self):
        self.calls.append("policy")
        return self.policy

    async def read_login_candidate(self, email):
        self.calls.append(("candidate", email))
        return {
            "schema": "aap-identity-login-candidate/v1",
            "item": self.candidate,
        }

    async def execute_session_command(self, command):
        self.calls.append("execute")
        self.command = command
        if self.failure is not None:
            return {
                "schema": NATIVE_SESSION_PROTOCOL,
                "ok": False,
                "error": {
                    "code": self.failure,
                    "failed_count": command["login"]["failed_count"]
                    if self.failure == "invalid_credentials"
                    else None,
                },
            }
        login = command["login"]
        claims = SessionClaims(
            sub=uuid.UUID(login["actor_id"]),
            role="employee",
            exp=int(datetime.now(UTC).timestamp()) + 86400,
            jti=uuid.UUID(command["operation_id"]),
            gen=0,
            iss=ISSUER,
        )
        actor = Principal(
            actor_id=claims.sub,
            name="Current account",
            email=login["email"],
            role=claims.role,
            is_active=True,
            credential_type="jwt",
            credential_id=claims.jti,
            generation=claims.gen,
            scopes=None,
            expires_at=claims.expires_at,
        )
        return {
            "schema": NATIVE_SESSION_PROTOCOL,
            "ok": True,
            "operation": "login",
            "operation_id": str(command["operation_id"]),
            "principal": actor.model_dump(mode="json"),
            "data": {"claims": claims.model_dump(mode="json")},
        }


@pytest.mark.asyncio
async def test_auth_and_key_only_runtime_stays_ready_and_session_rpc_is_disabled():
    app = create_app(settings(), data=IdentityData())
    body = login_body()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        ready = await client.get("/ready")
        unavailable = await client.post(
            "/v1/identity/sessions/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert ready.status_code == 200
    assert (
        unavailable.status_code == 503
        and body["payload"]["password"] not in unavailable.text
    )


@pytest.mark.asyncio
async def test_session_rpc_reads_policy_and_returns_closed_failure_without_secrets():
    data = SessionData(failure="invalid_credentials")
    counter = Counter()
    app = create_app(
        settings(
            session_database_url="postgresql://sessions-unused",
            login_redis_url="redis://counter-unused",
        ),
        data=IdentityData(),
        session_data=data,
        login_counter=counter,
    )
    body = login_body(password="hidden-password-value")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        ready = await client.get("/ready")
        unauthorized = await client.post(
            "/v1/identity/sessions/execute",
            headers={"Authorization": "Bearer wrong"},
            json=body,
        )
        failure = await client.post(
            "/v1/identity/sessions/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert ready.status_code == 200 and data.calls[0] == "policy"
    assert unauthorized.status_code == 401
    assert failure.status_code == 200
    assert failure.json() == {
        "schema": "aap-identity-sessions/v1",
        "ok": False,
        "error": {"code": "invalid_credentials", "failed_count": 1},
    }
    assert body["payload"]["password"] not in failure.text
    assert counter.calls == ["get", "increment"]
    assert data.command is not None
    assert "password" not in data.command["login"]


@pytest.mark.asyncio
async def test_session_rpc_returns_success_token_from_native_claims():
    password = "Password!2026"
    data = SessionData(
        candidate={
            "actor_id": str(ACTOR_ID),
            "password_hash": bcrypt.hashpw(
                password.encode(), bcrypt.gensalt(rounds=4)
            ).decode(),
        }
    )
    counter = Counter()
    app = create_app(
        settings(
            session_database_url="postgresql://sessions-unused",
            login_redis_url="redis://counter-unused",
        ),
        data=IdentityData(),
        session_data=data,
        login_counter=counter,
    )
    body = login_body(password=password)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        reply = await client.post(
            "/v1/identity/sessions/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert reply.status_code == 200
    assert reply.json()["schema"] == "aap-identity-sessions/v1"
    assert reply.json()["data"]["claims"]["jti"] == body["operation_id"]
    assert password not in reply.text
    assert counter.calls == ["get", "reset"]


@pytest.mark.asyncio
async def test_session_rpc_bounds_body_and_deadline():
    data = SessionData(failure="invalid_credentials")
    app = create_app(
        settings(
            session_database_url="postgresql://sessions-unused",
            login_redis_url="redis://counter-unused",
        ),
        data=IdentityData(),
        session_data=data,
        login_counter=Counter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        oversized = await client.post(
            "/v1/identity/sessions/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b" " * 65537,
        )
    assert oversized.status_code == 400 and "password" not in oversized.text

    stopped = asyncio.Event()

    class SlowSessionData(SessionData):
        async def execute_session_command(self, command):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    slow = SlowSessionData()
    app = create_app(
        settings(
            session_database_url="postgresql://sessions-unused",
            login_redis_url="redis://counter-unused",
            request_timeout_seconds=0.01,
        ),
        data=IdentityData(),
        session_data=slow,
        login_counter=Counter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        timed_out = await client.post(
            "/v1/identity/sessions/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=login_body(),
        )
    assert timed_out.status_code == 503 and stopped.is_set()


def test_session_settings_optional_pairs_files_and_redis_requirement(tmp_path):
    base = {
        "database_url": "postgresql://auth-unused",
        "token": CONTROL,
        "signing_secret": SECRET,
    }
    assert Settings(**base).session_database_url is None

    with pytest.raises(ValueError, match="requires login_redis_url"):
        Settings(**base, session_database_url="postgresql://sessions")

    session_file = tmp_path / "sessions-dsn"
    redis_file = tmp_path / "redis-dsn"
    session_file.write_text("postgresql://sessions\n")
    redis_file.write_text("redis://counter\n")
    configured = Settings(
        **base,
        session_database_url_file=str(session_file),
        login_redis_url_file=str(redis_file),
    )
    assert configured.session_database_url.get_secret_value() == "postgresql://sessions"
    assert configured.login_redis_url.get_secret_value() == "redis://counter"
    with pytest.raises(ValueError, match="at most one session_database_url"):
        Settings(
            **base,
            session_database_url="postgresql://direct",
            session_database_url_file=str(session_file),
            login_redis_url="redis://counter",
        )
    session_file.write_text("x" * 4097)
    with pytest.raises(ValueError, match="Invalid session_database_url file"):
        Settings(
            **base,
            session_database_url_file=str(session_file),
            login_redis_url="redis://counter",
        )
