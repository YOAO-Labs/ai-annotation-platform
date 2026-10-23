from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.key_postgres import KeyPostgresData


class Connection:
    def __init__(self, *, allowed=True, result=None, wrong_operation=False):
        self.allowed = allowed
        self.result = result or {"schema": "aap-identity-keys-policy/v1"}
        self.wrong_operation = wrong_operation
        self.calls = []
        self.closed = False
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args, self.in_transaction))
        if "has_function_privilege" in sql:
            return self.allowed
        if "key_policy" in sql:
            return json.dumps(self.result)
        if "execute_key_command" in sql:
            command = json.loads(args[0])
            return json.dumps(
                {
                    "schema": "aap-identity-keys-native/v1",
                    "ok": True,
                    "operation": "create_api_key"
                    if self.wrong_operation
                    else command["operation"],
                    "operation_id": command["operation_id"],
                    "principal": command["actor"],
                    "data": {"items": []},
                }
            )
        raise AssertionError(sql)

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_key_readiness_requires_exact_two_function_grants_and_closes_connection(
    monkeypatch,
):
    connection = Connection(
        result={
            "schema": "aap-identity-keys-policy/v1",
            "derivation": "aap-identity-api-keys/v1",
        }
    )
    connect_calls = []

    async def connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    policy = await KeyPostgresData("postgresql://keys").key_policy()
    assert policy["derivation"] == "aap-identity-api-keys/v1"
    assert connection.calls[1][1][0] == [
        "aap_identity_keys_v1.key_policy()",
        "aap_identity_keys_v1.execute_key_command(jsonb)",
    ]
    assert connect_calls[0][1]["server_settings"] == {
        "application_name": "aap-identity-key-commands"
    }
    assert connection.closed and not connection.in_transaction
    assert connection.committed and not connection.rolled_back


@pytest.mark.asyncio
async def test_key_readiness_fails_closed_when_a_function_grant_is_missing(monkeypatch):
    connection = Connection(allowed=False)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await KeyPostgresData("postgresql://keys").key_policy()
    assert connection.closed
    assert connection.rolled_back and not connection.committed


@pytest.mark.asyncio
async def test_execute_uses_one_fresh_bounded_transaction_and_jsonb_function(
    monkeypatch,
):
    connections = []

    async def connect(*args, **kwargs):
        connection = Connection()
        connections.append((connection, args, kwargs))
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    data = KeyPostgresData("postgresql://keys", connect_timeout=2, command_timeout=4)
    actor = Principal(
        actor_id=uuid.uuid4(),
        name="Current account",
        email="current@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=1,
        scopes=None,
        expires_at=datetime(2026, 10, 20, 1, tzinfo=UTC),
    )
    operation_id = uuid.uuid4()
    command = {
        "schema": "aap-identity-keys-native/v1",
        "operation": "list_api_keys",
        "operation_id": str(operation_id),
        "actor": actor.model_dump(mode="json"),
        "expected_key_hash": None,
        "payload": {},
        "new_key": None,
    }
    result = await data.execute_key_command(command)
    connection, args, kwargs = connections[0]
    sql, params, in_transaction = connection.calls[0]
    assert result["ok"] is True
    assert sql == "SELECT aap_identity_keys_v1.execute_key_command($1::jsonb)"
    assert json.loads(params[0]) == command and in_transaction is True
    assert kwargs["timeout"] == 2 and kwargs["command_timeout"] == 4
    assert connection.closed and not connection.in_transaction
    assert connection.committed and not connection.rolled_back


@pytest.mark.asyncio
async def test_malformed_native_reply_rolls_back_before_private_transaction_commit(
    monkeypatch,
):
    connection = Connection(wrong_operation=True)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    actor = Principal(
        actor_id=uuid.uuid4(),
        name="Current account",
        email="current@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=1,
        scopes=None,
        expires_at=datetime(2026, 10, 20, 1, tzinfo=UTC),
    )
    command = {
        "schema": "aap-identity-keys-native/v1",
        "operation": "list_api_keys",
        "operation_id": str(uuid.uuid4()),
        "actor": actor.model_dump(mode="json"),
        "expected_key_hash": None,
        "payload": {},
        "new_key": None,
    }
    with pytest.raises(IdentityUnavailable):
        await KeyPostgresData("postgresql://keys").execute_key_command(command)
    assert connection.closed and connection.rolled_back
    assert not connection.committed
