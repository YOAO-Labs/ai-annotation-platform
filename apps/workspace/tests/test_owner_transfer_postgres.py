from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from types import ModuleType
import uuid

import asyncpg
import pytest

from aap_workspace.owner_transfer import OwnerTransferManager
from aap_workspace.owner_transfer_contract import (
    OWNER_TRANSFER_PROTOCOL,
    OwnerTransferFailure,
    OwnerTransferRequest,
    OwnerTransferResult,
    OwnerTransferSuccess,
    OwnerTransferUnavailable,
)
from aap_workspace.owner_transfer_postgres import (
    _ACCOUNT_LOCK_HINT,
    _OWNER_TRANSFER_FUNCTIONS,
    _OWNER_TRANSFER_SCHEMA,
    _PROJECT_LOCK_HINT,
    OwnerTransferPostgresData,
)

_REQUEST = OwnerTransferRequest(
    schema=OWNER_TRANSFER_PROTOCOL,
    operation_id=uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984"),
    actor_id=uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a"),
    project_id=uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586"),
    new_owner_id=uuid.UUID("b6cdd33b-8bca-4a4c-bd0b-846dc8ca6be5"),
)


def _success():
    return OwnerTransferSuccess(
        schema=OWNER_TRANSFER_PROTOCOL,
        ok=True,
        operation_id=_REQUEST.operation_id,
        actor_id=_REQUEST.actor_id,
        project_id=_REQUEST.project_id,
        new_owner_id=_REQUEST.new_owner_id,
        data=OwnerTransferResult(
            previous_owner_id=uuid.UUID("16a512e8-7084-4ad6-b6b8-f83a8f4ade6c"),
            updated_at=datetime(2026, 10, 21, 12, tzinfo=UTC),
            changed=True,
        ),
    ).model_dump(mode="json", by_alias=True)


class FakePostgresError(asyncpg.PostgresError):
    def __init__(self, sqlstate: str, hint: str | None = None):
        super().__init__("injected postgres failure")
        self.sqlstate = sqlstate
        self.hint = hint


class FakeTransaction:
    def __init__(self, connection, readonly):
        self.connection = connection
        self.readonly = readonly

    async def __aenter__(self):
        self.connection.in_transaction = True
        self.connection.transaction_readonly = self.readonly
        self.connection.events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.connection.events.append("rollback")
        else:
            self.connection.events.append("commit")
        self.connection.in_transaction = False
        return False


class FakeConnection:
    def __init__(self, *, policy=None, transfer=None, transfer_error=None):
        self.policy = policy or {"schema": OWNER_TRANSFER_PROTOCOL}
        self.transfer = transfer or _success()
        self.transfer_error = transfer_error
        self.events = []
        self.queries = []
        self.in_transaction = False
        self.transaction_readonly = None
        self.closed = False

    def transaction(self, *, readonly=False):
        return FakeTransaction(self, readonly)

    async def fetchval(self, query, *args):
        self.queries.append((query, args, self.in_transaction))
        if query.endswith("owner_transfer_policy()"):
            return self.policy
        assert query.endswith("transfer_project_owner($1::jsonb)")
        if self.transfer_error is not None:
            raise self.transfer_error
        assert json.loads(args[0]) == _REQUEST.model_dump(mode="json", by_alias=True)
        return self.transfer

    async def close(self, *, timeout):
        assert timeout == 2
        self.closed = True


def _install_capability_helper(monkeypatch, calls):
    module = ModuleType("aap_owner_data.postgres")

    class FunctionRoleUnavailable(Exception):
        pass

    async def verify_function_only_role(connection, *, schema, functions):
        calls.append((connection, schema, functions, connection.in_transaction))

    module.FunctionRoleUnavailable = FunctionRoleUnavailable
    module.verify_function_only_role = verify_function_only_role
    monkeypatch.setitem(sys.modules, module.__name__, module)


def _install_connection(monkeypatch, connection):
    monkeypatch.setattr(
        asyncpg, "connect", lambda *args, **kwargs: _async_conn(connection)
    )


async def _async_conn(connection):
    return connection


@pytest.mark.asyncio
async def test_operation_checks_role_and_policy_on_same_transaction_connection(
    monkeypatch,
):
    calls = []
    _install_capability_helper(monkeypatch, calls)
    connection = FakeConnection()
    _install_connection(monkeypatch, connection)
    data = OwnerTransferPostgresData("postgresql://owner-transfer:test")

    reply = await OwnerTransferManager(data).execute(_REQUEST)

    assert reply.ok is True
    assert calls == [
        (connection, _OWNER_TRANSFER_SCHEMA, _OWNER_TRANSFER_FUNCTIONS, True)
    ]
    assert [sql.rsplit(".", 1)[-1] for sql, _, _ in connection.queries] == [
        "owner_transfer_policy()",
        "transfer_project_owner($1::jsonb)",
    ]
    assert all(in_transaction for _, _, in_transaction in connection.queries)
    assert connection.events == ["begin", "commit"]
    assert connection.closed is True


@pytest.mark.asyncio
async def test_policy_mismatch_fails_closed_before_transfer_and_rolls_back(monkeypatch):
    calls = []
    _install_capability_helper(monkeypatch, calls)
    connection = FakeConnection(policy={"schema": "aap-other/v1"})
    _install_connection(monkeypatch, connection)
    data = OwnerTransferPostgresData("postgresql://owner-transfer:test")

    with pytest.raises(OwnerTransferUnavailable):
        await OwnerTransferManager(data).execute(_REQUEST)

    assert len(connection.queries) == 1
    assert connection.queries[0][0].endswith("owner_transfer_policy()")
    assert connection.events == ["begin", "rollback"]
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01"])
@pytest.mark.parametrize(
    ("hint", "expected_detail", "expected_rollback"),
    [
        (_ACCOUNT_LOCK_HINT + "\nsource hint", "账号正在更新，请刷新后重试", True),
        (_PROJECT_LOCK_HINT + "\nsource hint", {"reason": "project_busy"}, False),
    ],
)
async def test_only_marked_expected_lock_states_are_mapped(
    monkeypatch, sqlstate, hint, expected_detail, expected_rollback
):
    calls = []
    _install_capability_helper(monkeypatch, calls)
    connection = FakeConnection(transfer_error=FakePostgresError(sqlstate, hint=hint))
    _install_connection(monkeypatch, connection)
    data = OwnerTransferPostgresData("postgresql://owner-transfer:test")

    reply = await OwnerTransferManager(data).execute(_REQUEST)

    assert isinstance(reply, OwnerTransferFailure)
    assert reply.error.detail == expected_detail
    assert reply.error.rollback is expected_rollback
    # The standalone asyncpg owner aborts its own transaction regardless of the
    # rollback bit; the borrowed caller adapter uses that bit to preserve legacy
    # caller-session behaviour.
    assert connection.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_unmarked_or_unexpected_sqlstate_is_preserved_as_raw_error(monkeypatch):
    calls = []
    _install_capability_helper(monkeypatch, calls)
    error = FakePostgresError("23503", hint=_ACCOUNT_LOCK_HINT)
    connection = FakeConnection(transfer_error=error)
    _install_connection(monkeypatch, connection)
    data = OwnerTransferPostgresData("postgresql://owner-transfer:test")

    with pytest.raises(FakePostgresError) as raised:
        await OwnerTransferManager(data).execute(_REQUEST)
    assert raised.value is error
    assert connection.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_busy_state_without_exact_marker_remains_a_raw_error(monkeypatch):
    calls = []
    _install_capability_helper(monkeypatch, calls)
    error = FakePostgresError("55P03", hint="unrelated project lock hint")
    connection = FakeConnection(transfer_error=error)
    _install_connection(monkeypatch, connection)
    data = OwnerTransferPostgresData("postgresql://owner-transfer:test")

    with pytest.raises(FakePostgresError) as raised:
        await OwnerTransferManager(data).execute(_REQUEST)
    assert raised.value is error
    assert connection.events == ["begin", "rollback"]
