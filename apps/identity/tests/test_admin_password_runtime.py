from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.admin_password_contract import AdminPasswordPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from admin_password_helpers import CONTROL, SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class AdminPasswordData:
    def __init__(self, policy=None):
        self.policy = policy or AdminPasswordPolicyReply(
            schema="aap-identity-admin-passwords-policy/v1",
            policy="aap-identity-admin-passwords/v1",
        )

    async def admin_password_policy(self):
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
async def test_admin_password_route_is_unavailable_without_its_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/admin-passwords/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_admin_password_route_requires_control_json_and_closed_request():
    app = create_app(
        settings(), data=BaseData(), admin_password_data=AdminPasswordData()
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/admin-passwords/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        wrong_media = await client.post(
            "/v1/identity/admin-passwords/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid = await client.post(
            "/v1/identity/admin-passwords/execute",
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
async def test_ready_requires_exact_admin_password_policy():
    app = create_app(
        settings(),
        data=BaseData(),
        admin_password_data=AdminPasswordData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_blank_admin_password_file_disables_only_optional_capability():
    configured = settings(admin_password_database_url_file="  ")
    assert configured.admin_password_database_url is None
    assert configured.admin_password_database_url_file is None


def test_admin_password_dsn_rejects_ambiguous_direct_and_file_sources(tmp_path):
    path = tmp_path / "dsn"
    path.write_text("postgresql://identity.invalid/admin")
    with pytest.raises(ValidationError):
        settings(
            admin_password_database_url=SecretStr(
                "postgresql://identity.invalid/other"
            ),
            admin_password_database_url_file=str(path),
        )
