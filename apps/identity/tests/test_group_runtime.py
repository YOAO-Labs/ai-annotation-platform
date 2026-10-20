from __future__ import annotations

from pydantic import SecretStr

import pytest
from httpx import ASGITransport, AsyncClient

from aap_identity.group_contract import GroupPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from group_helpers import CONTROL, SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class GroupData:
    def __init__(self, policy=None):
        self.policy = policy or GroupPolicyReply(
            schema="aap-identity-groups-policy/v1",
            policy="aap-identity-groups/v1",
        )

    async def group_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr(SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_group_route_is_unavailable_without_its_explicit_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/groups/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_group_route_requires_control_bearer_and_json_media_type():
    app = create_app(settings(), data=BaseData(), group_data=GroupData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/groups/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        invalid_media = await client.post(
            "/v1/identity/groups/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
    assert unauthorized.status_code == 401
    assert invalid_media.status_code == 400


@pytest.mark.asyncio
async def test_group_route_decodes_untrusted_wire_before_authentication():
    app = create_app(settings(), data=BaseData(), group_data=GroupData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/groups/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_accepts_the_exact_group_policy():
    app = create_app(settings(), data=BaseData(), group_data=GroupData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_requires_exact_group_policy_when_group_data_is_bound():
    wrong = GroupPolicyReply(
        schema="aap-identity-groups-policy/v1", policy="aap-identity-groups/v1"
    ).model_copy(update={"policy": "wrong"})
    app = create_app(settings(), data=BaseData(), group_data=GroupData(wrong))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_group_database_url_supports_optional_environment_file(tmp_path):
    dsn_file = tmp_path / "groups-dsn"
    dsn_file.write_text("postgresql://groups.invalid/identity\n")
    configured = settings(group_database_url_file=str(dsn_file))
    assert configured.group_database_url.get_secret_value() == (
        "postgresql://groups.invalid/identity"
    )
