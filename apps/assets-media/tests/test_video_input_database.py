import asyncio
from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import pytest

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs_runtime.database import (
    AsyncpgVideoInputDatabase,
    prepare_native_call,
)
from video_input_fixtures import CURRENT_ACTOR, LEASE, record, time_entries


def request():
    accepted = record()
    return c.ReadVideoInputRequest(
        input_id=accepted.input_id,
        job_id=accepted.job_id,
        task_id=accepted.task.task_id,
        actor_id=CURRENT_ACTOR,
    )


def test_native_binding_rejects_unknown_operation_model_and_cross_login_roles():
    for operation, value, exception in (
        ("read_accepted_video_input; DROP TABLE tasks", request(), TypeError),
        ("read_accepted_video_input", c.SweepVideoInputsRequest(), TypeError),
        ("read_accepted_video_input", request(), PermissionError),
    ):
        with pytest.raises(exception):
            prepare_native_call(operation, value, allowed_roles={"worker"})


def test_native_time_staging_serializes_nested_values_without_interpolated_sql():
    staging = c.StageVideoTimesRequest(
        input_id=record().input_id,
        generation=1,
        lease_token=LEASE,
        cursor=0,
        entries=time_entries(),
    )
    sql, arguments = prepare_native_call(
        "stage_accepted_video_times", staging, allowed_roles={"worker"}
    )
    assert (
        sql
        == "SELECT aap_assets_v1.stage_accepted_video_times($1::uuid, $2::bigint, $3::uuid, $4::bigint, $5::jsonb)"
    )
    assert arguments[:4] == (staging.input_id, 1, LEASE, 0)
    assert json.loads(arguments[4]) == [
        entry.model_dump(mode="json") for entry in time_entries()
    ]
    assert str(staging.input_id) not in sql


class Pool:
    def __init__(self, raw):
        self.raw = raw
        self.arguments = None
        self.exited = False
        self.execute = AsyncMock()

    @asynccontextmanager
    async def acquire(self, *, timeout):
        assert timeout == 5
        yield self

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        finally:
            self.exited = True

    async def fetchval(self, sql, *args, timeout):
        self.arguments = sql, args
        if isinstance(self.raw, BaseException):
            raise self.raw
        return self.raw


async def test_database_uses_short_transaction_and_validates_before_commit():
    accepted = record()
    pool = Pool(
        {
            "schema": c.EXECUTION_SCHEMA,
            "ok": True,
            "data": accepted.model_dump(mode="json"),
        }
    )
    result = await AsyncpgVideoInputDatabase(pool, allowed_roles={"control"}).call(
        "read_accepted_video_input", request()
    )
    assert result == accepted and pool.exited
    pool.execute.assert_awaited_once_with(
        "SELECT set_config('statement_timeout', $1, true)", "5000"
    )
    assert pool.arguments[1] == (
        accepted.input_id,
        accepted.job_id,
        accepted.task.task_id,
        CURRENT_ACTOR,
    )


@pytest.mark.parametrize(
    "raw,code",
    [
        (
            {
                "schema": c.EXECUTION_SCHEMA,
                "ok": False,
                "error": {"code": "source_changed"},
            },
            "source_changed",
        ),
        ({"schema": c.EXECUTION_SCHEMA, "ok": True, "data": {}}, "invalid_response"),
        (RuntimeError("private database credentials"), "storage_unavailable"),
    ],
)
async def test_database_preserves_protocol_failures_and_hides_driver_details(raw, code):
    pool = Pool(raw)
    with pytest.raises(c.VideoInputFailure, match=f"^{code}$"):
        await AsyncpgVideoInputDatabase(pool, allowed_roles={"control"}).call(
            "read_accepted_video_input", request()
        )
    assert pool.exited


async def test_database_cancellation_propagates_and_releases_the_transaction():
    pool = Pool(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await AsyncpgVideoInputDatabase(pool, allowed_roles={"control"}).call(
            "read_accepted_video_input", request()
        )
    assert pool.exited


@pytest.mark.parametrize("roles", [set(), {"workflow"}, {"control", "superuser"}])
def test_runtime_cannot_start_with_workflow_or_unknown_login(roles):
    with pytest.raises(ValueError):
        AsyncpgVideoInputDatabase(None, allowed_roles=roles)
