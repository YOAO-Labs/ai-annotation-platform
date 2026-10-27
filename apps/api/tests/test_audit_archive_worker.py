"""Durable archive job state, redelivery and cancellation boundaries."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.user import User
from app.workers import audit_partition as worker


@pytest.fixture
async def jobs(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    identifiers = []

    @asynccontextmanager
    async def session():
        async with factory() as db:
            yield db

    monkeypatch.setattr(worker, "task_session", session)
    try:
        yield factory, identifiers
    finally:
        async with factory() as db:
            await db.execute(
                delete(AsyncJob).where(AsyncJob.celery_task_id.in_(identifiers))
            )
            await db.commit()


def task_id(jobs):
    value = str(uuid.uuid4())
    jobs[1].append(value)
    return value


async def stored(jobs, identifier):
    async with jobs[0]() as db:
        return await db.scalar(
            select(AsyncJob).where(AsyncJob.celery_task_id == identifier)
        )


def result():
    return {
        "archived_partitions": ["audit_logs_y2001m01"],
        "archive_keys": ["2001/01/synthetic.jsonl.gz"],
        "output_ids": [str(uuid.uuid4())],
        "total_rows": 2,
    }


async def test_running_is_committed_before_service_and_failure_is_durable(
    jobs, monkeypatch
):
    identifier = task_id(jobs)

    async def fail(db, *, retain_months, job_id):
        row = await stored(jobs, identifier)
        assert row is not None and row.id == job_id and row.status == "running"
        assert row.started_at is not None and retain_months == 12
        raise ValueError("private provider credentials must not escape")

    monkeypatch.setattr(worker.AuditPartitionService, "archive_old_partitions", fail)
    with pytest.raises(RuntimeError, match="^audit_archive_failed$"):
        await worker._archive_async(12, identifier)
    row = await stored(jobs, identifier)
    assert row.status == "failed" and row.completed_at is not None
    assert row.error_message == "audit_archive_failed"


async def test_failure_preserves_prior_committed_months(jobs, monkeypatch):
    identifier = task_id(jobs)
    completed = result()

    class ReadbackFailure(ValueError):
        code = "audit_archive_storage_integrity_mismatch"

    async def fail_after_month(db, *, retain_months, job_id):
        row = await db.get(AsyncJob, job_id)
        row.result = deepcopy(completed)
        await db.commit()
        raise ReadbackFailure("private storage response")

    monkeypatch.setattr(
        worker.AuditPartitionService, "archive_old_partitions", fail_after_month
    )
    with pytest.raises(
        RuntimeError, match="^audit_archive_storage_integrity_mismatch$"
    ):
        await worker._archive_async(12, identifier)
    row = await stored(jobs, identifier)
    assert row.status == "failed" and row.result == completed


async def test_duplicate_delivery_never_restarts_running_or_completed_attempt(
    jobs, monkeypatch
):
    identifier = task_id(jobs)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    completed = result()

    async def slow(db, *, retain_months, job_id):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return deepcopy(completed)

    monkeypatch.setattr(worker.AuditPartitionService, "archive_old_partitions", slow)
    original = asyncio.create_task(worker._archive_async(12, identifier))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = await worker._archive_async(12, identifier)
        assert duplicate["deduplicated"] and duplicate["status"] == "running"
        assert (await stored(jobs, identifier)).status == "running"
    finally:
        release.set()
        first = await original
    assert first["status"] == "completed"
    again = await worker._archive_async(12, identifier)
    assert again["deduplicated"] and again["status"] == "completed"
    assert calls == 1
    async with jobs[0]() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AsyncJob)
                .where(AsyncJob.celery_task_id == identifier)
            )
            == 1
        )


@pytest.mark.parametrize("state", ["cancelled", "failed"])
async def test_service_cannot_overwrite_competing_terminal_state(
    jobs, monkeypatch, state
):
    identifier = task_id(jobs)
    committed = result()

    async def already_terminal(db, *, retain_months, job_id):
        row = await db.get(AsyncJob, job_id)
        row.status, row.result = state, deepcopy(committed)
        await db.commit()
        return {**committed, "total_rows": 999}

    monkeypatch.setattr(
        worker.AuditPartitionService, "archive_old_partitions", already_terminal
    )
    returned = await worker._archive_async(12, identifier)
    assert returned["status"] == state and returned["total_rows"] == 2
    row = await stored(jobs, identifier)
    assert row.status == state and row.result == committed


async def test_python_cancellation_records_terminal_and_preserves_partial_result(
    jobs, monkeypatch
):
    identifier = task_id(jobs)
    committed = result()

    async def interrupted(db, *, retain_months, job_id):
        row = await db.get(AsyncJob, job_id)
        row.result = deepcopy(committed)
        await db.commit()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        worker.AuditPartitionService, "archive_old_partitions", interrupted
    )
    with pytest.raises(asyncio.CancelledError):
        await worker._archive_async(12, identifier)
    row = await stored(jobs, identifier)
    assert row.status == "cancelled" and row.result == committed


async def test_unknown_terminal_commit_uses_confirmed_job_state(jobs, monkeypatch):
    identifier = task_id(jobs)
    committed = result()
    factory = jobs[0]

    @asynccontextmanager
    async def session():
        async with factory() as db:
            real_commit = db.commit
            commits = 0

            async def uncertain():
                nonlocal commits
                commits += 1
                await real_commit()
                if commits == 2:
                    raise OSError("commit acknowledgement lost")

            monkeypatch.setattr(db, "commit", uncertain)
            yield db

    async def succeeds(db, *, retain_months, job_id):
        return deepcopy(committed)

    monkeypatch.setattr(worker, "task_session", session)
    monkeypatch.setattr(
        worker.AuditPartitionService, "archive_old_partitions", succeeds
    )
    returned = await worker._archive_async(12, identifier)
    assert returned["status"] == "completed" and returned["total_rows"] == 2
    assert (await stored(jobs, identifier)).status == "completed"


async def test_audit_cancel_keeps_published_months(
    jobs, httpx_client, super_admin, annotator, db_session
):
    identifier = task_id(jobs)
    committed = result()
    async with jobs[0]() as db:
        row = AsyncJob(
            kind="audit_archive",
            status="running",
            celery_task_id=identifier,
            payload={"retain_months": 12},
            result=deepcopy(committed),
        )
        db.add(row)
        await db.commit()
        job_id = row.id
    for actor, expected in [(annotator, 403), (super_admin, 200)]:
        response = await httpx_client.post(
            f"/api/v1/async-jobs/{job_id}/cancel",
            headers={"Authorization": "Bearer " + actor[1]},
        )
        assert response.status_code == expected, response.text
    row = await db_session.scalar(
        select(AsyncJob).where(AsyncJob.celery_task_id == identifier)
    )
    assert row.status == "cancelled" and row.result == committed
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job_id}/cancel",
        headers={"Authorization": "Bearer " + super_admin[1]},
    )
    assert response.status_code == 409


async def test_user_owned_archive_cancel_emits_terminal_notification(
    httpx_client, super_admin, db_session
):
    row = AsyncJob(
        kind="audit_archive",
        status="running",
        user_id=super_admin[0].id,
        payload={},
        result=result(),
    )
    db_session.add(row)
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{row.id}/cancel",
        headers={"Authorization": "Bearer " + super_admin[1]},
    )
    assert response.status_code == 200, response.text
    notification = await db_session.scalar(
        select(Notification).where(
            Notification.target_id == row.id,
            Notification.type == "job.cancelled",
            Notification.user_id == super_admin[0].id,
        )
    )
    assert notification is not None


async def test_terminal_notification_and_owner_cancel_have_consistent_lock_order(
    jobs, monkeypatch
):
    from app.services.async_job_notify import notify_job_terminal

    owner_id = uuid.uuid4()
    identifier = task_id(jobs)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    async with jobs[0]() as db:
        db.add(
            User(
                id=owner_id,
                email=f"{owner_id}@example.invalid",
                name="Archive owner",
                password_hash="test-only",
                role="super_admin",
            )
        )
        await db.commit()

    async def owned_service(db, *, retain_months, job_id):
        row = await db.get(AsyncJob, job_id)
        row.user_id = owner_id
        await db.commit()
        entered.set()
        await release.wait()
        return result()

    monkeypatch.setattr(
        worker.AuditPartitionService, "archive_old_partitions", owned_service
    )
    try:
        task = asyncio.create_task(worker._archive_async(12, identifier))
        await asyncio.wait_for(entered.wait(), 5)
        async with jobs[0]() as cancel:
            await cancel.scalar(
                select(User.id).where(User.id == owner_id).with_for_update()
            )
            cancel_pid = await cancel.scalar(text("SELECT pg_backend_pid()"))
            release.set()

            async def blocked_on_owner():
                async with jobs[0]() as observer:
                    while True:
                        blocked = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND :owner_pid=ANY(pg_blocking_pids(pid)))"
                            ),
                            {"owner_pid": cancel_pid},
                        )
                        if blocked:
                            return
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(blocked_on_owner(), 5)
            row = await cancel.scalar(
                select(AsyncJob)
                .where(AsyncJob.celery_task_id == identifier)
                .with_for_update(nowait=True)
            )
            row.status = "cancelled"
            await cancel.flush()
            await notify_job_terminal(cancel, job_id=row.id)
            await cancel.commit()
        returned = await asyncio.wait_for(task, 5)
        assert returned["status"] == "cancelled"
        async with jobs[0]() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Notification)
                    .where(
                        Notification.user_id == owner_id,
                        Notification.type == "job.cancelled",
                    )
                )
                == 1
            )
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with jobs[0]() as db:
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
