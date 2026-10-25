"""Copy observation never replaces the durability policy for late physical ACKs."""

from copy import deepcopy

import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.user import User
from app.services import storage_migration_copies as copies
from tests.test_storage_migration_copy_worker import (
    add_objects,
    attempt,
    clients as clients,  # noqa: F401
    execute,
    world as world,  # noqa: F401
)


async def delta_world(world, clients, tmp_path):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    async with world.maker() as db:
        row = await copies.owner(db, world.copy_id, private=True)
        path = tmp_path / "checkpoint.sqlite"
        path.write_bytes(row.baseline)
        path.chmod(0o600)
        with copies.Checkpoint(path) as checkpoint:
            data = checkpoint.decode(
                *checkpoint.db.execute(
                    "SELECT data,digest FROM results LIMIT 1"
                ).fetchone()
            )
        path.unlink()
    await attempt(world, "run")
    async with world.maker() as db:
        assert await copies.claim(db, world.job_id, world.task_id)
        await db.commit()
    return {**data, "state": "writing", "nonce": "a" * 32}


async def test_prepared_baseline_records_work_before_terminal_once(world):
    assert await execute(world)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        assert job.status == "completed"
        assert job.progress_observation["stage"] == "preparing"
        assert job.progress_observation["completed"] == 0
        at, observation = job.last_progress_at, deepcopy(job.progress_observation)
    assert not await execute(world)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        assert (job.last_progress_at, job.progress_observation) == (at, observation)


async def test_changed_copy_delta_advances_same_percent_duplicate_does_not(
    world, clients, tmp_path
):
    data = await delta_world(world, clients, tmp_path)
    async with world.maker() as db:
        await copies.save_delta(
            db, world.job_id, world.task_id, data, copies.row_digest(data)
        )
        job = await db.get(AsyncJob, world.job_id)
        assert job.progress_pct == 0
        assert job.progress_observation["sequence"] == 1
        at, observation = job.last_progress_at, deepcopy(job.progress_observation)
        await copies.save_delta(
            db, world.job_id, world.task_id, data, copies.row_digest(data)
        )
        assert (job.last_progress_at, job.progress_observation) == (at, observation)
        changed = {**data, "state": "uncertain", "error": "put_outcome_unknown"}
        await copies.save_delta(
            db,
            world.job_id,
            world.task_id,
            changed,
            copies.row_digest(changed),
        )
        assert job.progress_observation["sequence"] == 3 and job.progress_pct == 0


@pytest.mark.parametrize("late", ["terminal", "revoked", "observation_failure"])
async def test_late_or_unobservable_ack_stays_durable_without_refresh(
    world, clients, tmp_path, monkeypatch, late
):
    data = await delta_world(world, clients, tmp_path)
    async with world.maker() as db:
        if late == "terminal":
            a = await copies.by_job(db, world.job_id)
            job = await db.get(AsyncJob, world.job_id)
            a.status = job.status = "failed"
        elif late == "revoked":
            actor = await db.get(User, world.actor_id)
            actor.role = "annotator"
        else:

            async def broken(*args, **kwargs):
                raise RuntimeError("observation unavailable")

            monkeypatch.setattr(copies.progress, "record", broken)
        await db.commit()
        await copies.save_delta(
            db, world.job_id, world.task_id, data, copies.row_digest(data)
        )
    async with world.maker() as db:
        saved = await db.get(copies.Delta, (world.copy_id, data["id"]))
        job = await db.get(AsyncJob, world.job_id)
        assert saved.data == data
        assert job.last_progress_at is None and job.progress_observation is None


async def test_copy_owner_commit_failure_rolls_back_receipt_and_observation(
    world, clients, tmp_path, monkeypatch
):
    data = await delta_world(world, clients, tmp_path)
    async with world.maker() as db:

        async def fail():
            raise RuntimeError("copy commit failed")

        monkeypatch.setattr(db, "commit", fail)
        with pytest.raises(RuntimeError, match="copy commit failed"):
            await copies.save_delta(
                db, world.job_id, world.task_id, data, copies.row_digest(data)
            )
        await db.rollback()
    async with world.maker() as db:
        assert await db.get(copies.Delta, (world.copy_id, data["id"])) is None
        job = await db.get(AsyncJob, world.job_id)
        assert job.last_progress_at is None and job.progress_observation is None
