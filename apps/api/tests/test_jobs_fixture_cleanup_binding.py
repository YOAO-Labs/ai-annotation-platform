"""Pure failure-boundary checks for fixed fixture cleanup owner calls."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.v1._test_seed import _try_fixture_delete


_JOBS_DELETE = "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:pids AS uuid[]))"
_LABELING_DELETE = (
    "SELECT aap_labeling_v1.delete_fixture_project_rows(CAST(:pids AS uuid[]))"
)
_FIXTURE_DELETES = (_JOBS_DELETE, _LABELING_DELETE)


def test_labeling_fixture_cleanup_resources_match_the_frozen_migration():
    root = Path(__file__).resolve().parents[3]
    migration = root / "apps/api/alembic/sql/0358_labeling_fixture_cleanup"
    resource = files("aap_labeling")
    sql = resource.joinpath("sql/0358_labeling_fixture_cleanup.sql").read_text(
        encoding="utf-8"
    )
    schema = json.loads(
        resource.joinpath("fixture-cleanup.schema.json").read_text(encoding="utf-8")
    )

    assert sql == migration.with_suffix(".sql").read_text(encoding="utf-8")
    assert schema == json.loads(
        migration.with_suffix(".schema.json").read_text(encoding="utf-8")
    )
    assert schema["$id"] == "aap-labeling-fixture-cleanup/v1"
    assert schema["x-postgresql-functions"] == {
        "aap_labeling_v1.delete_fixture_project_rows": {
            "arguments": "uuid[]",
            "returns": "bigint",
            "volatility": "volatile",
            "parallel": "unsafe",
        }
    }


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
@pytest.mark.parametrize("statement", _FIXTURE_DELETES)
async def test_fixture_owner_delete_rejects_invalid_count_and_rolls_back(
    statement, count
):
    db = _Session(result=_Result(count))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, statement, {"pids": ["project-id"]})

    assert caught.value.status_code == 503
    assert db.calls[0][0] == statement
    assert db.savepoint_outcomes == ["rollback"]


@pytest.mark.parametrize(
    "statement,sqlstate,message,expected_detail",
    [
        (
            statement,
            "23514",
            "video_execution_not_retired",
            {
                "code": "video_execution_not_retired",
                "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
            },
        )
        for statement in _FIXTURE_DELETES
    ]
    + [
        (statement, "55P03", "lock not available", {"reason": "project_busy"})
        for statement in _FIXTURE_DELETES
    ],
)
async def test_fixture_owner_delete_preserves_project_guard_conflicts(
    statement, sqlstate, message, expected_detail
):
    db = _Session(error=_DatabaseError(sqlstate, message))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, statement, {"pids": ["project-id"]})

    assert caught.value.status_code == 409
    assert caught.value.detail == expected_detail
    assert db.savepoint_outcomes == ["rollback"]


@pytest.mark.parametrize("statement", _FIXTURE_DELETES)
async def test_fixture_owner_delete_maps_unavailable_owner_to_503_without_skipping(
    statement,
):
    db = _Session(error=_DatabaseError("42883", "function does not exist"))

    with pytest.raises(HTTPException) as caught:
        await _try_fixture_delete(db, statement, {"pids": ["project-id"]})

    assert caught.value.status_code == 503
    assert db.savepoint_outcomes == ["rollback"]


@pytest.mark.parametrize("statement", _FIXTURE_DELETES)
async def test_fixture_owner_delete_keeps_transaction_abort_fail_fast(statement):
    error = _DatabaseError("40P01", "deadlock detected")
    db = _Session(error=error)

    with pytest.raises(_DatabaseError) as caught:
        await _try_fixture_delete(db, statement, {"pids": ["project-id"]})

    assert caught.value is error
    assert db.savepoint_outcomes == ["rollback"]


@pytest.mark.parametrize("statement", _FIXTURE_DELETES)
async def test_fixture_owner_delete_releases_savepoint_and_preserves_statement_params(
    statement,
):
    project_ids = ["project-a", "project-b"]
    params = {"pids": project_ids}
    db = _Session(result=_Result(2))

    await _try_fixture_delete(db, statement, params)

    assert db.calls == [(statement, params)]
    assert db.calls[0][1] is params
    assert db.calls[0][1]["pids"] is project_ids
    assert db.savepoint_outcomes == ["release"]


async def test_unrelated_legacy_fixture_delete_keeps_skip_behavior():
    db = _Session(error=RuntimeError("legacy table unavailable"))

    await _try_fixture_delete(db, "DELETE FROM legacy_fixture_rows", {})

    assert db.savepoint_outcomes == ["rollback"]
