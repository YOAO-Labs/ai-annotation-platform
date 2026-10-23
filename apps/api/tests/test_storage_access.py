"""Physical operation receipts outlive awaits, not their actual I/O."""

import asyncio
from contextlib import asynccontextmanager
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.services import storage_access as access
from app.services.content_snapshot import ContentError
from tests.test_storage_maintenance_impact import location


@asynccontextmanager
async def world(test_engine):
    location_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker() as db:
            yield db, location_id
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location_id
                )
            )
            await connection.execute(
                delete(Gate).where(Gate.location_id == location_id)
            )


async def count(test_engine, location_id):
    async with test_engine.connect() as connection:
        return await connection.scalar(
            select(func.count())
            .select_from(StorageOperation)
            .where(StorageOperation.location_id == location_id)
        )


async def test_missing_state_and_unlocked_check_do_not_create_gate(test_engine):
    async with world(test_engine) as (db, location_id):
        assert await access.state(db, location_id) == {
            "state": "active",
            "revision": 1,
            "changed_at": None,
            "changed_by": None,
        }
        assert await access.check(db, location_id) == 1
        async with test_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(Gate.location_id).where(Gate.location_id == location_id)
                )
                is None
            )


async def test_independent_receipt_does_not_commit_caller_and_connection_bind_is_supported(
    db_session, test_engine
):
    db = db_session
    where = await location(db)
    location_id = where.id
    try:
        assert await access.check(db, location_id, lock=True) == 1
        # The caller already owns Gate SHARE and an uncommitted business location.
        permit = await asyncio.wait_for(
            access.acquire(db, location_id, "outer_transaction", expected_revision=1), 2
        )
        async with permit:
            assert permit.revision == 1
            assert await count(test_engine, location_id) == 1
            async with test_engine.connect() as connection:
                from app.db.models.dataset_asset import StorageLocation

                assert (
                    await connection.scalar(
                        select(StorageLocation.id).where(
                            StorageLocation.id == location_id
                        )
                    )
                    is None
                )
        assert await count(test_engine, location_id) == 0
        await db.rollback()
        assert await access.state(db, location_id) == {
            "state": "active",
            "revision": 1,
            "changed_at": None,
            "changed_by": None,
        }
    finally:
        await db.rollback()
        await db.bind.rollback()
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location_id
                )
            )
            await connection.execute(
                delete(Gate).where(Gate.location_id == location_id)
            )


async def test_drain_rejects_new_io_and_resume_rejects_old_proof(test_engine):
    async with world(test_engine) as (db, location_id):
        permit = await access.acquire(db, location_id, "old_read")
        async with test_engine.begin() as connection:
            await connection.execute(
                update(Gate)
                .where(Gate.location_id == location_id)
                .values(state="draining", revision=2)
            )
        assert await count(test_engine, location_id) == 1
        with pytest.raises(ContentError, match="^storage_maintenance$"):
            await access.acquire(db, location_id, "new_read")
        with pytest.raises(ContentError, match="storage_maintenance_changed"):
            await access.check(db, location_id, expected_revision=1, lock=True)
        await db.rollback()
        async with test_engine.begin() as connection:
            await connection.execute(
                update(Gate)
                .where(Gate.location_id == location_id)
                .values(state="active", revision=3)
            )
        with pytest.raises(ContentError, match="storage_maintenance_changed"):
            await access.acquire(db, location_id, "old_read", expected_revision=1)
        async with await access.acquire(
            db, location_id, "fresh_read", expected_revision=3
        ):
            assert await count(test_engine, location_id) == 2
        await permit.__aexit__(None, None, None)
        assert await count(test_engine, location_id) == 0


@pytest.mark.parametrize("action", ["admit", "publish"])
async def test_nowait_gate_conflict_keeps_caller_transaction_usable(
    test_engine, action
):
    async with world(test_engine) as (db, location_id):
        await access.ensure(db, location_id)
        async with test_engine.begin() as control:
            await control.execute(
                select(Gate.location_id)
                .where(Gate.location_id == location_id)
                .with_for_update()
            )
            with pytest.raises(ContentError, match="storage_maintenance_busy"):
                if action == "admit":
                    await asyncio.wait_for(access.acquire(db, location_id, "read"), 2)
                else:
                    await asyncio.wait_for(access.check(db, location_id, lock=True), 2)
            assert await db.scalar(select(1)) == 1


async def test_publication_share_lock_excludes_control_until_caller_transaction_ends(
    test_engine,
):
    async with world(test_engine) as (db, location_id):
        await access.check(db, location_id, lock=True)
        with pytest.raises(DBAPIError):
            async with test_engine.begin() as control:
                await control.execute(
                    select(Gate.location_id)
                    .where(Gate.location_id == location_id)
                    .with_for_update(nowait=True)
                )
        async with await access.acquire(db, location_id, "compatible_read"):
            assert await count(test_engine, location_id) == 1
        await db.rollback()
        async with test_engine.begin() as control:
            assert (
                await control.scalar(
                    select(Gate.location_id)
                    .where(Gate.location_id == location_id)
                    .with_for_update(nowait=True)
                )
                == location_id
            )


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_executor_receipt_survives_interruption_until_real_thread_finishes(
    test_engine, interruption
):
    async with world(test_engine) as (db, location_id):
        started, release = threading.Event(), threading.Event()
        physically_finished = threading.Event()

        def blocking():
            started.set()
            release.wait(5)
            physically_finished.set()
            return "finished"

        task = asyncio.create_task(
            access.run(
                db,
                location_id,
                "blocking_read",
                blocking,
                timeout=0.02 if interruption == "timeout" else None,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if interruption == "cancel":
                task.cancel()
            await asyncio.sleep(0.05)
            if interruption == "cancel":
                task.cancel()
            assert not task.done()
            assert not physically_finished.is_set()
            assert await count(test_engine, location_id) == 1
        finally:
            release.set()
        with pytest.raises(
            asyncio.CancelledError if interruption == "cancel" else TimeoutError
        ):
            await asyncio.wait_for(task, 3)
        assert physically_finished.is_set()
        assert await count(test_engine, location_id) == 0


async def test_settlement_failure_retains_record_and_original_body_error(
    test_engine, monkeypatch
):
    async with world(test_engine) as (db, location_id):
        permit = await access.acquire(db, location_id, "settlement_failure")

        async def unavailable():
            raise RuntimeError("test-secret-path")

        monkeypatch.setattr(permit, "_settle", unavailable)
        with pytest.raises(
            ContentError, match="storage_operation_unsettled"
        ) as failure:
            async with permit:
                pass
        assert failure.value.status == 503
        assert await count(test_engine, location_id) == 1
        with pytest.raises(ValueError, match="body failed"):
            async with permit:
                raise ValueError("body failed")
        assert await count(test_engine, location_id) == 1


async def test_cancelled_settlement_finishes_ledger_transaction_before_return(
    test_engine, monkeypatch
):
    async with world(test_engine) as (db, location_id):
        permit = await access.acquire(db, location_id, "settlement_cancel")
        original = permit._settle
        entered, release = asyncio.Event(), asyncio.Event()

        async def settle():
            entered.set()
            await release.wait()
            await original()

        monkeypatch.setattr(permit, "_settle", settle)

        async def body():
            async with permit:
                pass

        task = asyncio.create_task(body())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        assert not task.done()
        assert await count(test_engine, location_id) == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await count(test_engine, location_id) == 0


@pytest.mark.parametrize(
    "field,value", [("purpose", "unsafe/path"), ("node_id", "private path")]
)
async def test_receipt_rejects_arbitrary_path_like_labels(test_engine, field, value):
    async with world(test_engine) as (db, location_id):
        payload = {"purpose": "safe", field: value}
        with pytest.raises(ValueError, match="Invalid storage operation"):
            await access.acquire(db, location_id, **payload)
        assert await count(test_engine, location_id) == 0


@pytest.mark.parametrize("interruption", ["error", "cancel", "timeout"])
async def test_uncertain_close_remains_unsettled_after_late_executor_error(
    test_engine, interruption
):
    async with world(test_engine) as (db, location_id):
        started, release = threading.Event(), threading.Event()

        class Handle:
            def close(self):
                raise OSError("uncertain close")

        def blocking():
            started.set()
            release.wait(5)
            access.close_registered(Handle())

        task = asyncio.create_task(
            access.run(
                db,
                location_id,
                "uncertain_close",
                blocking,
                timeout=0.02 if interruption == "timeout" else None,
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            if interruption == "cancel":
                task.cancel()
            await asyncio.sleep(0.05)
            assert await count(test_engine, location_id) == 1
        finally:
            release.set()
        expected = {
            "error": OSError,
            "cancel": asyncio.CancelledError,
            "timeout": TimeoutError,
        }[interruption]
        with pytest.raises(expected) as caught:
            await task
        assert caught.value.storage_operation_unsettled is True
        assert await count(test_engine, location_id) == 1


async def test_read_error_with_successful_close_settles_receipt(test_engine):
    async with world(test_engine) as (db, location_id):
        with pytest.raises(OSError, match="read failed"):
            await access.run(
                db,
                location_id,
                "failed_read",
                lambda: (_ for _ in ()).throw(OSError("read failed")),
            )
        assert await count(test_engine, location_id) == 0


async def test_acquired_callback_persists_permit_identity_before_executor_starts(
    test_engine,
):
    async with world(test_engine) as (db, location_id):
        state = {}
        started = threading.Event()

        async def acquired(permit):
            assert not started.is_set()
            assert permit.location_id == location_id and permit.revision == 1
            assert await count(test_engine, location_id) == 1
            state["permit_id"] = permit.id
            await db.commit()
            state["callback_finished"] = True

        def read():
            assert state["callback_finished"]
            started.set()
            return state["permit_id"]

        identity = await access.run(
            db, location_id, "recorded_read", read, on_acquired=acquired
        )
        assert identity == state["permit_id"] and started.is_set()
        assert await count(test_engine, location_id) == 0


async def test_failed_acquired_callback_settles_receipt_without_starting_io(
    test_engine,
):
    async with world(test_engine) as (db, location_id):
        started = threading.Event()

        async def acquired(permit):
            assert await count(test_engine, location_id) == 1
            raise RuntimeError("owner evidence was not committed")

        with pytest.raises(RuntimeError, match="owner evidence was not committed"):
            await access.run(
                db, location_id, "unrecorded_read", started.set, on_acquired=acquired
            )
        assert not started.is_set()
        assert await count(test_engine, location_id) == 0


async def test_cancelled_acquired_callback_settles_before_return_and_never_starts_io(
    test_engine,
):
    async with world(test_engine) as (db, location_id):
        admitted, release = asyncio.Event(), asyncio.Event()
        started = threading.Event()

        async def acquired(permit):
            admitted.set()
            await release.wait()

        task = asyncio.create_task(
            access.run(
                db, location_id, "cancelled_read", started.set, on_acquired=acquired
            )
        )
        try:
            await asyncio.wait_for(admitted.wait(), 2)
            assert await count(test_engine, location_id) == 1
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            release.set()
        assert not started.is_set()
        assert await count(test_engine, location_id) == 0
