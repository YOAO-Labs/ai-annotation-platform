from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorStatus


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _Session:
    def __init__(self, results):
        self.results = iter(results)
        self.executed = []

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return next(self.results)


def _job_row(actor_id: uuid.UUID):
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind="batch_predict",
        project_id=None,
        retained_project_id=None,
        user_id=actor_id,
        status="running",
        progress_pct=25,
        payload={"prompt": "bus"},
        result={},
        error_message=None,
        celery_task_id=None,
        started_at=now,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_list_calls_identity_once_then_separate_page_and_total(monkeypatch):
    actor_id = uuid.uuid4()
    row = _job_row(actor_id)
    session = _Session([_Result(rows=[row]), _Result(scalar=3)])
    status_reads = []

    async def read_actor_status(_self, requested_actor_id):
        status_reads.append(requested_actor_id)
        return IdentityActorStatus(
            id=actor_id,
            role="employee",
            is_active=True,
        )

    monkeypatch.setattr(IdentityAccountLockData, "read_actor_status", read_actor_status)
    response = await SqlAsyncJobReadAdapter(session).list_jobs(
        actor_id=actor_id,
        status=["pending", "running"],
        kind=["future_kind", "batch_predict"],
        project_id=None,
        search="  bus  ",
        limit=7,
        offset=4,
    )

    assert status_reads == [actor_id]
    assert response.total == 3
    assert [item.id for item in response.items] == [row.id]
    assert len(session.executed) == 2
    page_statement, page_params = session.executed[0]
    total_statement, total_params = session.executed[1]
    assert "aap_jobs_v1.async_job_list_page" in str(page_statement)
    assert page_statement.get_execution_options()["populate_existing"] is True
    assert "aap_jobs_v1.async_job_list_total" in str(total_statement)
    assert page_params["actor_id"] == total_params["actor_id"] == actor_id
    assert page_params["platform_role"] == total_params["platform_role"] == "employee"
    assert (
        page_params["statuses"]
        == total_params["statuses"]
        == [
            "pending",
            "running",
        ]
    )
    assert (
        page_params["kinds"]
        == total_params["kinds"]
        == [
            "future_kind",
            "batch_predict",
        ]
    )
    assert page_params["search_pattern"] == total_params["search_pattern"] == "%bus%"
    assert page_params["limit"] == 7
    assert page_params["offset"] == 4
    assert "limit" not in total_params
    assert "offset" not in total_params


@pytest.mark.asyncio
async def test_inactive_actor_returns_empty_without_calling_jobs(monkeypatch):
    actor_id = uuid.uuid4()
    session = _Session([])

    async def read_actor_status(_self, requested_actor_id):
        assert requested_actor_id == actor_id
        return IdentityActorStatus(
            id=actor_id,
            role="employee",
            is_active=False,
        )

    monkeypatch.setattr(IdentityAccountLockData, "read_actor_status", read_actor_status)

    response = await SqlAsyncJobReadAdapter(session).list_jobs(
        actor_id=actor_id,
        status=None,
        kind=None,
        project_id=None,
        search=None,
        limit=50,
        offset=0,
    )

    assert response.items == []
    assert response.total == 0
    assert session.executed == []
