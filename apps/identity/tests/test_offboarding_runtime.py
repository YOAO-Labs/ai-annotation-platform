from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.contract import IdentityUnavailable, principal_reply
from aap_identity.offboarding_contract import OffboardingPolicyReply
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from offboarding_helpers import (
    OFFBOARDING_CONTROL,
    OFFBOARDING_SECRET,
    offboarding_request,
)
from session_helpers import principal, fixed_jwt


class BaseData:
    def __init__(self, actor):
        self.actor = actor

    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }

    async def read_jwt_principal(
        self, actor_id, jti, generation, expires_at, for_refresh=False
    ):
        return principal_reply(self.actor)


class FailingTransaction:
    async def prepare_offboarding_command(self, _payload):
        raise IdentityUnavailable()


class OffboardingData:
    def __init__(self, policy=None):
        self.policy = policy or OffboardingPolicyReply(
            schema="aap-identity-offboarding-policy/v1",
            policy="aap-identity-offboarding/v1",
        )

    async def offboarding_policy(self):
        return self.policy

    @asynccontextmanager
    async def transaction(self):
        yield FailingTransaction()


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(OFFBOARDING_CONTROL),
        "signing_secret": SecretStr(OFFBOARDING_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_offboarding_route_accepts_unbounded_stale_version_then_uses_owner():
    now = datetime.now(UTC).replace(microsecond=0)
    expires = now + timedelta(hours=1)
    jti = uuid.uuid4()
    actor = principal(
        role="super_admin",
        credential_id=jti,
        generation=7,
        expires_at=expires.isoformat(),
    )
    token = fixed_jwt(
        actor.actor_id,
        "super_admin",
        int(expires.timestamp()),
        jti,
        7,
        secret=OFFBOARDING_SECRET,
    )
    app = create_app(
        settings(), data=BaseData(actor), offboarding_data=OffboardingData()
    )
    request = offboarding_request(
        "commit_offboarding", token=token, preview_version="\x00" + "v" * 90_000
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/offboarding/execute",
            headers={
                "Authorization": f"Bearer {OFFBOARDING_CONTROL}",
                "Content-Type": "application/json",
            },
            content=request.model_dump_json(by_alias=True).encode(),
        )

    # Owner availability is checked after the uncapped request has been decoded
    # and admitted; a legacy 64 KiB body cap would return 400 instead.
    assert response.status_code == 503
    assert token not in response.text


@pytest.mark.asyncio
async def test_ready_requires_exact_offboarding_policy():
    actor = principal(role="super_admin")
    app = create_app(
        settings(),
        data=BaseData(actor),
        offboarding_data=OffboardingData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503

    app = create_app(
        settings(), data=BaseData(actor), offboarding_data=OffboardingData()
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200


def test_optional_offboarding_dsn_file_is_strict_and_blank_means_unset(tmp_path):
    disabled = settings(offboarding_database_url_file=" ")
    assert disabled.offboarding_database_url is None
    assert disabled.offboarding_database_url_file is None

    path = tmp_path / "offboarding-dsn"
    path.write_text("postgresql://identity.invalid/offboarding\n")
    configured = settings(offboarding_database_url_file=str(path))
    assert configured.offboarding_database_url.get_secret_value().endswith(
        "/offboarding"
    )
    with pytest.raises(
        ValidationError, match="Set at most one offboarding_database_url"
    ):
        settings(
            offboarding_database_url=SecretStr("postgresql://identity.invalid/one"),
            offboarding_database_url_file=str(path),
        )
