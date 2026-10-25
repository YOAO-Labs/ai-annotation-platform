from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect

from app.api.v1._test_seed_filters import _set_filter_fixture_job_outcomes
from app.db.models.async_job import AsyncJob


_OUTCOMES_SQL = (
    "SELECT aap_jobs_v1.set_filter_fixture_job_outcomes("
    "CAST(:p_completed_job_id AS pg_catalog.uuid), "
    "CAST(:p_failed_job_id AS pg_catalog.uuid))"
)


class _Session:
    def __init__(self, result: object = 2, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, uuid.UUID]]] = []
        self.expired: list[tuple[AsyncJob, list[str]]] = []

    async def scalar(self, statement, params):
        self.calls.append((str(statement), params))
        if self.error is not None:
            raise self.error
        return self.result

    def expire(self, row: AsyncJob, attributes: list[str]) -> None:
        self.expired.append((row, attributes))


def _job() -> AsyncJob:
    return AsyncJob(
        id=uuid.uuid4(),
        kind="batch_predict",
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="pending",
        progress_pct=0,
        payload={},
        result={},
        error_message=None,
        started_at=None,
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_filter_fixture_outcomes_call_owner_and_synchronize_cached_rows():
    completed, failed = _job(), _job()
    db = _Session()

    await _set_filter_fixture_job_outcomes(db, completed, failed)

    assert db.calls == [
        (
            _OUTCOMES_SQL,
            {
                "p_completed_job_id": completed.id,
                "p_failed_job_id": failed.id,
            },
        )
    ]
    assert completed.status == "completed"
    assert completed.progress_pct == 100
    assert completed.result == {"success_count": 2}
    assert completed.started_at is None and completed.completed_at is None
    assert failed.status == "failed"
    assert failed.progress_pct == 40
    assert failed.error_message == "fixture failure"
    assert failed.result == {}
    assert failed.started_at is None and failed.completed_at is None
    assert db.expired == [(completed, ["updated_at"]), (failed, ["updated_at"])]
    assert not inspect(completed).attrs.status.history.has_changes()
    assert not inspect(completed).attrs.progress_pct.history.has_changes()
    assert not inspect(completed).attrs.result.history.has_changes()
    assert not inspect(failed).attrs.status.history.has_changes()
    assert not inspect(failed).attrs.progress_pct.history.has_changes()
    assert not inspect(failed).attrs.error_message.history.has_changes()


@pytest.mark.parametrize("count", [True, "2", None, 1, 3])
@pytest.mark.asyncio
async def test_filter_fixture_outcomes_reject_non_exact_count_before_cache_changes(
    count: object,
):
    completed, failed = _job(), _job()
    db = _Session(result=count)

    with pytest.raises(RuntimeError, match="outcome count mismatch"):
        await _set_filter_fixture_job_outcomes(db, completed, failed)

    assert completed.status == failed.status == "pending"
    assert completed.progress_pct == failed.progress_pct == 0
    assert completed.result == failed.result == {}
    assert completed.error_message is failed.error_message is None
    assert db.expired == []


@pytest.mark.asyncio
async def test_filter_fixture_outcomes_propagate_owner_errors_without_cache_changes():
    completed, failed = _job(), _job()
    error = RuntimeError("owner unavailable")
    db = _Session(error=error)

    with pytest.raises(RuntimeError) as caught:
        await _set_filter_fixture_job_outcomes(db, completed, failed)

    assert caught.value is error
    assert completed.status == failed.status == "pending"
    assert completed.progress_pct == failed.progress_pct == 0
    assert db.expired == []
