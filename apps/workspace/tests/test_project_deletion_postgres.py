from __future__ import annotations

import json
import uuid

import pytest

from aap_audit_contract import ProjectDeletionAuditContext
from aap_owner_data import postgres as owner_postgres
from aap_workspace.project_deletion_contract import (
    PROJECT_DELETION_PROTOCOL,
    ProjectDeletionCommand,
)
from aap_workspace.project_deletion_postgres import (
    _PROJECT_DELETION_FUNCTIONS,
    _DELETE_PROJECT_SQL,
    NATIVE_SCHEMA,
    ProjectDeletionPostgresData,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")


def _command(audit_context=None):
    return ProjectDeletionCommand(
        operation_id=_OPERATION_ID,
        actor_id=_ACTOR_ID,
        project_id=_PROJECT_ID,
        audit_context=audit_context,
    )


def _reply():
    return {
        "schema": PROJECT_DELETION_PROTOCOL,
        "ok": True,
        "data": {
            "operation_id": str(_OPERATION_ID),
            "actor_id": str(_ACTOR_ID),
            "project_id": str(_PROJECT_ID),
            "deleted_tasks": 2,
            "deleted_annotations": 3,
            "audit_id": 1,
        },
    }


class FakeConnection:
    def __init__(self, *, active=True, closed=False, reply=None, error=None):
        self.active = active
        self.closed = closed
        self.reply = _reply() if reply is None else reply
        self.error = error
        self.events = []
        self.calls = []
        self.ownership_calls = []

    def is_closed(self):
        return self.closed

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, query, *args):
        self.events.append("write")
        self.calls.append((query, args, self.active))
        if self.error is not None:
            raise self.error
        return self.reply

    async def commit(self):
        self.ownership_calls.append("commit")

    async def rollback(self):
        self.ownership_calls.append("rollback")

    async def close(self):
        self.ownership_calls.append("close")

    def transaction(self):
        self.ownership_calls.append("transaction")
        raise AssertionError("The borrowed adapter must not create a transaction")


def _install_verifier(monkeypatch, *, error=None, end_transaction=False):
    calls = []

    async def verify(connection, *, schema, functions, role):
        assert role == "workspace_project_deletion_client"
        calls.append((connection, schema, functions, connection.is_in_transaction()))
        connection.events.append("verify")
        if end_transaction:
            connection.active = False
        if error is not None:
            raise error

    monkeypatch.setattr(owner_postgres, "verify_function_only_role", verify)
    return calls


@pytest.mark.asyncio
async def test_adapter_checks_exact_role_before_native_write_in_borrowed_transaction(
    monkeypatch,
):
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch)

    reply = await ProjectDeletionPostgresData(connection).delete_project(_command())

    assert reply == _reply()
    assert calls == [(connection, NATIVE_SCHEMA, _PROJECT_DELETION_FUNCTIONS, True)]
    assert connection.events == ["verify", "write"]
    query, args, active = connection.calls[0]
    assert query == _DELETE_PROJECT_SQL
    assert args == (_OPERATION_ID, _ACTOR_ID, _PROJECT_ID, None)
    assert active is True
    assert connection.ownership_calls == []


@pytest.mark.asyncio
async def test_null_audit_context_binds_sql_null_and_context_binds_json(monkeypatch):
    _install_verifier(monkeypatch)
    connection = FakeConnection()
    context = ProjectDeletionAuditContext(
        method="DELETE", path="/api/v1/projects/one", ip="192.0.2.8"
    )

    await ProjectDeletionPostgresData(connection).delete_project(_command(context))

    bound_context = connection.calls[0][1][3]
    assert isinstance(bound_context, str)
    assert json.loads(bound_context) == {
        "method": "DELETE",
        "path": "/api/v1/projects/one",
        "ip": "192.0.2.8",
        "request_id": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "closed", "message"),
    [
        (False, False, "active caller transaction"),
        (True, True, "open asyncpg connection"),
    ],
)
async def test_closed_or_autocommit_connection_is_rejected_before_gate_or_write(
    monkeypatch, active, closed, message
):
    connection = FakeConnection(active=active, closed=closed)
    calls = _install_verifier(monkeypatch)

    with pytest.raises(ValueError, match=message):
        await ProjectDeletionPostgresData(connection).delete_project(_command())

    assert calls == []
    assert connection.calls == []
    assert connection.ownership_calls == []


@pytest.mark.asyncio
async def test_transaction_must_still_be_active_after_role_verification(monkeypatch):
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch, end_transaction=True)

    with pytest.raises(ValueError, match="active caller transaction"):
        await ProjectDeletionPostgresData(connection).delete_project(_command())

    assert len(calls) == 1
    assert connection.events == ["verify"]
    assert connection.calls == []
    assert connection.ownership_calls == []


@pytest.mark.asyncio
async def test_function_gate_failure_prevents_write_and_propagates(monkeypatch):
    error = RuntimeError("not a function-only Workspace role")
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch, error=error)

    with pytest.raises(RuntimeError) as raised:
        await ProjectDeletionPostgresData(connection).delete_project(_command())

    assert raised.value is error
    assert calls == [(connection, NATIVE_SCHEMA, _PROJECT_DELETION_FUNCTIONS, True)]
    assert connection.events == ["verify"]
    assert connection.calls == []
    assert connection.ownership_calls == []


@pytest.mark.asyncio
async def test_unexpected_sql_error_is_preserved_and_adapter_does_not_rollback(
    monkeypatch,
):
    _install_verifier(monkeypatch)
    error = RuntimeError("native SQL error")
    connection = FakeConnection(error=error)

    with pytest.raises(RuntimeError) as raised:
        await ProjectDeletionPostgresData(connection).delete_project(_command())

    assert raised.value is error
    assert connection.events == ["verify", "write"]
    assert connection.ownership_calls == []
