from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import json
from uuid import UUID, uuid4

import pytest

from aap_preannotation_results import contract as c
from aap_preannotation_results_runtime.database import (
    AsyncpgBatchResultDatabase,
    EXECUTOR_OPERATIONS,
    prepare_native_call,
)


JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
ACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
PROJECT_ID = UUID("33333333-3333-4333-8333-333333333333")


def read_request():
    return c.ReadBatchResultExecutionRequest(
        job_id=JOB_ID,
        actor_id=ACTOR_ID,
        project_id=PROJECT_ID,
        request_digest="a" * 64,
    )


def read_result(progress_override=None):
    values = {
        "job_id": JOB_ID,
        "state": "running",
        "total": 0,
        "selection_cursor": 0,
        "selection_complete": True,
        "next_ordinal": 1,
        "bulk_overwrite_done": False,
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "total_cost": "0",
        "pipeline_stages": {},
        "gpu_arbiter_failures": [],
    }
    return c.ReadBatchResultExecutionResult(
        progress=progress_override or c.Progress(**values),
        members=(),
        next_ordinal=None,
    )


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transactions += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.commits += exc_type is None
        self.connection.rollbacks += exc_type is not None


class FakeConnection:
    def __init__(self, reply):
        self.reply = reply
        self.statements = []
        self.transactions = 0
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return FakeTransaction(self)

    async def execute(self, statement, *args):
        self.statements.append((statement, args))

    async def fetchval(self, statement, *args, **kwargs):
        self.statements.append((statement, args))
        return self.reply


class FakePool:
    def __init__(self, reply):
        self.connection = FakeConnection(reply)

    def acquire(self, *, timeout):
        assert timeout == 5

        class Acquire:
            async def __aenter__(inner_self):
                return self.connection

            async def __aexit__(inner_self, exc_type, exc, traceback):
                return None

        return Acquire()


def test_asyncpg_adapter_uses_one_jsonb_argument_and_commits_valid_result():
    request = read_request()
    reply = c.json_bytes(c.success_envelope(read_result()))
    pool = FakePool(reply)
    database = AsyncpgBatchResultDatabase(pool)

    result = asyncio.run(database.call("read_batch_result_execution", request))

    assert result.progress.job_id == JOB_ID
    connection = pool.connection
    assert (connection.transactions, connection.commits, connection.rollbacks) == (
        1,
        1,
        0,
    )
    assert connection.statements[1][0] == (
        "SELECT aap_jobs_v1.read_batch_result_execution($1::jsonb)"
    )
    statement, arguments = connection.statements[1]
    assert len(arguments) == 1
    assert json.loads(arguments[0]) == request.model_dump(mode="json")
    assert "schema" not in json.loads(arguments[0])


def test_adapter_rolls_back_scope_mismatch_before_commit():
    request = read_request()
    response = read_result(
        progress_override=c.Progress(
            job_id=uuid4(),
            state="running",
            total=0,
            selection_cursor=0,
            selection_complete=True,
            next_ordinal=1,
            bulk_overwrite_done=False,
            success_count=0,
            failed_count=0,
            skipped_count=0,
            total_cost="0",
            pipeline_stages={},
            gpu_arbiter_failures=[],
        )
    )
    pool = FakePool(c.json_bytes(c.success_envelope(response)))
    database = AsyncpgBatchResultDatabase(pool)

    with pytest.raises(c.BatchResultFailure) as error:
        asyncio.run(database.call("read_batch_result_execution", request))
    assert error.value.code == "invalid_response"
    assert pool.connection.commits == 0
    assert pool.connection.rollbacks == 1


def test_writer_calls_require_explicit_bridge_scope():
    request = c.FinishBatchResultWriteRequest(
        job_id=JOB_ID,
        actor_id=ACTOR_ID,
        project_id=PROJECT_ID,
        request_digest="a" * 64,
        generation=1,
        lease_token=uuid4(),
        ordinal=1,
        result_id=uuid4(),
        prepared_digest="b" * 64,
    )
    assert "finish_batch_result_write" not in EXECUTOR_OPERATIONS
    with pytest.raises(PermissionError):
        prepare_native_call("finish_batch_result_write", request)
    statement, arguments = prepare_native_call(
        "finish_batch_result_write", request, writer=True
    )
    assert statement == "SELECT aap_labeling_v1.finish_batch_result_write($1::jsonb)"
    assert len(arguments) == 1
    assert json.loads(arguments[0])["ordinal"] == 1

    pool = FakePool("unused")
    database = AsyncpgBatchResultDatabase(pool)
    with pytest.raises(c.BatchResultFailure) as error:
        asyncio.run(database.call("finish_batch_result_write", request))
    assert error.value.code == "invalid_request"
    assert pool.connection.statements == []


def test_native_prepare_checks_request_bytes_and_allowlisted_namespace():
    request = read_request()
    statement, arguments = prepare_native_call("read_batch_result_execution", request)
    assert statement.startswith("SELECT aap_jobs_v1.")
    assert len(arguments) == 1
    with pytest.raises(TypeError):
        prepare_native_call("not_a_capability", request)


def test_packages_have_no_application_or_storage_runtime_imports():
    package = Path(__file__).resolve().parents[1] / "src"
    forbidden = {"app", "sqlalchemy", "fastapi", "celery", "redis", "boto3"}
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            assert roots.isdisjoint(forbidden), f"forbidden import in {path}"
