from __future__ import annotations

import json
import uuid

import pytest

from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace.dataset_unlink_contract import (
    DATASET_UNLINK_PREVIEW_PROTOCOL,
    DATASET_UNLINK_PROTOCOL,
    DatasetUnlinkCommand,
    DatasetUnlinkPreviewCommand,
)
from aap_workspace import dataset_unlink_postgres as unlink_postgres
from aap_workspace.dataset_unlink_postgres import (
    _DATASET_UNLINK_FUNCTIONS,
    _PREVIEW_DATASET_UNLINK_SQL,
    _UNLINK_DATASET_SQL,
    DatasetUnlinkPostgresData,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_DATASET_ID = uuid.UUID("23b973f5-a1fa-4973-bde0-c61d36c93e8e")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_CLEANUP_BUCKET = "media-cache-test"
_ASSOCIATION_ID = uuid.uuid4()
_BATCH_ID = uuid.uuid4()


def _command(audit_context=None):
    return DatasetUnlinkCommand(
        operation_id=_OPERATION_ID,
        actor_id=_ACTOR_ID,
        dataset_id=_DATASET_ID,
        project_id=_PROJECT_ID,
        cleanup_bucket=_CLEANUP_BUCKET,
        audit_context=audit_context,
    )


def _preview_command():
    return DatasetUnlinkPreviewCommand(
        actor_id=_ACTOR_ID,
        dataset_id=_DATASET_ID,
        project_id=_PROJECT_ID,
    )


def _unlink_reply():
    return {
        "schema": DATASET_UNLINK_PROTOCOL,
        "ok": True,
        "data": {
            "operation_id": str(_OPERATION_ID),
            "actor_id": str(_ACTOR_ID),
            "dataset_id": str(_DATASET_ID),
            "project_id": str(_PROJECT_ID),
            "association_id": str(_ASSOCIATION_ID),
            "cleanup_bucket": _CLEANUP_BUCKET,
            "audit_id": 731,
            "deleted_tasks": 4,
            "deleted_annotations": 3,
            "deleted_batches": 1,
            "deleted_batch_ids": [str(_BATCH_ID)],
            "soft": False,
        },
    }


def _preview_reply():
    return {
        "schema": DATASET_UNLINK_PREVIEW_PROTOCOL,
        "ok": True,
        "data": {
            "will_delete_tasks": 2,
            "will_delete_annotations": 1,
            "will_delete_batches": 1,
        },
    }


class FakeConnection:
    def __init__(self, *, active=True, closed=False, error=None):
        self.active = active
        self.closed = closed
        self.error = error
        self.events = []
        self.calls = []
        self.ownership_calls = []

    def is_closed(self):
        return self.closed

    def is_in_transaction(self):
        return self.active

    async def fetchval(self, query, *args):
        self.events.append("call")
        self.calls.append((query, args, self.active))
        if self.error is not None:
            raise self.error
        if query == _PREVIEW_DATASET_UNLINK_SQL:
            return _preview_reply()
        return _unlink_reply()

    async def commit(self):
        self.ownership_calls.append("commit")

    async def rollback(self):
        self.ownership_calls.append("rollback")

    async def close(self):
        self.ownership_calls.append("close")

    def transaction(self, **_kwargs):
        self.ownership_calls.append("transaction")
        raise AssertionError("Dataset unlink must borrow the caller transaction")


def _install_verifier(monkeypatch, *, error=None, end_transaction=False):
    calls = []

    async def verify(connection, *, schema, functions, role):
        calls.append(
            (connection, schema, functions, role, connection.is_in_transaction())
        )
        connection.events.append("verify")
        if end_transaction:
            connection.active = False
        if error is not None:
            raise error

    monkeypatch.setattr(unlink_postgres, "verify_function_only_role", verify)
    return calls


@pytest.mark.asyncio
async def test_unlink_uses_exact_function_gate_and_borrowed_transaction(monkeypatch):
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch)

    result = await DatasetUnlinkPostgresData(connection).unlink_dataset(_command())

    assert result == _unlink_reply()
    assert calls == [
        (
            connection,
            "aap_workspace_v1",
            _DATASET_UNLINK_FUNCTIONS,
            "workspace_dataset_unlink_client",
            True,
        )
    ]
    assert connection.events == ["verify", "call"]
    query, args, active = connection.calls[0]
    assert query == _UNLINK_DATASET_SQL
    assert args == (
        _OPERATION_ID,
        _ACTOR_ID,
        _DATASET_ID,
        _PROJECT_ID,
        _CLEANUP_BUCKET,
        None,
    )
    assert active is True
    assert connection.ownership_calls == []


@pytest.mark.asyncio
async def test_unlink_binds_audit_context_as_json_and_null_as_sql_null(monkeypatch):
    _install_verifier(monkeypatch)
    context = ProjectDeletionAuditContext(
        method="DELETE",
        path=f"/api/v1/datasets/{_DATASET_ID}/link/{_PROJECT_ID}",
        ip="192.0.2.8",
        request_id="request-42",
    )
    connection = FakeConnection()

    await DatasetUnlinkPostgresData(connection).unlink_dataset(_command(context))

    bound_context = connection.calls[0][1][5]
    assert isinstance(bound_context, str)
    assert json.loads(bound_context) == context.model_dump(mode="json")


@pytest.mark.asyncio
async def test_preview_uses_existing_volatile_function_in_active_transaction(
    monkeypatch,
):
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch)

    result = await DatasetUnlinkPostgresData(connection).preview_dataset_unlink(
        _preview_command()
    )

    assert result == _preview_reply()
    assert calls == [
        (
            connection,
            "aap_workspace_v1",
            _DATASET_UNLINK_FUNCTIONS,
            "workspace_dataset_unlink_client",
            True,
        )
    ]
    query, args, active = connection.calls[0]
    assert query == _PREVIEW_DATASET_UNLINK_SQL
    assert args == (_ACTOR_ID, _DATASET_ID, _PROJECT_ID)
    assert active is True
    assert connection.ownership_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "closed", "operation", "message"),
    [
        (False, False, "unlink", "active caller transaction"),
        (True, True, "unlink", "open asyncpg connection"),
        (False, False, "preview", "active caller transaction"),
        (True, True, "preview", "open asyncpg connection"),
    ],
)
async def test_connection_must_be_open_and_in_transaction_before_gate_or_call(
    monkeypatch, active, closed, operation, message
):
    connection = FakeConnection(active=active, closed=closed)
    calls = _install_verifier(monkeypatch)
    data = DatasetUnlinkPostgresData(connection)

    with pytest.raises(ValueError, match=message):
        if operation == "unlink":
            await data.unlink_dataset(_command())
        else:
            await data.preview_dataset_unlink(_preview_command())

    assert calls == []
    assert connection.calls == []
    assert connection.ownership_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unlink", "preview"])
async def test_transaction_must_remain_active_after_function_role_verification(
    monkeypatch, operation
):
    connection = FakeConnection()
    calls = _install_verifier(monkeypatch, end_transaction=True)
    data = DatasetUnlinkPostgresData(connection)

    with pytest.raises(ValueError, match="active caller transaction"):
        if operation == "unlink":
            await data.unlink_dataset(_command())
        else:
            await data.preview_dataset_unlink(_preview_command())

    assert len(calls) == 1
    assert connection.events == ["verify"]
    assert connection.calls == []
    assert connection.ownership_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unlink", "preview"])
async def test_verifier_and_sql_errors_propagate_without_transaction_ownership(
    monkeypatch, operation
):
    verifier_error = RuntimeError("unexpected function gate failure")
    connection = FakeConnection()
    _install_verifier(monkeypatch, error=verifier_error)
    data = DatasetUnlinkPostgresData(connection)

    with pytest.raises(RuntimeError) as raised:
        if operation == "unlink":
            await data.unlink_dataset(_command())
        else:
            await data.preview_dataset_unlink(_preview_command())
    assert raised.value is verifier_error
    assert connection.events == ["verify"]
    assert connection.calls == []
    assert connection.ownership_calls == []

    sql_error = RuntimeError("unexpected Native SQL error")
    connection = FakeConnection(error=sql_error)
    _install_verifier(monkeypatch)
    data = DatasetUnlinkPostgresData(connection)
    with pytest.raises(RuntimeError) as raised:
        if operation == "unlink":
            await data.unlink_dataset(_command())
        else:
            await data.preview_dataset_unlink(_preview_command())
    assert raised.value is sql_error
    assert connection.events == ["verify", "call"]
    assert connection.ownership_calls == []
