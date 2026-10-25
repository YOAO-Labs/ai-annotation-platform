"""Independent transactions fence concurrent observations and retained schema."""

import asyncio
import copy
import importlib.util
from pathlib import Path
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.services import storage_job_progress as progress
from tests.test_storage_job_progress import observe, public_job, running


async def test_independent_concurrent_transactions_increment_under_job_lock(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await running(db)
        job_id, task_id = job.id, job.celery_task_id
        await db.commit()

    async def callback(marker):
        async with maker() as db:
            current = await db.get(AsyncJob, job_id)
            changed = await observe(db, current, marker=marker)
            await db.commit()
            return changed

    try:
        assert await asyncio.gather(callback("a"), callback("b")) == [True, True]
        async with maker() as db:
            current = await db.get(AsyncJob, job_id)
            assert current.progress_observation["sequence"] == 2
            assert current.progress_observation["execution_id"] == str(
                progress.execution_id(job_id, task_id)
            )
    finally:
        async with maker() as db:
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
            await db.commit()


async def test_new_task_does_not_inherit_or_accept_previous_execution(db_session):
    job = await running(db_session)
    old_task = job.celery_task_id
    assert await observe(db_session, job, sequence=10)
    old = copy.deepcopy(job.progress_observation)
    job.celery_task_id = str(uuid.uuid4())
    await db_session.flush()
    assert progress.public(
        job.id,
        job.kind,
        job.celery_task_id,
        job.last_progress_at,
        old,
        observed_at=job.last_progress_at,
    ) == (None, None)
    assert not await observe(
        db_session, job, task_id=old_task, marker="late", sequence=11
    )
    assert await observe(db_session, job, marker="new-task", sequence=1)
    assert job.progress_observation["sequence"] == 1
    assert job.progress_observation["execution_id"] != old["execution_id"]


def test_other_job_cannot_reuse_even_same_task_observation():
    job = public_job()
    assert not progress.valid(
        job.progress_observation, uuid.uuid4(), job.kind, job.celery_task_id
    )


@pytest.mark.parametrize("field", ["last_progress_at", "progress_observation"])
async def test_downgrade_preserves_any_new_observation_fact(db_session, field):
    from datetime import datetime, timezone

    job = await running(db_session)
    setattr(
        job,
        field,
        datetime.now(timezone.utc)
        if field == "last_progress_at"
        else {"retained": True},
    )
    await db_session.flush()
    path = Path("alembic/versions/0259_storage_job_progress.py")
    spec = importlib.util.spec_from_file_location("progress_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(session):
        with Operations.context(MigrationContext.configure(session.connection())):
            module.downgrade()

    with pytest.raises(RuntimeError, match="keep schema"):
        await db_session.run_sync(downgrade)
    # The rejection precedes DDL, and the transaction remains usable.
    assert (
        await db_session.execute(
            select(AsyncJob.last_progress_at, AsyncJob.progress_observation).where(
                AsyncJob.id == job.id
            )
        )
    ).one()
