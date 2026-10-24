"""Pure guards for the cross-frame Jobs queue adapter boundary."""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest
from fastapi import HTTPException

from app.api.v1.tasks import cross_frame_jobs
from app.db.models.async_job import AsyncJobStatus
from app.services.cross_frame_job import JOB_KIND
from app.workers import cross_frame_job


class _FakeSession:
    def __init__(self, job):
        self.job = job
        self.commits = 0
        self.committed_execution_ids = []

    async def commit(self):
        self.commits += 1
        self.committed_execution_ids.append(self.job.celery_task_id)

    async def get(self, _model, job_id, *, populate_existing=False):
        assert job_id == self.job.id
        return self.job


def _install_queue(monkeypatch, dispatch_job):
    module = ModuleType("app.services.jobs_queue")
    module.dispatch_job = dispatch_job
    monkeypatch.setitem(sys.modules, "app.services.jobs_queue", module)


@pytest.mark.asyncio
async def test_dispatch_predeclares_execution_id_and_preserves_existing_binding(
    monkeypatch,
):
    job = SimpleNamespace(id=uuid.uuid4(), celery_task_id="legacy-execution-id")
    db = _FakeSession(job)
    requests = []

    async def dispatch_job(**request):
        requests.append(request)
        assert db.commits == 1
        return "broker-response-id"

    _install_queue(monkeypatch, dispatch_job)

    result = await cross_frame_jobs._dispatch_job(db, job)

    assert result is job
    assert job.celery_task_id == "legacy-execution-id"
    assert requests == [
        {
            "task_name": "app.workers.cross_frame_job.run_cross_frame_job",
            "execution_id": "legacy-execution-id",
            "args": [str(job.id)],
        }
    ]
    assert db.commits == 1
    assert db.committed_execution_ids == ["legacy-execution-id"]


@pytest.mark.asyncio
async def test_unknown_dispatch_result_does_not_overwrite_a_claimed_job(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(), celery_task_id=None, status=AsyncJobStatus.PENDING.value
    )
    db = _FakeSession(job)
    requests = []
    failed_updates = []

    async def dispatch_job(**request):
        requests.append(request)
        assert db.commits == 1
        assert job.celery_task_id == request["execution_id"]
        # Model the broker accepting the task and the worker claiming it before
        # the producer receives an ambiguous timeout.
        job.status = AsyncJobStatus.RUNNING.value
        raise TimeoutError("reply lost after broker acceptance")

    async def mark_dispatch_failed(session, job_id, *, error):
        failed_updates.append((session, job_id, error))
        assert job.status == AsyncJobStatus.RUNNING.value
        return False

    notified = []

    async def notify_job_terminal(_db, *, job_id):
        notified.append(job_id)

    _install_queue(monkeypatch, dispatch_job)
    monkeypatch.setattr(
        cross_frame_jobs.async_job_svc, "mark_dispatch_failed", mark_dispatch_failed
    )
    monkeypatch.setattr(cross_frame_jobs, "notify_job_terminal", notify_job_terminal)

    with pytest.raises(HTTPException) as raised:
        await cross_frame_jobs._dispatch_job(db, job)

    assert raised.value.status_code == 503
    assert raised.value.detail == "cross-frame worker is unavailable"
    assert len(requests) == 1
    assert requests[0]["task_name"] == "app.workers.cross_frame_job.run_cross_frame_job"
    assert requests[0]["args"] == [str(job.id)]
    assert uuid.UUID(requests[0]["execution_id"])
    assert job.celery_task_id == requests[0]["execution_id"]
    assert failed_updates == [(db, job.id, "cross-frame job dispatch failed")]
    assert notified == []
    assert job.status == AsyncJobStatus.RUNNING.value
    assert db.commits == 2
    assert db.committed_execution_ids == [requests[0]["execution_id"]] * 2


@pytest.mark.asyncio
async def test_dispatch_failure_notifies_only_after_pending_transition(monkeypatch):
    job = SimpleNamespace(
        id=uuid.uuid4(), celery_task_id=None, status=AsyncJobStatus.PENDING.value
    )
    db = _FakeSession(job)
    notified = []

    async def dispatch_job(**_request):
        raise TimeoutError("broker unavailable before acceptance")

    async def mark_dispatch_failed(session, job_id, *, error):
        assert session is db
        assert job_id == job.id
        assert error == "cross-frame job dispatch failed"
        assert job.status == AsyncJobStatus.PENDING.value
        job.status = AsyncJobStatus.FAILED.value
        return True

    async def notify_job_terminal(_db, *, job_id):
        notified.append(job_id)

    _install_queue(monkeypatch, dispatch_job)
    monkeypatch.setattr(
        cross_frame_jobs.async_job_svc, "mark_dispatch_failed", mark_dispatch_failed
    )
    monkeypatch.setattr(cross_frame_jobs, "notify_job_terminal", notify_job_terminal)

    with pytest.raises(HTTPException) as raised:
        await cross_frame_jobs._dispatch_job(db, job)

    assert raised.value.status_code == 503
    assert job.status == AsyncJobStatus.FAILED.value
    assert notified == [job.id]
    assert db.commits == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        AsyncJobStatus.COMPLETED.value,
        AsyncJobStatus.FAILED.value,
        AsyncJobStatus.CANCELLED.value,
    ],
)
async def test_worker_rejects_terminal_job_before_running_or_domain_work(
    monkeypatch, status
):
    job = SimpleNamespace(id=uuid.uuid4(), kind=JOB_KIND, status=status)
    db = _FakeSession(job)
    marked_running = []

    async def mark_running(*_args, **_kwargs):
        marked_running.append(True)
        raise AssertionError("terminal job must be rejected before mark_running")

    monkeypatch.setattr(cross_frame_job.async_job_svc, "mark_running", mark_running)

    await cross_frame_job.execute_cross_frame_job(
        db, job_id=job.id, celery_task_id="late-delivery"
    )

    assert marked_running == []
    assert db.commits == 0
