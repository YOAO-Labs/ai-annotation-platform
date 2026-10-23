from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.invitation_contract import NativeInvitationPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from invitation_helpers import CONTROL, INVITATION_SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class InvitationData:
    def __init__(self, policy=None):
        self.policy = policy or NativeInvitationPolicyReply(
            schema="aap-identity-invitations-policy/v1",
            policy="aap-identity-invitations/v1",
        )

    async def invitation_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr(INVITATION_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_invitation_route_fails_closed_without_an_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/invitations/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_invitation_route_requires_control_bearer_and_json_media_type():
    app = create_app(settings(), data=BaseData(), invitation_data=InvitationData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/invitations/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        invalid_media = await client.post(
            "/v1/identity/invitations/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid_request = await client.post(
            "/v1/identity/invitations/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthorized.status_code == 401
    assert invalid_media.status_code == 400
    assert invalid_request.status_code == 400


@pytest.mark.asyncio
async def test_ready_requires_exact_invitation_policy_when_bound():
    bad_policy = NativeInvitationPolicyReply(
        schema="aap-identity-invitations-policy/v1",
        policy="aap-identity-invitations/v1",
    ).model_copy(update={"policy": "wrong"})
    app = create_app(
        settings(), data=BaseData(), invitation_data=InvitationData(bad_policy)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_invitation_database_url_supports_optional_environment_file(tmp_path):
    dsn_file = tmp_path / "invitation-dsn"
    dsn_file.write_text("postgresql://invitations.invalid/identity\n")
    configured = settings(invitation_database_url_file=str(dsn_file))
    assert configured.invitation_database_url.get_secret_value() == (
        "postgresql://invitations.invalid/identity"
    )


def test_invitation_policy_fallback_preserves_long_frontend_urls():
    url = "https://frontend.example.test/" + ("path/" * 500)
    configured = settings(invitation_frontend_base_url=url)
    assert configured.invitation_frontend_base_url == url


def test_invitation_dsn_rejects_ambiguous_secret_sources(tmp_path):
    dsn_file = tmp_path / "invitation-dsn"
    dsn_file.write_text("postgresql://invitations.invalid/identity\n")
    with pytest.raises(
        ValidationError, match="Set at most one invitation_database_url"
    ):
        settings(
            invitation_database_url=SecretStr("postgresql://other.invalid/db"),
            invitation_database_url_file=str(dsn_file),
        )
