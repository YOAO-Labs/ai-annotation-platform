from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.application.quality_execution import _AsyncJobLifecycle
from app.db.models.async_job import AsyncJobStatus
from app.db.models.notification import Notification
from app.services import async_job as async_job_svc
from app.services.notification import NotificationService


@pytest.mark.asyncio
async def test_quality_job_lifecycle_shares_caller_transaction(
    db_session, annotator, monkeypatch
):
    user, _token = annotator

    async def skip_external_publish(self, _notifications):
        return None

    monkeypatch.setattr(NotificationService, "publish_committed", skip_external_publish)

    job = await async_job_svc.create_job(db_session, kind="mask_qc", user_id=user.id)
    lifecycle = _AsyncJobLifecycle(db_session)

    rolled_back = await db_session.begin_nested()
    await lifecycle.mark_running(job.id, celery_task_id="rolled-back-task")
    await lifecycle.update_progress(job.id, 37)
    await lifecycle.mark_complete(job.id, result={"run_id": str(uuid.uuid4())})
    await lifecycle.notify_terminal(job_id=job.id)
    assert (
        await db_session.execute(
            select(Notification).where(Notification.target_id == job.id)
        )
    ).scalar_one().type == "job.completed"

    await rolled_back.rollback()
    await db_session.refresh(job)
    assert job.status == AsyncJobStatus.PENDING.value
    assert job.progress_pct == 0
    assert (
        await db_session.execute(
            select(Notification).where(Notification.target_id == job.id)
        )
    ).scalar_one_or_none() is None

    committed = await db_session.begin_nested()
    await lifecycle.mark_running(job.id, celery_task_id="committed-task")
    await lifecycle.update_progress(job.id, 37)
    result = {"run_id": str(uuid.uuid4())}
    await lifecycle.mark_complete(job.id, result=result)
    await lifecycle.notify_terminal(job_id=job.id)
    await committed.commit()

    await db_session.refresh(job)
    notification = (
        await db_session.execute(
            select(Notification).where(Notification.target_id == job.id)
        )
    ).scalar_one()
    assert job.status == AsyncJobStatus.COMPLETED.value
    assert job.progress_pct == 100
    assert job.celery_task_id == "committed-task"
    assert job.result == result
    assert notification.type == "job.completed"
    assert notification.payload["run_id"] == result["run_id"]
