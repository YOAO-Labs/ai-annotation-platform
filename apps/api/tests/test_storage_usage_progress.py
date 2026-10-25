"""Only successful fenced capacity pages count as committed work."""

from copy import deepcopy
from datetime import timedelta
import uuid

import pytest

from app.db.models.async_job import AsyncJob
from app.services import async_job as jobs, storage_usage as usage
from app.workers import storage_usage as worker
from tests.test_storage_usage import ready as ready  # noqa: F401

real_list_page = worker.list_page


async def running(ready):
    db, actor = ready
    job, _ = await usage.start(db, actor)
    inventory = await usage.locked(db)
    inventory.status = "running"
    inventory.lease_until = usage.now() + timedelta(seconds=usage.LEASE_SECONDS)
    await jobs.mark_running(db, job.id)
    await db.commit()
    return db, job, inventory


async def test_capacity_pages_advance_at_same_percent_and_bad_page_does_not(
    ready, monkeypatch
):
    db, job, inventory = await running(ready)
    pages = iter(
        [
            {"count": 0, "bytes": 0, "token": "next", "objects": []},
            {"count": 0, "bytes": 0, "token": "third", "objects": []},
            {"count": 1, "bytes": 0, "token": None, "objects": []},
        ]
    )
    monkeypatch.setattr(worker, "list_page", lambda *_: next(pages))
    assert job.last_progress_at is None
    for expected in (1, 2):
        assert await worker.process_page(
            db, job.id, job.celery_task_id, inventory.generation
        )
        assert job.progress_pct == 0
        assert job.progress_observation["completed"] == expected
        assert job.progress_observation["sequence"] == expected
    at, observation = job.last_progress_at, deepcopy(job.progress_observation)
    assert await worker.process_page(
        db, job.id, job.celery_task_id, inventory.generation
    )
    assert inventory.failed_count == 1
    assert (job.last_progress_at, job.progress_observation) == (at, observation)


@pytest.mark.parametrize("stale", ["task", "generation", "terminal"])
async def test_capacity_stale_owner_does_not_list_or_progress(
    ready, monkeypatch, stale
):
    db, job, inventory = await running(ready)
    task_id, generation = job.celery_task_id, inventory.generation
    if stale == "task":
        task_id = str(uuid.uuid4())
    elif stale == "generation":
        generation -= 1
    else:
        job.status = "failed"
        await db.commit()
    monkeypatch.setattr(
        worker, "list_page", lambda *_: pytest.fail("stale owner listed")
    )
    assert not await worker.process_page(db, job.id, task_id, generation)
    await db.refresh(job)
    assert job.last_progress_at is None and job.progress_observation is None


async def test_real_minio_capacity_page_and_progress_commit_together(
    ready, monkeypatch
):
    db, job, inventory = await running(ready)
    monkeypatch.setattr(worker, "list_page", real_list_page)
    target = inventory.scope[0]
    key = f"progress-{uuid.uuid4()}.bin"
    client = worker._client()
    client.put_object(Bucket=target["name"], Key=key, Body=b"capacity-progress")
    try:
        assert await worker.process_page(
            db, job.id, job.celery_task_id, inventory.generation
        )
        # A fresh ORM read sees the same accepted page's checkpoint and observation.
        await db.refresh(job)
        assert job.progress_observation["unit"] == "pages"
        assert job.progress_observation["completed"] == 1
        snapshot = await db.get(worker.Snapshot, target["key"], populate_existing=True)
        assert snapshot.object_count >= 1 and snapshot.total_size_bytes >= len(
            b"capacity-progress"
        )
        assert snapshot.attempt_status == "completed"
    finally:
        client.delete_object(Bucket=target["name"], Key=key)


async def test_capacity_owner_final_commit_failure_rolls_back_progress(
    ready, monkeypatch
):
    db, job, inventory = await running(ready)
    job_id, task_id, generation = job.id, job.celery_task_id, inventory.generation
    original = db.commit
    commits = 0

    async def commit():
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("owner final commit failed")
        await original()

    monkeypatch.setattr(db, "commit", commit)
    with pytest.raises(RuntimeError, match="owner final commit failed"):
        await worker.process_page(db, job_id, task_id, generation)
    await db.rollback()
    stored = await db.get(AsyncJob, job_id, populate_existing=True)
    assert stored.last_progress_at is None and stored.progress_observation is None
