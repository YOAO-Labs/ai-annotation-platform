from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.group_contract import native_group_command
from aap_identity.group_postgres import GroupPostgresData, GroupPostgresTransaction
from group_helpers import group_request
from session_helpers import principal


class Connection:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetchval(self, sql, payload):
        self.calls.append((sql, payload))
        return json.dumps(self.result)


class PolicyConnection:
    def __init__(self, grants):
        self.grants = grants
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, _sql):
        return {
            "schema": "aap-identity-groups-policy/v1",
            "policy": "aap-identity-groups/v1",
        }

    async def fetch(self, _sql):
        return [{"proname": name, "args": args} for name, args in self.grants]

    async def close(self, timeout):
        assert timeout == 2
        self.closed = True


@pytest.mark.asyncio
async def test_native_command_uses_only_the_versioned_owner_function():
    request = group_request("list_groups")
    command = native_group_command(
        request,
        actor=principal(role="super_admin"),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    result = {
        "schema": "aap-identity-groups-native/v1",
        "ok": True,
        "operation": "list_groups",
        "operation_id": str(request.operation_id),
        "principal": command.actor.model_dump(mode="json"),
        "data": {"items": []},
    }
    connection = Connection(result)
    returned = await GroupPostgresTransaction(connection).execute_group_command(
        command.model_dump(mode="json", by_alias=True)
    )

    assert returned == result
    assert len(connection.calls) == 1
    assert connection.calls[0][0] == (
        "SELECT aap_identity_groups_v1.execute_group_command($1::jsonb)"
    )
    payload = json.loads(connection.calls[0][1])
    assert payload["schema"] == "aap-identity-groups-native/v1"
    assert "token" not in payload


@pytest.mark.asyncio
async def test_policy_ready_requires_exact_owner_function_grants(monkeypatch):
    connection = PolicyConnection(
        [("execute_group_command", "jsonb"), ("group_policy", "")]
    )
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=lambda *args, **kwargs: _return(connection),
            PostgresError=Exception,
            InterfaceError=Exception,
        ),
    )
    policy = await GroupPostgresData("postgresql://groups.invalid/db").group_policy()
    assert policy.policy == "aap-identity-groups/v1"
    assert connection.closed

    extra = PolicyConnection(
        [
            ("execute_group_command", "jsonb"),
            ("group_policy", ""),
            ("unsafe_helper", "jsonb"),
        ]
    )
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=lambda *args, **kwargs: _return(extra),
            PostgresError=Exception,
            InterfaceError=Exception,
        ),
    )
    with pytest.raises(IdentityUnavailable):
        await GroupPostgresData("postgresql://groups.invalid/db").group_policy()
    assert extra.closed


async def _return(value):
    return value
