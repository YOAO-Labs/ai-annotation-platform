"""A late API broker response cannot replace a claimed export attempt."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import async_job
from app.services.jobs_dispatch import jobs_dispatch_data, prepare_job_dispatch
from app.services.jobs_lifecycle_data import job_lock_statement
from tests.factory import create_project, create_user
from yoao_jobs.dispatch_contract import JobsDispatchRejected


async def _prepare_export(db, project):
    job = await async_job.create_job(
        db, kind="export", project_id=project.id, user_id=project.owner_id
    )
    lease = await prepare_job_dispatch(
        db,
        job_id=job.id,
        task_name="app.workers.export.run_export",
        args=[],
        kwargs={"project_id": str(project.id), "async_job_id": str(job.id)},
    )
    return job, lease


@pytest.mark.parametrize("claimed", [False, True])
@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "cancelled"]
)
async def test_late_dispatch_ack_only_updates_the_intent(db_session, claimed, status):
    db = db_session
    owner = await create_user(
        db, "project_admin", "export-dispatch@example.test", "Owner"
    )
    project = await create_project(db, owner_id=owner.id)
    job, lease = await _prepare_export(db, project)
    task_id = lease.execution_id if claimed else None
    async_job.bind_execution_id(job, task_id)
    if status == "running":
        await async_job.mark_running(db, job.id)
    elif status == "completed":
        await async_job.mark_complete(db, job.id)
    elif status == "failed":
        await async_job.mark_failed(db, job.id, error="worker failed")
    elif status == "cancelled":
        await async_job.mark_cancelled(db, job.id)
    await db.flush()

    data = jobs_dispatch_data(db)
    assert (await data.finish(lease, accepted=True)).state == "submitted"
    with pytest.raises(JobsDispatchRejected, match="stale_lease"):
        await data.finish(lease, accepted=True)
    current = await async_job.get_job(db, job.id, populate_existing=True)
    assert current.status == status
    assert current.celery_task_id == task_id


async def test_worker_claim_lock_does_not_block_or_reassign_late_ack(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    async with sessions.begin() as setup:
        owner = await create_user(
            setup, "project_admin", f"export-dispatch-{nonce}@example.test", "Owner"
        )
        project = await create_project(setup, owner_id=owner.id)
        job, lease = await _prepare_export(setup, project)

    try:
        async with sessions.begin() as worker:
            running = await worker.scalar(
                job_lock_statement(job.id, mode="update_nowait", populate_existing=True)
            )
            await async_job.mark_running(
                worker, running.id, celery_task_id=lease.execution_id
            )
            await worker.flush()
            async with sessions.begin() as api:
                receipt = await asyncio.wait_for(
                    jobs_dispatch_data(api).finish(lease, accepted=True), timeout=2
                )
                assert receipt.state == "submitted"
        async with sessions() as reader:
            current = await async_job.get_job(reader, job.id)
            assert (current.status, current.celery_task_id) == (
                "running",
                lease.execution_id,
            )
    finally:
        async with sessions.begin() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.async_jobs WHERE id = :id"), {"id": job.id}
            )
            await cleanup.execute(
                text("DELETE FROM public.projects WHERE id = :id"), {"id": project.id}
            )
            await cleanup.execute(
                text("DELETE FROM public.users WHERE id = :id"), {"id": owner.id}
            )
