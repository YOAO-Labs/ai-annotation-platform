from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.suspension_contract import SuspensionPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from suspension_helpers import SUSPENSION_CONTROL, SUSPENSION_SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class SuspensionData:
    def __init__(self, policy=None):
        self.policy = policy or SuspensionPolicyReply(
            schema="aap-identity-suspensions-policy/v1",
            policy="aap-identity-suspensions/v1",
        )

    async def suspension_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(SUSPENSION_CONTROL),
        "signing_secret": SecretStr(SUSPENSION_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_suspension_route_is_unavailable_without_its_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/suspensions/execute",
            headers={
                "Authorization": f"Bearer {SUSPENSION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_suspension_route_requires_control_bearer_json_and_closed_request():
    app = create_app(settings(), data=BaseData(), suspension_data=SuspensionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthenticated = await client.post(
            "/v1/identity/suspensions/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        wrong_media = await client.post(
            "/v1/identity/suspensions/execute",
            headers={
                "Authorization": f"Bearer {SUSPENSION_CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid = await client.post(
            "/v1/identity/suspensions/execute",
            headers={
                "Authorization": f"Bearer {SUSPENSION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthenticated.status_code == 401
    assert wrong_media.status_code == 400
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_requires_exact_suspension_owner_policy():
    app = create_app(
        settings(),
        data=BaseData(),
        suspension_data=SuspensionData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503

    app = create_app(settings(), data=BaseData(), suspension_data=SuspensionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optional_suspension_dsn_file_disables_or_selects_only_this_owner(tmp_path):
    disabled = settings(suspension_database_url_file=" ")
    assert disabled.suspension_database_url is None
    assert disabled.suspension_database_url_file is None

    path = tmp_path / "suspension-dsn"
    path.write_text("postgresql://identity.invalid/suspensions\n")
    configured = settings(suspension_database_url_file=str(path))
    assert configured.suspension_database_url.get_secret_value().endswith(
        "/suspensions"
    )
    with pytest.raises(
        ValidationError, match="Set at most one suspension_database_url"
    ):
        settings(
            suspension_database_url=SecretStr("postgresql://other.invalid/suspensions"),
            suspension_database_url_file=str(path),
        )
