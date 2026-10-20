from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from aap_identity.actor_contract import (
    ACTOR_PROTOCOL,
    ActorContext,
    actor_failure_reply,
    actor_reply,
)
from aap_identity.contract import IdentityUnavailable
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings

CONTROL = "actor-context-control-token"


class Data:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }

    async def read_actor_context(self, actor_id, admitted_role):
        self.calls.append((actor_id, admitted_role))
        if self.error is not None:
            raise self.error
        return self.reply


def _settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr("actor-context-test-signing-secret-32-bytes"),
    }
    values.update(changes)
    return Settings(**values)


def _request(actor_id: uuid.UUID, role="employee"):
    return {
        "schema": ACTOR_PROTOCOL,
        "actor_id": str(actor_id),
        "admitted_role": role,
    }


@pytest.mark.asyncio
async def test_actor_context_route_requires_control_and_json_and_uses_auth_data():
    actor_id = uuid.uuid4()
    actor = ActorContext(
        id=actor_id,
        name="Fresh",
        email="fresh@example.test",
        role="employee",
        is_active=True,
        weekly_target_default=200,
    )
    data = Data(actor_reply(actor))
    app = create_app(_settings(), data=data)
    headers = {
        "Authorization": f"Bearer {CONTROL}",
        "Content-Type": "application/json",
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthorized = await client.post(
            "/v1/identity/actor-context",
            headers={"Content-Type": "application/json"},
            json=_request(actor_id),
        )
        invalid_media = await client.post(
            "/v1/identity/actor-context",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"{}",
        )
        response = await client.post(
            "/v1/identity/actor-context", headers=headers, json=_request(actor_id)
        )

    assert unauthorized.status_code == 401
    assert invalid_media.status_code == 400
    assert invalid_media.json() == actor_failure_reply("invalid_request")
    assert response.status_code == 200
    assert response.json() == actor_reply(actor)
    assert data.calls == [(actor_id, "employee")]


@pytest.mark.asyncio
async def test_actor_context_route_returns_only_bounded_typed_failures():
    actor_id = uuid.uuid4()
    headers = {
        "Authorization": f"Bearer {CONTROL}",
        "Content-Type": "application/json",
    }
    app = create_app(_settings(), data=Data(actor_failure_reply("credential_invalid")))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        rejected = await client.post(
            "/v1/identity/actor-context", headers=headers, json=_request(actor_id)
        )
        malformed = await client.post(
            "/v1/identity/actor-context", headers=headers, content=b"{}"
        )
        oversized = await client.post(
            "/v1/identity/actor-context", headers=headers, content=b" " * 65537
        )
        deeply_nested = await client.post(
            "/v1/identity/actor-context",
            headers=headers,
            content=b"[" * 2000 + b"0" + b"]" * 2000,
        )

    assert rejected.status_code == 200
    assert rejected.json() == actor_failure_reply("credential_invalid")
    assert malformed.status_code == 400
    assert malformed.json() == actor_failure_reply("invalid_request")
    assert oversized.status_code == 400
    assert oversized.json() == actor_failure_reply("invalid_request")
    assert deeply_nested.status_code == 400
    assert deeply_nested.json() == actor_failure_reply("invalid_request")


@pytest.mark.asyncio
async def test_unavailable_or_misbinding_actor_owner_is_503():
    actor_id = uuid.uuid4()
    headers = {
        "Authorization": f"Bearer {CONTROL}",
        "Content-Type": "application/json",
    }
    bad_context = ActorContext(
        id=uuid.uuid4(),
        name="Other",
        email="other@example.test",
        role="employee",
        is_active=True,
        weekly_target_default=None,
    )
    for data in (
        Data(error=IdentityUnavailable()),
        Data(reply=actor_reply(bad_context)),
    ):
        app = create_app(_settings(), data=data)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://identity.test"
        ) as client:
            response = await client.post(
                "/v1/identity/actor-context",
                headers=headers,
                json=_request(actor_id),
            )
        assert response.status_code == 503
        assert response.json() == {"detail": "Identity actor context unavailable"}
