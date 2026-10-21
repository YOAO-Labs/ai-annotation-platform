from __future__ import annotations

import asyncio
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from aap_inference.pipeline_contract import (
    PipelineInvalidRequest,
    PipelineRejected,
    PipelineUnavailable,
    pipeline_policy,
    pipeline_command_request,
)
from aap_inference.pipeline_postgres import (
    PipelinePostgresData,
    PipelinePostgresTransaction,
    _POLICY_SQL,
    _STEP_SQL,
)


class _FakePostgresError(Exception):
    def __init__(self, sqlstate: str, hint: str | None = None) -> None:
        self.sqlstate = sqlstate
        self.hint = hint
        super().__init__("private driver diagnostic")


class _FakeInterfaceError(Exception):
    pass


class _FakeConnection:
    def __init__(
        self,
        raw=None,
        error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.raw = raw if raw is not None else {"schema": "native-step"}
        self.error = error
        self.close_error = close_error
        self.calls = []
        self.closed = 0
        self.terminated = 0

    async def fetchval(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        if args[0] == _POLICY_SQL:
            return pipeline_policy().model_dump(mode="json", by_alias=True)
        return self.raw

    def transaction(self, *, readonly: bool):
        return self.transaction_factory(readonly=readonly)

    async def close(self, *, timeout):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error

    def terminate(self):
        self.terminated += 1


class _FakeTransaction:
    def __init__(self, *, readonly: bool, commit_error=None) -> None:
        self.readonly = readonly
        self.commit_error = commit_error
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self):
        self.started = True

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def _fake_asyncpg(monkeypatch, connection):
    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=connect,
            PostgresError=_FakePostgresError,
            InterfaceError=_FakeInterfaceError,
        ),
    )

    async def verify_function_only_role(*args, **kwargs):
        return None

    monkeypatch.setitem(
        sys.modules,
        "aap_owner_data.postgres",
        SimpleNamespace(
            FunctionRoleUnavailable=type("FunctionRoleUnavailable", (Exception,), {}),
            verify_function_only_role=verify_function_only_role,
        ),
    )


def _request(operation="create_pipeline", target_id=None):
    return pipeline_command_request(
        operation,
        actor_id=uuid.UUID("0f9210aa-91c3-4a26-a78f-16367d4f811d"),
        actor_role="project_admin",
        target_id=target_id,
        payload={},
    )


def _transaction(connection):
    return PipelinePostgresTransaction(
        connection,
        SimpleNamespace(
            PostgresError=_FakePostgresError,
            InterfaceError=_FakeInterfaceError,
        ),
    )


def test_step_uses_one_fixed_native_call_and_empty_json_args():
    connection = _FakeConnection()
    transaction = _transaction(connection)
    result = asyncio.run(transaction.step(_request(), "begin", None))

    assert transaction.autoflush is False
    assert transaction.owns_commit is True
    assert result == {"schema": "native-step"}
    sql, request_json, action, args_json, columns, values = connection.calls[0]
    assert sql == _STEP_SQL
    assert json.loads(request_json)["schema"] == "aap-inference-pipelines/v1"
    assert action == "begin"
    assert json.loads(args_json) == {}
    assert columns == []
    assert values == []


def test_step_rejects_unapproved_write_columns_before_calling_native():
    connection = _FakeConnection()
    transaction = _transaction(connection)

    with pytest.raises(PipelineInvalidRequest):
        asyncio.run(
            transaction.step(
                _request(),
                "pipeline_insert",
                {},
                columns=["owner_id"],
                values=[str(uuid.uuid4())],
            )
        )
    assert connection.calls == []


def test_noop_update_keeps_empty_write_rpc_for_legacy_flush_phase():
    target_id = uuid.UUID("d6df1331-2645-4da1-b1cf-e358e131ab4e")
    connection = _FakeConnection()
    transaction = _transaction(connection)

    result = asyncio.run(
        transaction.step(
            _request("update_pipeline", target_id),
            "pipeline_update",
            {"pipeline_id": str(target_id)},
            columns=[],
            values=[],
        )
    )

    assert result == {"schema": "native-step"}
    assert connection.calls[0][2] == "pipeline_update"
    assert connection.calls[0][4:] == ([], [])


def test_only_exact_workspace_lock_hint_maps_to_retryable_409():
    busy = _FakePostgresError("40001", "aap.workspace.project_access.project_busy")
    transaction = _transaction(_FakeConnection(error=busy))

    with pytest.raises(PipelineRejected) as captured:
        asyncio.run(transaction.step(_request(), "project_access", {"phase": "actor"}))
    assert captured.value.status_code == 409
    assert captured.value.detail == {"reason": "project_busy"}
    assert captured.value.rollback is True


def test_unrecognized_lock_hint_does_not_become_busy_409():
    error = _FakePostgresError("55P03", "unrelated.lock")
    transaction = _transaction(_FakeConnection(error=error))

    with pytest.raises(PipelineRejected) as captured:
        asyncio.run(transaction.step(_request(), "project_access", {"phase": "actor"}))
    assert captured.value.status_code == 500
    assert captured.value.detail == "Internal Server Error"
    assert captured.value.rollback is True
    assert "private driver diagnostic" not in str(captured.value)


def test_unknown_commit_error_becomes_unavailable_not_rolled_back(monkeypatch):
    commit_error = _FakePostgresError("08006")
    transaction = _FakeTransaction(readonly=False, commit_error=commit_error)
    connection = _FakeConnection()
    connection.transaction_factory = lambda *, readonly: transaction
    _fake_asyncpg(monkeypatch, connection)
    data = PipelinePostgresData("postgresql://inference@localhost/db")

    async def run():
        async with data.transaction(readonly=False):
            pass

    with pytest.raises(PipelineUnavailable):
        asyncio.run(run())
    assert transaction.started is True
    assert transaction.committed is False
    assert transaction.rolled_back is False
    assert connection.closed == 1


def test_deferred_constraint_commit_error_is_known_rollback(monkeypatch):
    commit_error = _FakePostgresError("23514")
    transaction = _FakeTransaction(readonly=False, commit_error=commit_error)
    connection = _FakeConnection()
    connection.transaction_factory = lambda *, readonly: transaction
    _fake_asyncpg(monkeypatch, connection)
    data = PipelinePostgresData("postgresql://inference@localhost/db")

    async def run():
        async with data.transaction(readonly=False):
            pass

    with pytest.raises(PipelineRejected) as captured:
        asyncio.run(run())
    assert captured.value.status_code == 500
    assert captured.value.detail == "Internal Server Error"
    assert captured.value.rollback is True
    assert transaction.committed is False
    assert connection.closed == 1


def test_statement_completion_unknown_commit_error_stays_unavailable(monkeypatch):
    commit_error = _FakePostgresError("40003")
    transaction = _FakeTransaction(readonly=False, commit_error=commit_error)
    connection = _FakeConnection()
    connection.transaction_factory = lambda *, readonly: transaction
    _fake_asyncpg(monkeypatch, connection)
    data = PipelinePostgresData("postgresql://inference@localhost/db")

    async def run():
        async with data.transaction(readonly=False):
            pass

    with pytest.raises(PipelineUnavailable):
        asyncio.run(run())
    assert transaction.committed is False
    assert transaction.rolled_back is False
    assert connection.closed == 1


def test_close_failure_after_commit_terminates_and_preserves_unknown_outcome(
    monkeypatch,
):
    transaction = _FakeTransaction(readonly=False)
    connection = _FakeConnection(close_error=_FakeInterfaceError("cleanup"))
    connection.transaction_factory = lambda *, readonly: transaction
    _fake_asyncpg(monkeypatch, connection)
    data = PipelinePostgresData("postgresql://inference@localhost/db")

    async def run():
        async with data.transaction(readonly=False):
            pass

    with pytest.raises(PipelineUnavailable):
        asyncio.run(run())
    assert transaction.committed is True
    assert connection.closed == 1
    assert connection.terminated == 1


def test_body_rejection_rolls_back_and_cleanup_error_does_not_replace_it(monkeypatch):
    transaction = _FakeTransaction(readonly=True)
    connection = _FakeConnection(close_error=_FakeInterfaceError("cleanup"))
    connection.transaction_factory = lambda *, readonly: transaction
    _fake_asyncpg(monkeypatch, connection)
    data = PipelinePostgresData("postgresql://inference@localhost/db")
    rejection = PipelineRejected(409, {"reason": "project_busy"}, rollback=True)

    async def run():
        async with data.transaction(readonly=True):
            raise rejection

    with pytest.raises(PipelineRejected) as captured:
        asyncio.run(run())
    assert captured.value is rejection
    assert transaction.readonly is True
    assert transaction.rolled_back is True
    assert transaction.committed is False
    assert connection.closed == 1
    assert connection.terminated == 1
