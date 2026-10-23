from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.directory_contract import DirectoryPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from directory_helpers import CONTROL, DIRECTORY_SECRET


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class DirectoryData:
    def __init__(self, policy=None):
        self.policy = policy or DirectoryPolicyReply(
            schema="aap-identity-directory-policy/v1",
            policy="aap-identity-directory/v1",
        )

    async def directory_policy(self):
        return self.policy


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr(DIRECTORY_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_directory_route_is_unavailable_without_its_explicit_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/directory/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_directory_route_requires_control_bearer_json_and_closed_request():
    app = create_app(settings(), data=BaseData(), directory_data=DirectoryData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/directory/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        invalid_media = await client.post(
            "/v1/identity/directory/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid_request = await client.post(
            "/v1/identity/directory/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthorized.status_code == 401
    assert invalid_media.status_code == 400
    assert invalid_request.status_code == 400
    assert invalid_request.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_requires_the_exact_directory_policy_when_bound():
    app = create_app(settings(), data=BaseData(), directory_data=DirectoryData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200

    wrong = DirectoryPolicyReply(
        schema="aap-identity-directory-policy/v1",
        policy="aap-identity-directory/v1",
    ).model_copy(update={"policy": "wrong"})
    app = create_app(settings(), data=BaseData(), directory_data=DirectoryData(wrong))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503


def test_directory_database_url_supports_optional_environment_file(tmp_path):
    dsn_file = tmp_path / "directory-dsn"
    dsn_file.write_text("postgresql://directory.invalid/identity\n")
    configured = settings(directory_database_url_file=str(dsn_file))
    assert configured.directory_database_url.get_secret_value() == (
        "postgresql://directory.invalid/identity"
    )


def test_blank_directory_database_url_file_disables_the_optional_owner():
    configured = settings(directory_database_url_file=" ")
    assert configured.directory_database_url is None
    assert configured.directory_database_url_file is None


def test_directory_database_url_rejects_ambiguous_secret_sources(tmp_path):
    dsn_file = tmp_path / "directory-dsn"
    dsn_file.write_text("postgresql://directory.invalid/identity\n")
    with pytest.raises(ValidationError, match="Set at most one directory_database_url"):
        settings(
            directory_database_url=SecretStr("postgresql://other.invalid/db"),
            directory_database_url_file=str(dsn_file),
        )
