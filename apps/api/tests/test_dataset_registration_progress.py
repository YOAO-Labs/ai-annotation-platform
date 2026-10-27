"""Only an accepted entry transition is registration progress."""

from copy import deepcopy

import pytest

from app.services import dataset_registration as registration
from tests.test_dataset_registration import (
    begin_confirmed,
    content_world as content_world,  # noqa: F401
    drain,
    start,
)


async def test_registration_transition_and_failure_fact_are_progress(
    db_session, content_world
):
    run, task_id, fence = await start(db_session, content_world)
    job = await db_session.get(registration.AsyncJob, run.async_job_id)
    assert job.last_progress_at is None
    work = await registration.next_entry(db_session, run.id, task_id, fence)
    assert job.last_progress_at is None
    await registration.record(
        db_session,
        run.id,
        task_id,
        fence,
        work["path_digest"],
        "error",
        error_code="image_unreadable",
    )
    assert job.progress_observation["stage"] == "preflight"
    assert job.progress_observation["unit"] == "entries"
    assert job.progress_observation["completed"] == 1
    assert run.counts["error_count"] == 1
    at, observation = job.last_progress_at, deepcopy(job.progress_observation)
    with pytest.raises(registration.LostRegistrationLease):
        await registration.record(
            db_session, run.id, task_id, fence, work["path_digest"], "error"
        )
    assert (job.last_progress_at, job.progress_observation) == (at, observation)


async def test_registration_entry_and_progress_rollback_together(
    db_session, content_world
):
    run, task_id, fence = await start(db_session, content_world)
    job = await db_session.get(registration.AsyncJob, run.async_job_id)
    work = await registration.next_entry(db_session, run.id, task_id, fence)
    with pytest.raises(RuntimeError):
        async with db_session.begin_nested():
            await registration.record(
                db_session, run.id, task_id, fence, work["path_digest"], "error"
            )
            raise RuntimeError("publication rolled back")
    await db_session.refresh(job)
    await db_session.refresh(run)
    assert job.last_progress_at is None and run.counts["pending_count"] == 1


async def test_confirmed_registration_observes_new_execution_and_real_entry(
    db_session, content_world
):
    run, task_id, fence = await start(db_session, content_world)
    await drain(db_session, run, task_id, fence)
    old = await db_session.get(registration.AsyncJob, run.async_job_id)
    before = deepcopy(old.progress_observation)
    assert before["stage"] == "preflight"
    task_id, fence = await begin_confirmed(db_session, run, content_world)
    job = await db_session.get(registration.AsyncJob, run.async_job_id)
    assert job.id != old.id and job.last_progress_at is None
    await drain(db_session, run, task_id, fence)
    assert job.status == "completed"
    assert job.progress_observation["stage"] == "registering"
    assert job.progress_observation["completed"] == run.counts["registered_count"] == 1
    assert job.progress_observation["execution_id"] != before["execution_id"]
