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
    def __init__(self, policy, grants, *, table_access=False, column_access=False):
        self.policy = policy
        self.grants = grants
        self.table_access = table_access
        self.column_access = column_access
        self.closed = False
        self.queries = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, sql):
        self.queries.append(sql)
        if "has_table_privilege" in sql:
            return self.table_access or self.column_access
        return self.policy

    async def fetch(self, sql):
        self.queries.append(sql)
        return [
            {
                "nspname": namespace,
                "proname": name,
                "args": args,
                "prosecdef": security_definer,
            }
            for namespace, name, args, security_definer, kind, return_type in self.grants
            if namespace not in {"pg_catalog", "information_schema"}
            and not namespace.startswith(("pg_toast", "pg_temp_"))
            and kind in {"f", "p", "a", "w"}
            and return_type not in {"trigger", "event_trigger"}
            and (namespace.startswith("aap_") or security_definer)
        ]

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
        [
            (
                "aap_identity_deactivation_v1",
                "deactivation_policy",
                "",
                False,
                "f",
                "jsonb",
            ),
            (
                "aap_identity_deactivation_v1",
                "execute_deactivation_command",
                "jsonb",
                True,
                "f",
                "jsonb",
            ),
            ("public", "ordinary_helper", "text", False, "f", "void"),
            ("aap_ignored", "trigger_function", "", False, "f", "trigger"),
            ("aap_ignored", "event_trigger_function", "", False, "f", "event_trigger"),
            ("pg_catalog", "pg_notify", "text", True, "f", "void"),
        ],
    )
    due = PolicyConnection(
        {
            "schema": "aap-identity-deactivation-due-policy/v1",
            "policy": "aap-identity-deactivation-due/v1",
        },
        [
            (
                "aap_identity_deactivation_v1",
                "due_policy",
                "",
                False,
                "f",
                "jsonb",
            ),
            (
                "aap_identity_deactivation_v1",
                "process_due_deactivations",
                "jsonb",
                True,
                "f",
                "jsonb",
            ),
        ],
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
    user_capability_sql = next(sql for sql in user.queries if "pg_proc" in sql)
    due_capability_sql = next(sql for sql in due.queries if "pg_proc" in sql)
    assert "oidvectortypes(function.proargtypes)" in user_capability_sql
    assert "prokind IN ('f', 'p', 'a', 'w')" in user_capability_sql
    assert "return_type.typname IN ('trigger', 'event_trigger')" in user_capability_sql
    assert "namespace.nspname NOT LIKE 'pg_temp_%'" in user_capability_sql
    assert "OR function.prosecdef" in user_capability_sql
    assert "oidvectortypes(function.proargtypes)" in due_capability_sql
    table_sql = next(sql for sql in due.queries if "has_table_privilege" in sql)
    assert "has_any_column_privilege" in table_sql
    assert user.closed and due.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_grant",
    [
        ("aap_notifications_v1", "unexpected", "uuid", True, "f", "jsonb"),
        ("public", "unsafe_definer", "uuid", True, "f", "jsonb"),
    ],
)
async def test_readiness_rejects_extra_domain_and_security_definer_capabilities(
    monkeypatch, extra_grant
):
    connection = PolicyConnection(
        {
            "schema": "aap-identity-deactivation-policy/v1",
            "policy": "aap-identity-deactivation/v1",
        },
        [
            (
                "aap_identity_deactivation_v1",
                "deactivation_policy",
                "",
                False,
                "f",
                "jsonb",
            ),
            (
                "aap_identity_deactivation_v1",
                "execute_deactivation_command",
                "jsonb",
                True,
                "f",
                "jsonb",
            ),
            extra_grant,
        ],
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
    with pytest.raises(IdentityUnavailable):
        await DeactivationPostgresData(
            "postgresql://user.invalid/db"
        ).deactivation_policy()
    assert connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("table_access,column_access", [(True, False), (False, True)])
async def test_readiness_rejects_table_and_column_privileges(
    monkeypatch, table_access, column_access
):
    connection = PolicyConnection(
        {
            "schema": "aap-identity-deactivation-due-policy/v1",
            "policy": "aap-identity-deactivation-due/v1",
        },
        [
            (
                "aap_identity_deactivation_v1",
                "due_policy",
                "",
                False,
                "f",
                "jsonb",
            ),
            (
                "aap_identity_deactivation_v1",
                "process_due_deactivations",
                "jsonb",
                True,
                "f",
                "jsonb",
            ),
        ],
        table_access=table_access,
        column_access=column_access,
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
    with pytest.raises(IdentityUnavailable):
        await DeactivationDuePostgresData(
            "postgresql://operator.invalid/db"
        ).due_policy()
    assert connection.closed


async def _return(value):
    return value
