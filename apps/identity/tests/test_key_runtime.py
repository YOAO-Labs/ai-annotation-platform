from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from aap_identity.contract import Principal, principal_reply
from aap_identity.key_contract import KEY_DERIVATION, KEY_PROTOCOL, NATIVE_KEY_PROTOCOL
from aap_identity.security import create_access_token
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

SECRET = "runtime-key-secret"
CONTROL = "private-runtime-control"
ACTOR_ID = uuid.uuid4()


def auth_settings(**changes):
    return Settings(
        database_url="postgresql://auth-unused",
        token=CONTROL,
        signing_secret=SECRET,
        **changes,
    )


def jwt_token():
    return create_access_token(
        str(ACTOR_ID), "employee", secret=SECRET, expire_minutes=5, gen=1
    )


class IdentityData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }

    async def read_jwt_principal(
        self, actor_id, jti, generation, expires_at, for_refresh=False
    ):
        return principal_reply(
            Principal(
                actor_id=actor_id,
                name="Current account",
                email="current@example.test",
                role="employee",
                is_active=True,
                credential_type="jwt",
                credential_id=jti,
                generation=generation,
                scopes=None,
                expires_at=expires_at,
            )
        )


class KeyData:
    def __init__(self, *, policy=None):
        self.policy = policy or {
            "schema": "aap-identity-keys-policy/v1",
            "derivation": KEY_DERIVATION,
        }
        self.commands = []

    async def key_policy(self):
        return self.policy

    async def execute_key_command(self, payload):
        self.commands.append(payload)
        return {
            "schema": NATIVE_KEY_PROTOCOL,
            "ok": True,
            "operation": payload["operation"],
            "operation_id": payload["operation_id"],
            "principal": payload["actor"],
            "data": {"items": []},
        }


def key_request(token=None):
    return {
        "schema": KEY_PROTOCOL,
        "operation": "list_api_keys",
        "operation_id": str(uuid.uuid4()),
        "token": token or jwt_token(),
        "payload": {},
    }


@pytest.mark.asyncio
async def test_read_only_runtime_is_ready_but_key_rpc_is_unavailable_without_key_dsn():
    app = create_app(auth_settings(), data=IdentityData())
    body = key_request()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        reply = await client.post(
            "/v1/identity/api-keys/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert reply.status_code == 503 and body["token"] not in reply.text


@pytest.mark.asyncio
async def test_key_rpc_uses_private_control_auth_closed_body_and_native_command():
    data = KeyData()
    app = create_app(
        auth_settings(key_database_url="postgresql://keys-unused"),
        data=IdentityData(),
        key_data=data,
    )
    body = key_request()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        assert (await client.get("/ready")).status_code == 200
        denied = await client.post(
            "/v1/identity/api-keys/execute",
            headers={"Authorization": "Bearer wrong"},
            json=body,
        )
        invalid = await client.post(
            "/v1/identity/api-keys/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json={**body, "unknown": "not accepted"},
        )
        good = await client.post(
            "/v1/identity/api-keys/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=body,
        )
    assert denied.status_code == 401
    assert invalid.status_code == 400 and body["token"] not in invalid.text
    assert good.status_code == 200
    assert good.json()["schema"] == KEY_PROTOCOL and good.json()["ok"] is True
    assert good.json()["operation_id"] == body["operation_id"]
    assert good.json()["principal"]["actor_id"] == str(ACTOR_ID)
    assert body["token"] not in good.text
    assert len(data.commands) == 1
    assert "token" not in data.commands[0]


@pytest.mark.asyncio
async def test_key_readiness_checks_policy_when_dsn_is_configured():
    app = create_app(
        auth_settings(key_database_url="postgresql://keys-unused"),
        data=IdentityData(),
        key_data=KeyData(policy={"schema": "wrong"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
async def test_key_rpc_bounds_body_and_enforces_the_same_request_deadline():
    data = KeyData()
    app = create_app(
        auth_settings(key_database_url="postgresql://keys-unused"),
        data=IdentityData(),
        key_data=data,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        too_large = await client.post(
            "/v1/identity/api-keys/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b" " * 65537,
        )
    assert too_large.status_code == 400 and "token" not in too_large.text

    stopped = asyncio.Event()

    class SlowKeyData(KeyData):
        async def execute_key_command(self, payload):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    slow = SlowKeyData()
    app = create_app(
        auth_settings(
            key_database_url="postgresql://keys-unused",
            request_timeout_seconds=0.01,
        ),
        data=IdentityData(),
        key_data=slow,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://identity"
    ) as client:
        timed_out = await client.post(
            "/v1/identity/api-keys/execute",
            headers={"Authorization": f"Bearer {CONTROL}"},
            json=key_request(),
        )
    assert timed_out.status_code == 503 and stopped.is_set()


def test_key_database_url_is_optional_exclusive_and_uses_bounded_secret_files(tmp_path):
    assert auth_settings().key_database_url is None

    key_dsn = tmp_path / "keys-dsn"
    key_dsn.write_text("postgresql://keys-from-file\n")
    configured = auth_settings(key_database_url_file=str(key_dsn))
    assert (
        configured.key_database_url.get_secret_value() == "postgresql://keys-from-file"
    )

    with pytest.raises(ValueError, match="at most one key_database_url"):
        auth_settings(
            key_database_url="postgresql://direct",
            key_database_url_file=str(key_dsn),
        )
    key_dsn.write_text(" ")
    with pytest.raises(ValueError, match="Empty key_database_url"):
        auth_settings(key_database_url_file=str(key_dsn))
    key_dsn.write_text("x" * 4097)
    with pytest.raises(ValueError, match="Invalid key_database_url file"):
        auth_settings(key_database_url_file=str(key_dsn))
