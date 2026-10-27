"""Real native work observations remain optional, bounded and fully owned."""

import asyncio
import io
import threading
import uuid

import pytest

from app.db.models.async_job import AsyncJob
from app.db.models.storage_maintenance import StorageOperation
from app.services import storage_migration_inventory as native
from app.services import storage_migration_inventories as service
from app.workers import storage_migration_inventory as worker
from tests.test_storage_migration_inventories import EmptySource
from tests.test_storage_migration_inventory_worker import (
    execute,
    fake_storage,
    world as world,
)


def rendered(monkeypatch, client, *, on_progress=None):
    monkeypatch.setattr(native, "_now", lambda: "2026-10-26T00:00:00+00:00")
    sink = worker.ReportSink()
    native.write_inventory(
        None,
        client,
        {purpose: "one" for purpose in native.PURPOSES},
        sink=sink,
        on_progress=on_progress,
    )
    return sink.value


def test_actual_empty_pages_advance_and_callback_does_not_change_report_bytes(
    monkeypatch,
):
    plain, events = rendered(monkeypatch, EmptySource()), []
    observed = rendered(monkeypatch, EmptySource(), on_progress=events.append)
    assert plain == observed
    assert events == [
        {"sequence": 1, "unit": "pages", "completed": 1},
        {"sequence": 2, "unit": "pages", "completed": 2},
    ]


def test_observer_errors_never_turn_successful_report_into_failure(monkeypatch):
    calls = []

    def broken(event):
        calls.append(event)
        raise RuntimeError("private observation failure")

    value = rendered(monkeypatch, EmptySource(), on_progress=broken)
    assert calls and native.verify_inventory(stream=io.BytesIO(value))["complete"]


def test_guard_polling_and_rejected_pages_are_not_work_observations(monkeypatch):
    source, events, polls = EmptySource(), [], []
    source.list_objects_v2 = lambda **kwargs: {"Contents": []}
    source.list_multipart_uploads = lambda **kwargs: {"Uploads": []}
    sink = worker.ReportSink()
    native.write_inventory(
        None,
        source,
        {purpose: "one" for purpose in native.PURPOSES},
        sink=sink,
        checkpoint=lambda: polls.append(1),
        on_progress=events.append,
    )
    assert polls and not events
    assert not native.verify_inventory(stream=io.BytesIO(sink.value))["complete"]


def test_object_observation_follows_full_readback_and_body_close(monkeypatch):
    client, events, bodies = EmptySource(), [], []
    client.list_objects_v2 = lambda **kwargs: {
        "IsTruncated": False,
        "Contents": [{"Key": "private-key", "ETag": '"etag"', "Size": 3}],
    }
    head = {
        "ResponseMetadata": {"HTTPStatusCode": 200},
        "ContentLength": 3,
        "ETag": '"etag"',
    }
    client.head_object = lambda **kwargs: dict(head)

    def get(**kwargs):
        body = io.BytesIO(b"abc")
        bodies.append(body)
        return {**head, "Body": body}

    client.get_object = get

    def observed(event):
        if event["unit"] == "records":
            assert bodies and all(body.closed for body in bodies)
        events.append(event)

    value = rendered(monkeypatch, client, on_progress=observed)
    assert (
        native.verify_inventory(stream=io.BytesIO(value))["counts"]["verified_objects"]
        == 1
    )
    assert events[0] == {"sequence": 1, "unit": "records", "completed": 1}
    assert "private-key" not in str(events)


async def test_native_single_slot_never_waits_for_slow_observer_and_final_flush(
    monkeypatch,
):
    entered, release, observed = asyncio.Event(), asyncio.Event(), []

    async def slow(bind, job_id, task_id, event):
        observed.append(event)
        entered.set()
        await release.wait()

    monkeypatch.setattr(worker, "_observe", slow)
    owner = worker._Progress(
        None, asyncio.get_running_loop(), threading.Event(), uuid.uuid4(), "task"
    )
    owner({"sequence": 1, "unit": "records", "completed": 1})
    await asyncio.wait_for(entered.wait(), 5)

    def produce():
        for sequence in range(2, 10002):
            owner({"sequence": sequence, "unit": "records", "completed": sequence})

    try:
        await asyncio.wait_for(asyncio.to_thread(produce), 5)
        assert len(observed) == 1 and not owner.task.done()
        closing = asyncio.create_task(owner.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 5)
        assert [event["sequence"] for event in observed] == [1, 10001]
        assert owner.pending is None and owner.incoming is None and owner.task.done()
    finally:
        release.set()
        if not owner.task.done():
            await owner.close()


async def test_observer_is_throttled_but_final_close_flushes_latest(monkeypatch):
    observed, entered = [], asyncio.Event()

    async def record(*args):
        observed.append(args[-1])
        entered.set()

    monkeypatch.setattr(worker, "_observe", record)
    owner = worker._Progress(
        None, asyncio.get_running_loop(), threading.Event(), uuid.uuid4(), "task"
    )
    try:
        owner({"sequence": 1, "unit": "pages", "completed": 1})
        await asyncio.wait_for(entered.wait(), 5)
        for n in range(2, 12):
            owner({"sequence": n, "unit": "pages", "completed": n})
            await asyncio.sleep(0)
        assert len(observed) == 1
    finally:
        await owner.close()
    assert [e["sequence"] for e in observed] == [1, 11]


async def test_real_native_progress_is_visible_to_independent_readers_while_running(
    world, monkeypatch
):
    client = fake_storage(monkeypatch, world.source)
    entered, release = threading.Event(), threading.Event()

    def blocked(**kwargs):
        entered.set()
        assert release.wait(15)
        return {"IsTruncated": False, "Uploads": []}

    client.list_multipart_uploads = blocked
    task = asyncio.create_task(execute(world))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        for _ in range(100):
            async with world.maker() as db:
                job = await db.get(AsyncJob, world.job_id)
                if job.last_progress_at is not None:
                    break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("actual accepted page did not become independently visible")
        assert job.status == "running" and job.progress_pct == 0
        assert job.progress_observation["unit"] == "pages"
        first = (job.last_progress_at, job.progress_observation)
        for _ in range(3):
            async with world.maker() as db:
                await service.checkpoint(db, world.job_id, world.task_id)
                current = await db.get(AsyncJob, world.job_id)
                assert (current.last_progress_at, current.progress_observation) == first
        release.set()
        assert await task
        async with world.maker() as db:
            current = await db.get(AsyncJob, world.job_id)
            assert current.status == "completed"
            assert current.progress_observation["sequence"] >= first[1]["sequence"]
            assert (
                await db.get(
                    StorageOperation,
                    (await service.by_job(db, world.job_id)).operation_id,
                )
                is None
            )
    finally:
        release.set()
        if not task.done():
            await worker.storage_access.finish(task)


@pytest.mark.parametrize("changed", ["task", "cancel", "role", "terminal"])
async def test_observation_rechecks_actual_inventory_owner(world, changed):
    async with world.maker() as db:
        assert await service.claim(db, world.job_id, world.task_id)
        await db.commit()
        row = await service.by_job(db, world.job_id)
        job = await db.get(AsyncJob, world.job_id)
        if changed == "task":
            job.celery_task_id = "other"
        elif changed == "cancel":
            await service.cancel(db, world.job_id)
        elif changed == "terminal":
            await service.finish(
                db, row, job, "failed", error_code="inventory_read_failed"
            )
        else:
            from app.db.models.user import User

            (await db.get(User, world.actor_id)).role = "project_admin"
        await db.commit()
        bind = worker.storage_access.engine(db)
    try:
        await worker._observe(
            bind,
            world.job_id,
            world.task_id,
            {"sequence": 1, "unit": "pages", "completed": 1},
        )
    except Exception:
        assert changed == "role"
    async with world.maker() as db:
        assert (await db.get(AsyncJob, world.job_id)).last_progress_at is None


async def test_ordinary_observer_failure_preserves_successful_worker_result(
    world, monkeypatch
):
    fake_storage(monkeypatch, world.source)

    async def broken(*args):
        raise RuntimeError("private observation outage")

    monkeypatch.setattr(worker, "_observe", broken)
    assert await execute(world)
    async with world.maker() as db:
        row, job = (
            await service.by_job(db, world.job_id),
            await db.get(AsyncJob, world.job_id),
        )
        assert row.status == job.status == "completed" and row.complete
        assert job.last_progress_at is None and job.progress_observation is None


async def test_new_cancel_during_observer_close_after_native_failure_is_not_suppressed(
    world, monkeypatch
):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def observe(*args):
        entered.set()
        try:
            await release.wait()
        finally:
            finished.set()

    def failed(*args, on_progress=None):
        on_progress({"sequence": 1, "unit": "pages", "completed": 1})
        raise native.InventoryStopped("inventory_time_budget")

    monkeypatch.setattr(worker, "_observe", observe)
    monkeypatch.setattr(worker, "scan", failed)
    task = asyncio.create_task(execute(world))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        async with world.maker() as db:
            row = await service.by_job(db, world.job_id)
            assert row.status == "cancelled"
            assert await db.get(StorageOperation, row.operation_id) is None
    finally:
        release.set()
        if not task.done():
            await worker.storage_access.finish(task)
