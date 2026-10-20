from __future__ import annotations

import json
import uuid

import pytest

from aap_identity.actor_contract import ActorContext, actor_reply
from aap_identity.contract import IdentityUnavailable
from aap_identity.postgres import PostgresData


class Transaction:
    def __init__(self, connection, readonly):
        self.connection = connection
        self.readonly = readonly

    async def __aenter__(self):
        self.connection.readonly.append(self.readonly)
        return self

    async def __aexit__(self, *_args):
        return False


class Connection:
    def __init__(self, *, actor_reply_value=None, capabilities=True):
        self.actor_reply_value = actor_reply_value
        self.capabilities = capabilities
        self.readonly = []
        self.calls = []
        self.closed = False

    def transaction(self, *, readonly=False):
        return Transaction(self, readonly)

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "read_actor_context" in query:
            return json.dumps(self.actor_reply_value)
        if "bool_and(has_function_privilege" in query:
            self.capability_names = args[0]
            return self.capabilities
        if "authentication_policy()" in query:
            return json.dumps(
                {
                    "schema": "aap-identity-policy/v1",
                    "issuer": "aap-identity/v1",
                    "refresh_grace_seconds": 604800,
                }
            )
        raise AssertionError(f"Unexpected Identity query: {query}")

    async def close(self, *, timeout):
        self.closed = True


@pytest.mark.asyncio
async def test_read_actor_context_uses_one_readonly_function_call_and_closes_connection(
    monkeypatch,
):
    import asyncpg

    actor_id = uuid.uuid4()
    expected = actor_reply(
        ActorContext(
            id=actor_id,
            name="Raw  Name",
            email=" raw@example.test ",
            role="employee",
            is_active=True,
            weekly_target_default=2147483647,
        )
    )
    connection = Connection(actor_reply_value=expected)

    async def connect(*_args, **_kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = PostgresData("postgresql://actor.invalid/identity")

    assert await data.read_actor_context(actor_id, "employee") == expected
    assert connection.readonly == [True]
    assert connection.closed is True
    assert len(connection.calls) == 1
    query, args = connection.calls[0]
    assert query == "SELECT aap_identity_v1.read_actor_context($1,$2)"
    assert args == (actor_id, "employee")


@pytest.mark.asyncio
async def test_authentication_readiness_requires_actor_context_as_fifth_capability(
    monkeypatch,
):
    import asyncpg

    connection = Connection()

    async def connect(*_args, **_kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = PostgresData("postgresql://actor.invalid/identity")

    policy = await data.authentication_policy()
    assert policy["schema"] == "aap-identity-policy/v1"
    assert connection.capability_names == [
        "aap_identity_v1.authentication_policy()",
        "aap_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean)",
        "aap_identity_v1.read_api_key_candidates(text,uuid,integer)",
        "aap_identity_v1.confirm_api_key_principal(uuid,text)",
        "aap_identity_v1.read_actor_context(uuid,text)",
    ]
    assert connection.closed is True

    missing_actor = Connection(capabilities=False)

    async def connect_missing(*_args, **_kwargs):
        return missing_actor

    monkeypatch.setattr(asyncpg, "connect", connect_missing)
    with pytest.raises(IdentityUnavailable):
        await data.authentication_policy()
    assert missing_actor.closed is True
