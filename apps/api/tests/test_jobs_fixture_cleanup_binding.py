"""Pure failure-boundary checks for the fixed Jobs fixture cleanup call."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1._test_seed import _try_fixture_delete


_JOBS_DELETE = "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:pids AS uuid[]))"


class _Result:
    def __init__(self, count):
        self.count = count

    def scalar_one(self):
        return self.count


class _Savepoint:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.db.savepoint_outcomes.append("rollback" if exc_type else "release")
        return False


class _Session:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.savepoint_outcomes = []

    def begin_nested(self):
        return _Savepoint(self)

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        if self.error is not None:
            raise self.error
        return self.result


class _OriginalDatabaseError(Exception):
    def __init__(self, sqlstate, message):
        super().__init__(message)
        self.sqlstate = sqlstate


class _DatabaseError(Exception):
    def __init__(self, sqlstate, message):
        super().__init__(message)
        self.orig = _OriginalDatabaseError(sqlstate, message)


@pytest.mark.parametrize("count", [True, -1, None])
async def test_jobs_fixture_delete_rejects_invalid_count_and_rolls_back(count):
    db = _Session(result=_Result(count))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, _JOBS_DELETE, {"pids": ["project-id"]})

    assert caught.value.status_code == 503
    assert db.calls[0][0] == _JOBS_DELETE
    assert db.savepoint_outcomes == ["rollback"]


@pytest.mark.parametrize(
    "sqlstate,message,expected_detail",
    [
        (
            "23514",
            "video_execution_not_retired",
            {
                "code": "video_execution_not_retired",
                "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
            },
        ),
        ("55P03", "lock not available", {"reason": "project_busy"}),
    ],
)
async def test_jobs_fixture_delete_preserves_project_guard_conflicts(
    sqlstate, message, expected_detail
):
    db = _Session(error=_DatabaseError(sqlstate, message))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, _JOBS_DELETE, {"pids": ["project-id"]})

    assert caught.value.status_code == 409
    assert caught.value.detail == expected_detail
    assert db.savepoint_outcomes == ["rollback"]


async def test_jobs_fixture_delete_maps_unavailable_owner_to_503_without_skipping():
    db = _Session(error=_DatabaseError("42883", "function does not exist"))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, _JOBS_DELETE, {"pids": ["project-id"]})

    assert caught.value.status_code == 503
    assert db.savepoint_outcomes == ["rollback"]


async def test_jobs_fixture_delete_keeps_transaction_abort_fail_fast():
    error = _DatabaseError("40P01", "deadlock detected")
    db = _Session(error=error)

    with pytest.raises(_DatabaseError) as caught:
        await _try_fixture_delete(db, _JOBS_DELETE, {"pids": ["project-id"]})

    assert caught.value is error
    assert db.savepoint_outcomes == ["rollback"]


async def test_jobs_fixture_delete_releases_savepoint_and_preserves_statement_params():
    project_ids = ["project-a", "project-b"]
    params = {"pids": project_ids}
    db = _Session(result=_Result(2))

    await _try_fixture_delete(db, _JOBS_DELETE, params)

    assert db.calls == [(_JOBS_DELETE, params)]
    assert db.calls[0][1] is params
    assert db.calls[0][1]["pids"] is project_ids
    assert db.savepoint_outcomes == ["release"]


async def test_unrelated_legacy_fixture_delete_keeps_skip_behavior():
    db = _Session(error=RuntimeError("legacy table unavailable"))

    await _try_fixture_delete(db, "DELETE FROM legacy_fixture_rows", {})

    assert db.savepoint_outcomes == ["rollback"]
