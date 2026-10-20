from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.deactivation_contract import (
    native_deactivation_command,
)
from aap_identity.deactivation_postgres import (
    DeactivationDuePostgresData,
    DeactivationPostgresData,
    DeactivationPostgresTransaction,
)
from deactivation_helpers import (
    deactivation_request,
    due_request,
    native_due_success,
    native_user_success,
    requested_account,
)
from session_helpers import principal


class TransactionConnection:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.events = []
        self.in_transaction = False
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.in_transaction = False

    async def fetchval(self, sql, payload=None):
        assert self.in_transaction
        self.calls.append((sql, payload))
        return json.dumps(self.result)

    async def close(self, timeout):
        assert timeout == 2
        self.closed = True


class PolicyConnection:
    def __init__(self, policy, grants):
        self.policy = policy
        self.grants = grants
        self.closed = False
        self.grant_query = ""

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, _sql):
        return self.policy

    async def fetch(self, sql):
        self.grant_query = sql
        return [{"proname": name, "args": args} for name, args in self.grants]

    async def close(self, timeout):
        assert timeout == 2
        self.closed = True


@pytest.mark.asyncio
async def test_native_user_command_calls_only_lifecycle_capability():
    request = deactivation_request("cancel_self_deactivation")
    command = native_deactivation_command(
        request,
        actor=principal(role="employee"),
        expected_key_hash=None,
        request_fingerprint="a" * 64,
        derivation_id="b" * 64,
    )
    result = native_user_success(command, requested_account())
    connection = TransactionConnection(result)
    async with connection.transaction():
        raw = await DeactivationPostgresTransaction(
            connection
        ).execute_deactivation_command(command.model_dump(mode="json", by_alias=True))
    assert raw == result
    assert connection.calls[0][0] == (
        "SELECT aap_identity_deactivation_v1.execute_deactivation_command($1::jsonb)"
    )
    encoded = json.loads(connection.calls[0][1])
    assert "token" not in encoded


@pytest.mark.asyncio
async def test_due_page_validates_before_operator_transaction_commits(monkeypatch):
    request = due_request(limit=16)
    connection = TransactionConnection(
        native_due_success(
            request,
            processed=2,
            examined=3,
            blocked_last_super_admin=1,
            has_more=True,
        )
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

    reply = await DeactivationDuePostgresData(
        "postgresql://due.invalid/db"
    ).process_due_deactivations(request)
    assert reply.processed == 2 and reply.blocked_last_super_admin == 1
    assert connection.events == ["commit"] and connection.closed
    assert connection.calls[0][0] == (
        "SELECT aap_identity_deactivation_v1.process_due_deactivations($1::jsonb)"
    )
    assert json.loads(connection.calls[0][1]) == {
        "schema": "aap-identity-deactivation-due-native/v1",
        "operation_id": str(request.operation_id),
        "limit": 16,
    }

    invalid = TransactionConnection(
        native_due_success(request, processed=3, examined=2, has_more=False)
    )
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=lambda *args, **kwargs: _return(invalid),
            PostgresError=Exception,
            InterfaceError=Exception,
        ),
    )
    with pytest.raises(IdentityUnavailable):
        await DeactivationDuePostgresData(
            "postgresql://due.invalid/db"
        ).process_due_deactivations(request)
    assert invalid.events == ["rollback"] and invalid.closed


@pytest.mark.asyncio
async def test_user_and_operator_ready_require_exact_function_grants(monkeypatch):
    user = PolicyConnection(
        {
            "schema": "aap-identity-deactivation-policy/v1",
            "policy": "aap-identity-deactivation/v1",
        },
        [("deactivation_policy", ""), ("execute_deactivation_command", "jsonb")],
    )
    due = PolicyConnection(
        {
            "schema": "aap-identity-deactivation-due-policy/v1",
            "policy": "aap-identity-deactivation-due/v1",
        },
        [("due_policy", ""), ("process_due_deactivations", "jsonb")],
    )
    connections = [user, due]

    async def connect(*args, **kwargs):
        return connections.pop(0)

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=connect, PostgresError=Exception, InterfaceError=Exception
        ),
    )
    user_policy = await DeactivationPostgresData(
        "postgresql://user.invalid/db"
    ).deactivation_policy()
    due_policy = await DeactivationDuePostgresData(
        "postgresql://due.invalid/db"
    ).due_policy()
    assert user_policy.policy == "aap-identity-deactivation/v1"
    assert due_policy.policy == "aap-identity-deactivation-due/v1"
    assert "oidvectortypes(p.proargtypes)" in user.grant_query
    assert "oidvectortypes(p.proargtypes)" in due.grant_query
    assert user.closed and due.closed


async def _return(value):
    return value
