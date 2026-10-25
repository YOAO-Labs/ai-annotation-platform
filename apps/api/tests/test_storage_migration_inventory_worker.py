"""Committed independent sessions prove claim races, cancellation and real drain."""

import asyncio
from contextlib import asynccontextmanager
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from botocore.hooks import HierarchicalEmitter
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.user import User
from app.services import storage_migration_inventories as service
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage_migration_inventory import InventoryStopped
from app.workers import storage_migration_inventory as worker
from tests.factory import create_user
from tests.test_storage_migration_inventories import EmptySource


@pytest.fixture
async def world(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        await ensure_legacy_bindings(db)
        actor = await create_user(
            db, "super_admin", f"inventory-{uuid.uuid4()}@test.local", "Inventory"
        )
        actor_id = actor.id
        row, job, _ = await service.start(db, actor)
        job_id, task_id = job.id, row.task_id
        await db.commit()
    try:
        yield SimpleNamespace(
            maker=maker,
            job_id=job_id,
            task_id=task_id,
            actor_id=actor_id,
            source=row.source,
        )
    finally:
        async with maker() as db:
            row = await service.by_job(db, job_id)
            if row and row.operation_id:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.id == row.operation_id
                    )
                )
            await db.execute(
                delete(Notification).where(Notification.target_id == job_id)
            )
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id == actor_id,
                    AuditLog.action == "storage.migration_inventory_start",
                )
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()


def fake_storage(monkeypatch, source, *, close_error=False):
    client = EmptySource()
    client.meta = SimpleNamespace(
        endpoint_url=source["endpoint"], events=HierarchicalEmitter()
    )
    client.closed = False

    def close():
        client.closed = True
        if close_error:
            raise RuntimeError("private client credential")

    client.close = close
    configured = SimpleNamespace(
        client=client,
        **{
            attribute: source["purpose_buckets"][purpose]
            for purpose, attribute in service.PURPOSE_BUCKET_ATTRIBUTES.items()
        },
    )
    monkeypatch.setattr(worker, "StorageService", lambda **kwargs: configured)
    return client


async def execute(world):
    async with world.maker() as db:
        return await worker.execute(db, world.job_id, world.task_id)


async def test_two_independent_deliveries_run_core_once_and_close_client(
    world, monkeypatch
):
    client = fake_storage(monkeypatch, world.source)
    actual, calls = worker.scan, []

    def counted(*args, **kwargs):
        calls.append(1)
        return actual(*args, **kwargs)

    monkeypatch.setattr(worker, "scan", counted)
    results = await asyncio.gather(execute(world), execute(world))
    assert results.count(True) == 1 and calls == [1]
    assert client.closed
    async with world.maker() as db:
        row = await service.by_job(db, world.job_id)
        assert row.status == "completed" and row.complete
        assert row.counts["buckets"] == len(
            set(world.source["purpose_buckets"].values())
        )
        assert await db.get(StorageOperation, row.operation_id) is None
        assert service.item(row).report_available


async def test_unknown_client_close_keeps_receipt_and_never_report(world, monkeypatch):
    client = fake_storage(monkeypatch, world.source, close_error=True)
    assert await execute(world)
    assert client.closed
    async with world.maker() as db:
        row = await service.by_job(db, world.job_id)
        assert (
            row.status == "failed" and row.error_code == "storage_operation_unsettled"
        )
        assert await db.get(StorageOperation, row.operation_id)
        assert not service.item(row).report_available


@pytest.mark.parametrize("budget", ["time", "output"])
async def test_actual_app_budget_failure_closes_and_has_no_report(
    world, monkeypatch, budget
):
    client = fake_storage(monkeypatch, world.source)
    if budget == "time":
        clock = iter([0, 10000])
        monkeypatch.setattr(
            worker, "time", SimpleNamespace(monotonic=lambda: next(clock))
        )
    else:
        monkeypatch.setattr(worker, "REPORT_LIMIT", 32)
    assert await execute(world)
    async with world.maker() as db:
        row = await service.by_job(db, world.job_id)
        assert row.status == "failed"
        assert row.error_code == (
            "inventory_time_budget"
            if budget == "time"
            else "inventory_report_byte_budget"
        )
        assert await db.get(StorageOperation, row.operation_id) is None
        assert not service.item(row).report_available
    if budget == "output":
        assert client.closed


async def test_publish_failure_and_redelivery_do_not_repeat_source_scan(
    world, monkeypatch
):
    fake_storage(monkeypatch, world.source)
    actual, calls = worker.scan, []

    def counted(*args, **kwargs):
        calls.append(1)
        return actual(*args, **kwargs)

    monkeypatch.setattr(worker, "scan", counted)
    monkeypatch.setattr(
        service, "publish", AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await execute(world)
    assert not await execute(world)
    assert calls == [1]
    async with world.maker() as db:
        row = await service.by_job(db, world.job_id)
        assert (
            row.status == "running"
            and await db.get(StorageOperation, row.operation_id) is None
        )
        assert await service.signal_interrupted(
            db, world.job_id, world.task_id, cancelled=False
        )
        await db.commit()


@pytest.mark.parametrize("stop_kind", ["caller", "domain", "role", "time"])
async def test_stop_does_not_publish_before_native_drain(world, monkeypatch, stop_kind):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def scan(*args, **kwargs):
        checkpoint = args[-1]
        entered.set()
        try:
            assert release.wait(10)
            if stop_kind == "time":
                raise InventoryStopped("inventory_time_budget")
            checkpoint()
        finally:
            closed.set()

    monkeypatch.setattr(worker, "scan", scan)
    task = asyncio.create_task(execute(world))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        async with world.maker() as db:
            row = await service.by_job(db, world.job_id)
            assert row.status == "running" and await db.get(
                StorageOperation, row.operation_id
            )
            if stop_kind == "domain":
                assert await service.cancel(db, world.job_id) == "cancel_requested"
            elif stop_kind == "role":
                actor = await db.get(User, world.actor_id)
                actor.role = "project_admin"
            await db.commit()
        if stop_kind == "caller":
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        async with world.maker() as db:
            assert (await service.by_job(db, world.job_id)).status == "running"
        release.set()
        if stop_kind == "caller":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert closed.is_set()
        async with world.maker() as db:
            row = await service.by_job(db, world.job_id)
            assert row.status == (
                "cancelled" if stop_kind in {"caller", "domain"} else "failed"
            )
            assert await db.get(StorageOperation, row.operation_id) is None
            assert not service.item(row).report_available
    finally:
        release.set()
        if not task.done():
            await worker.storage_access.finish(task)


async def test_cancel_during_final_publish_drains_then_propagates(world, monkeypatch):
    fake_storage(monkeypatch, world.source)
    entered, release = asyncio.Event(), asyncio.Event()
    actual = service.publish

    async def publish(*args, **kwargs):
        entered.set()
        await release.wait()
        return await actual(*args, **kwargs)

    monkeypatch.setattr(service, "publish", publish)
    task = asyncio.create_task(execute(world))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with world.maker() as db:
            assert (await service.by_job(db, world.job_id)).status == "completed"
    finally:
        release.set()
        if not task.done():
            await worker.storage_access.finish(task)


async def test_early_header_error_closes_owned_sink_and_client(monkeypatch):
    source = {
        "endpoint": "http://source",
        "purpose_buckets": {
            purpose: purpose for purpose in service.PURPOSE_BUCKET_ATTRIBUTES
        },
    }
    client = fake_storage(monkeypatch, source)
    sink = worker.ReportSink()
    monkeypatch.setattr(worker, "ReportSink", lambda: sink)
    with pytest.raises(ValueError):
        worker.scan(
            source,
            {"read_timeout_seconds": 1, "max_seconds": 1},
            {},
            {"too_deep": {"nested": object()}},
            lambda: None,
        )
    assert sink.closed and client.closed


async def test_signal_path_is_safe_and_idempotent_for_this_kind(world, monkeypatch):
    from app.workers import signals

    @asynccontextmanager
    async def session():
        async with world.maker() as db:
            yield db

    monkeypatch.setattr(
        signals,
        "create_async_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=AsyncMock()),
    )
    monkeypatch.setattr(signals, "async_sessionmaker", lambda *args, **kwargs: session)
    await signals._apply_terminal(
        world.task_id, cancelled=False, error="private source endpoint"
    )
    await signals._apply_terminal(
        world.task_id, cancelled=False, error="another secret"
    )
    async with world.maker() as db:
        row = await service.by_job(db, world.job_id)
        assert row.status == "failed" and row.error_code == "inventory_interrupted"
        notices = list(
            await db.scalars(
                select(Notification).where(Notification.target_id == world.job_id)
            )
        )
        assert len(notices) == 1 and "private" not in str(notices[0].payload)
