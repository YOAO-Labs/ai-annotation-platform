from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.deletion_contract import DeletionPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from deletion_helpers import DELETION_CONTROL, DELETION_SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class DeletionData:
    def __init__(self, policy=None):
        self.policy = policy or DeletionPolicyReply(
            schema="aap-identity-deletions-policy/v1",
            policy="aap-identity-deletions/v1",
        )

    async def deletion_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(DELETION_CONTROL),
        "signing_secret": SecretStr(DELETION_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_deletion_route_is_unavailable_without_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_deletion_route_requires_control_bearer_json_and_closed_request():
    app = create_app(settings(), data=BaseData(), deletion_data=DeletionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthenticated = await client.post(
            "/v1/identity/deletions/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        wrong_media = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthenticated.status_code == 401
    assert wrong_media.status_code == 400
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_requires_exact_deletion_owner_policy():
    app = create_app(
        settings(),
        data=BaseData(),
        deletion_data=DeletionData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503

    app = create_app(settings(), data=BaseData(), deletion_data=DeletionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optional_deletion_dsn_file_disables_or_selects_only_this_owner(tmp_path):
    disabled = settings(deletion_database_url_file=" ")
    assert disabled.deletion_database_url is None
    assert disabled.deletion_database_url_file is None

    path = tmp_path / "deletion-dsn"
    path.write_text("postgresql://identity.invalid/deletions\n")
    configured = settings(deletion_database_url_file=str(path))
    assert configured.deletion_database_url.get_secret_value().endswith("/deletions")
    with pytest.raises(ValidationError, match="Set at most one deletion_database_url"):
        settings(
            deletion_database_url=SecretStr("postgresql://other.invalid/deletions"),
            deletion_database_url_file=str(path),
        )
