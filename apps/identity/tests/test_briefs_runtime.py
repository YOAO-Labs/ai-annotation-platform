from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.briefs_contract import decode_native_briefs_command
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from briefs_helpers import (
    BRIEFS_CONTROL,
    BRIEFS_POLICY,
    legacy_fixture,
    native_briefs_success,
)


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class BriefsData:
    def __init__(self, policy=BRIEFS_POLICY):
        self.policy = policy
        self.transactions = 0

    async def briefs_policy(self):
        return self.policy

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield self

    async def execute_briefs_command(self, payload):
        command = decode_native_briefs_command(payload)
        return native_briefs_success(command)


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(BRIEFS_CONTROL),
        "signing_secret": SecretStr("briefs-test-signing-secret-32-bytes-min"),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_route_requires_control_json_and_returns_closed_failures():
    app = create_app(settings(), data=BaseData(), briefs_data=BriefsData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/briefs/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        invalid_media = await client.post(
            "/v1/identity/briefs/execute",
            headers={
                "Authorization": f"Bearer {BRIEFS_CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid_request = await client.post(
            "/v1/identity/briefs/execute",
            headers={
                "Authorization": f"Bearer {BRIEFS_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )

    assert unauthorized.status_code == 401
    assert invalid_media.status_code == 400
    assert invalid_request.status_code == 400
    assert invalid_request.json() == {
        "schema": "aap-identity-briefs/v1",
        "ok": False,
        "error": {"code": "invalid_request"},
    }


@pytest.mark.asyncio
async def test_route_has_no_invented_id_or_body_limit_and_empty_skips_owner_io():
    data = BriefsData()
    app = create_app(settings(), data=BaseData(), briefs_data=data)
    headers = {
        "Authorization": f"Bearer {BRIEFS_CONTROL}",
        "Content-Type": "application/json",
    }
    user_id = uuid.UUID(legacy_fixture()["inputs"][0]["id"])
    ids = [str(user_id)] * 5000
    body = {
        "schema": "aap-identity-briefs/v1",
        "operation": "get_user_briefs",
        "payload": {"user_ids": ids},
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/briefs/execute", headers=headers, json=body
        )
        empty = await client.post(
            "/v1/identity/briefs/execute",
            headers=headers,
            json={
                "schema": "aap-identity-briefs/v1",
                "operation": "get_user_briefs",
                "payload": {"user_ids": []},
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["users"][0]["id"] == str(user_id)
    assert data.transactions == 1
    assert empty.status_code == 200
    assert empty.json()["data"]["users"] == []
    assert data.transactions == 1


@pytest.mark.asyncio
async def test_readiness_checks_the_exact_briefs_policy():
    app = create_app(settings(), data=BaseData(), briefs_data=BriefsData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200

    bad = BriefsData(
        {
            "schema": "aap-identity-briefs-policy/v1",
            "policy": "wrong",
        }
    )
    app = create_app(settings(), data=BaseData(), briefs_data=bad)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_briefs_database_url_supports_file_and_blank_disables_owner(tmp_path):
    dsn_file = tmp_path / "briefs-dsn"
    dsn_file.write_text("postgresql://briefs.invalid/identity\n")
    configured = settings(briefs_database_url_file=str(dsn_file))
    assert configured.briefs_database_url.get_secret_value() == (
        "postgresql://briefs.invalid/identity"
    )
    disabled = settings(briefs_database_url_file=" ")
    assert disabled.briefs_database_url is None
    assert disabled.briefs_database_url_file is None


def test_briefs_database_url_rejects_ambiguous_sources(tmp_path):
    dsn_file = tmp_path / "briefs-dsn"
    dsn_file.write_text("postgresql://briefs.invalid/identity\n")
    with pytest.raises(ValidationError, match="Set at most one briefs_database_url"):
        settings(
            briefs_database_url=SecretStr("postgresql://other.invalid/identity"),
            briefs_database_url_file=str(dsn_file),
        )
