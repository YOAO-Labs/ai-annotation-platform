from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.platform_role_contract import PlatformRolePolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from platform_role_helpers import CONTROL, PLATFORM_ROLE_SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class PlatformRoleData:
    def __init__(self, policy=None):
        self.policy = policy or PlatformRolePolicyReply(
            schema="aap-identity-platform-roles-policy/v1",
            policy="aap-identity-platform-roles/v1",
        )

    async def platform_role_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr(PLATFORM_ROLE_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_platform_role_route_is_unavailable_without_its_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/platform-roles/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_platform_role_route_requires_control_bearer_json_and_closed_request():
    app = create_app(settings(), data=BaseData(), platform_role_data=PlatformRoleData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/platform-roles/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        wrong_media = await client.post(
            "/v1/identity/platform-roles/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid = await client.post(
            "/v1/identity/platform-roles/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthorized.status_code == 401
    assert wrong_media.status_code == 400
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_requires_the_exact_platform_role_policy():
    app = create_app(
        settings(),
        data=BaseData(),
        platform_role_data=PlatformRoleData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_blank_platform_role_dsn_file_disables_only_the_optional_capability():
    configured = settings(platform_role_database_url_file=" ")
    assert configured.platform_role_database_url is None
    assert configured.platform_role_database_url_file is None


def test_platform_role_dsn_file_and_direct_value_cannot_be_combined(tmp_path):
    path = tmp_path / "platform-role-dsn"
    path.write_text("postgresql://roles.invalid/identity")
    with pytest.raises(
        ValidationError, match="Set at most one platform_role_database_url"
    ):
        settings(
            platform_role_database_url=SecretStr("postgresql://other.invalid/identity"),
            platform_role_database_url_file=str(path),
        )
