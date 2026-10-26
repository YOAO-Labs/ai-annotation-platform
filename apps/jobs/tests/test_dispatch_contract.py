from __future__ import annotations

import asyncio
import uuid

import pytest

from aap_jobs.dispatch_data import JobsDispatchDataClient


def _dispatch(execution_id: str) -> dict:
    return {
        "schema": "aap-jobs-queue/v2",
        "operation": "dispatch",
        "task_name": "app.workers.tasks.batch_predict",
        "execution_id": execution_id,
        "args": ["project-id", "backend-id", ["task-id"]],
        "kwargs": {"manifest": {"items": [False, None, "雪"]}},
        "options": {"queue": "cpu"},
    }


def test_data_client_rejects_non_dispatch_queue_envelopes_before_native_call():
    calls = []

    async def execute(function_name, arguments):
        calls.append((function_name, arguments))
        raise AssertionError("invalid envelope reached Native")

    async def exercise():
        client = JobsDispatchDataClient(execute)
        execution_id = str(uuid.uuid4())
        malformed = _dispatch(execution_id)
        malformed["unsupported"] = True
        with pytest.raises(ValueError):
            await client.prepare(uuid.uuid4(), malformed)
        revoke = {
            "schema": "aap-jobs-queue/v2",
            "operation": "revoke",
            "execution_id": execution_id,
        }
        with pytest.raises(ValueError):
            await client.prepare(uuid.uuid4(), revoke)
        cyclic = _dispatch(execution_id)
        cyclic["args"].append(cyclic["args"])
        with pytest.raises(ValueError):
            await client.prepare(uuid.uuid4(), cyclic)

    asyncio.run(exercise())
    assert calls == []
