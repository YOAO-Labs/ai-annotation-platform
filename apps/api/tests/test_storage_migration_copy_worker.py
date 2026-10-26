"""Independent-session lifecycle, native drain and durable physical receipts."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from botocore.hooks import HierarchicalEmitter
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageLocation
from app.db.models.notification import Notification
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.storage_migration_copy import (
    StorageMigrationCopy as Copy,
    StorageMigrationCopyDelta as Delta,
)
from app.db.models.user import User
from app.schemas.storage_migration_copy import (
    StorageMigrationCopyAttemptRequest,
    StorageMigrationCopyGenerationRequest,
)
from app.services import storage_access, storage_migration_copies as service
from app.services.storage_migration_copy import CopyStopped
from app.workers import storage_migration_copy as worker
from tests.factory import create_user
from tests.storage_migration_isolation import migration_engine as migration_engine
from tests.test_storage_migration_copies import seed


@pytest.fixture
async def world(migration_engine):
    maker = async_sessionmaker(
        migration_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with maker() as db:
        actor = await create_user(
            db, "super_admin", f"copy-{uuid.uuid4()}@test.local", "Copy"
        )
        actor_id = actor.id
        inv, connection, location, body = await seed(db, actor)
        row, job, _ = await service.start(db, actor, body)
        copy_id, job_id, task_id, location_id, connection_id = (
            row.id,
            job.id,
            job.celery_task_id,
            location.id,
            connection.id,
        )
        await db.commit()
    try:
        yield SimpleNamespace(
            maker=maker,
            actor_id=actor_id,
            copy_id=copy_id,
            job_id=job_id,
            task_id=task_id,
            location_id=location_id,
            connection_id=connection_id,
        )
    finally:
        async with maker() as db:
            copy_ids = list(
                await db.scalars(
                    select(Copy.id)
                    .where(Copy.target_location_id == location_id)
                    .order_by(Copy.generation.desc())
                )
            )
            attempts = list(
                await db.scalars(
                    select(service.Attempt).where(service.Attempt.copy_id.in_(copy_ids))
                )
            )
            ids = [
                p
                for a in attempts
                for p in (a.source_operation_id, a.target_operation_id)
                if p
            ]
            if ids:
                await db.execute(
                    delete(StorageOperation).where(StorageOperation.id.in_(ids))
                )
            for owned_id in copy_ids:
                await db.execute(delete(Copy).where(Copy.id == owned_id))
            await db.execute(
                delete(Notification).where(Notification.user_id == actor_id)
            )
            await db.execute(delete(AsyncJob).where(AsyncJob.user_id == actor_id))
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id == actor_id,
                    AuditLog.action.in_(
                        [
                            "storage.migration_copy_start",
                            "storage.migration_inventory_start",
                        ]
                    ),
                )
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await db.execute(
                delete(StorageConnection).where(StorageConnection.id == connection_id)
            )
            await db.commit()


async def execute(world):
    async with world.maker() as db:
        return await worker.execute(db, world.job_id, world.task_id)


async def attempt(world, operation, **kwargs):
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        row, job, reused = await service.attempt(
            db,
            world.copy_id,
            actor,
            StorageMigrationCopyAttemptRequest(operation=operation, **kwargs),
        )
        world.job_id, world.task_id = job.id, job.celery_task_id
        await db.commit()
        return row, reused


async def test_real_collector_and_plan_once_with_no_source_or_target_sdk_calls(
    world, monkeypatch
):
    monkeypatch.setattr(
        worker,
        "StorageService",
        lambda **kw: pytest.fail("prepare must not construct S3 client"),
    )
    calls = []
    original = worker.native

    def native(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(worker, "native", native)
    results = await asyncio.gather(execute(world), execute(world))
    assert results.count(True) == 1 and calls == [1]
    async with world.maker() as db:
        row = await service.owner(db, world.copy_id)
        value = await service.item(db, row)
        assert (
            value.prepared
            and value.status == "completed"
            and value.operation == "prepare"
        )
        assert value.counts.model_dump() == dict.fromkeys(service.STATES, 0)
        assert value.source_summary["objects"] == 0
        raw, checksum = await service.download(
            db, row.id, await db.get(User, world.actor_id)
        )
        assert service.sha(raw) == checksum and raw.startswith(b"SQLite format 3")
        a = await service.by_job(db, world.job_id)
        assert not await db.get(StorageOperation, a.source_operation_id)
        assert not await db.get(StorageOperation, a.target_operation_id)


async def test_unknown_native_close_keeps_both_permits_and_blocks_resume(
    world, monkeypatch
):
    def fail(*args):
        error = CopyStopped("storage_operation_unsettled")
        raise storage_access.mark_unsettled(error)

    monkeypatch.setattr(worker, "native", fail)
    assert await execute(world)
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        assert a.status == "failed" and a.error_code == "storage_operation_unsettled"
        assert await db.get(StorageOperation, a.source_operation_id)
        assert await db.get(StorageOperation, a.target_operation_id)
        with pytest.raises(HTTPException) as caught:
            await service.attempt(
                db,
                world.copy_id,
                await db.get(User, world.actor_id),
                StorageMigrationCopyAttemptRequest(operation="prepare"),
            )
        assert caught.value.detail == {"code": "copy_execution_unsettled"}


async def test_running_cancel_waits_for_native_close_and_preserves_plan_owner(
    world, monkeypatch
):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def native(operation, values, access, checkpoint, *args):
        started.set()
        try:
            release.wait(10)
            checkpoint()
        finally:
            closed.set()

    monkeypatch.setattr(worker, "native", native)
    executing = asyncio.create_task(execute(world))
    assert await asyncio.to_thread(started.wait, 5)
    async with world.maker() as db:
        assert await service.cancel(db, world.job_id) == "cancel_requested"
        await db.commit()
        a = await service.by_job(db, world.job_id)
        assert a.status == "running" and await db.get(
            StorageOperation, a.source_operation_id
        )
    release.set()
    assert await executing and closed.is_set()
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        assert a.status == "cancelled"
        assert not await db.get(StorageOperation, a.source_operation_id)
        assert await service.owner(db, world.copy_id)


async def test_caller_cancel_drains_thread_before_terminal_publication(
    world, monkeypatch
):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def native(*args):
        started.set()
        try:
            release.wait(10)
            raise CopyStopped("copy_cancelled")
        finally:
            closed.set()

    monkeypatch.setattr(worker, "native", native)
    executing = asyncio.create_task(execute(world))
    assert await asyncio.to_thread(started.wait, 5)
    executing.cancel()
    await asyncio.sleep(0.02)
    assert not executing.done() and not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await executing
    assert closed.is_set()
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        assert a.status == "cancelled" and not await db.get(
            StorageOperation, a.target_operation_id
        )


async def test_failed_prepare_explicit_retry_and_secret_rotation_new_attempt(
    world, monkeypatch
):
    original = worker.native
    monkeypatch.setattr(
        worker,
        "native",
        lambda *args: (_ for _ in ()).throw(CopyStopped("fixture_prepare_failed")),
    )
    assert await execute(world)
    monkeypatch.setattr(worker, "native", original)
    async with world.maker() as db:
        connection = await db.get(StorageConnection, world.connection_id)
        location = await db.get(StorageLocation, world.location_id)
        connection.secret_enc, connection.revision, location.config_revision = (
            b"rotated",
            connection.revision + 1,
            location.config_revision + 1,
        )
        await db.commit()
    _, reused = await attempt(world, "prepare")
    assert not reused and await execute(world)
    async with world.maker() as db:
        row = await service.owner(db, world.copy_id)
        assert (
            row.baseline_sha256 and (await service.last(db, row)).status == "completed"
        )
        with pytest.raises(HTTPException):
            await service.attempt(
                db,
                row.id,
                await db.get(User, world.actor_id),
                StorageMigrationCopyAttemptRequest(operation="prepare"),
            )


async def test_explicit_hard_restart_uses_real_child_death_proof(world):
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proof = service.process_identity()
        proof.update(
            pid=child.pid,
            start=Path(f"/proc/{child.pid}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19],
        )
        async with world.maker() as db:
            a = await service.claim(db, world.job_id, world.task_id)
            a.process = proof
            await db.commit()
            row = await service.owner(db, world.copy_id)
            permits = [
                await storage_access.acquire(
                    db,
                    loc,
                    service.KIND,
                    expected_revision=a.scope["maintenance"][str(loc)],
                    node_id=f"migration:{child.pid}",
                )
                for loc in sorted(
                    {service.inventories.LEGACY_LOCATION_ID, row.target_location_id}
                )
            ]
            await service.acquired(db, world.job_id, world.task_id, permits)
            with pytest.raises(HTTPException):
                await service.attempt(
                    db,
                    world.copy_id,
                    await db.get(User, world.actor_id),
                    StorageMigrationCopyAttemptRequest(operation="run"),
                )
            await db.rollback()
        child.kill()
        child.wait(timeout=5)
        assert service.proven_dead(proof)
        old_job = world.job_id
        _, reused = await attempt(world, "prepare")
        assert not reused and world.job_id != old_job
        async with world.maker() as db:
            assert not await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.id.in_([p.id for p in permits])
                )
            )
            old = await service.by_job(db, old_job)
            assert old.status == "failed" and old.error_code == "copy_process_ended"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.fixture
def clients(monkeypatch):
    """Reuse the established CLI protocol fake without forking its algorithm."""
    import importlib.util
    import sys

    scripts = Path(__file__).resolve().parents[3] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location(
            "copy_protocol_fakes", scripts / "test_storage_migration_copy.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
    source, target = module.Client(source=True), module.Client()
    source.closed = target.closed = False
    source.close = lambda: setattr(source, "closed", True)
    target.close = lambda: setattr(target, "closed", True)
    from app.services.sources.s3_reference import S3Access

    @contextmanager
    def client(access, **kwargs):
        try:
            yield target
        finally:
            storage_access.close_registered(target)

    monkeypatch.setattr(S3Access, "client", client)

    def configured(**kwargs):
        return SimpleNamespace(client=source)

    monkeypatch.setattr(worker, "StorageService", configured)
    return SimpleNamespace(source=source, target=target, module=module)


def object_report(inventory, count, complete):
    from app.services.storage_migration_inventory import _Writer, SCHEMA
    from app.workers.storage_migration_inventory import ReportSink

    source = inventory.source
    sink = ReportSink()
    writer = _Writer(None, sink=sink)
    writer.emit(
        {
            "type": "header",
            "schema": SCHEMA,
            "source": {"endpoint": source["endpoint"]},
            "purpose_buckets": source["purpose_buckets"],
            "handling": {
                purpose: inventory.handling.get(purpose, "unreviewed")
                for purpose in service.inventories.PURPOSES
            },
            "limits": {
                key: value
                for key, value in inventory.limits.items()
                if key != "max_seconds"
            }
            | {"chunk_bytes": 1024**2},
        }
    )
    for index in range(count):
        writer.emit(
            {
                "type": "object",
                "bucket": source["purpose_buckets"]["datasets"],
                "key": f"private/raw/%2F/../{index}.bin",
                "version_id": f"v{index}",
                "size_bytes": 3,
                "etag": '"opaque"',
                "verification": {"status": "verified", "sha256": service.sha(b"abc")},
            }
        )
    writer.finish(
        {
            "complete": complete,
            "migration_ready": False,
            "counts": {
                "buckets": 0,
                "objects": count,
                "delete_markers": 0,
                "multipart": 0,
                "verified_objects": count,
                "verified_bytes": 3 * count,
                "bytes_read": 3 * count,
                "pages": 1,
                "issues": int(not complete),
                "entries": count,
            },
        }
    )
    writer.stream.close()
    return sink.value


async def add_objects(world, clients, count=2, complete=False):
    async with world.maker() as db:
        row = await service.owner(db, world.copy_id)
        inventory = await db.get(
            service.StorageMigrationInventory, row.source_inventory_job_id
        )
        raw = object_report(inventory, count, complete)
        row.source_report, row.source_report_sha256, row.source_complete = (
            raw,
            service.sha(raw),
            complete,
        )
        endpoint = row.source["endpoint"]
        await db.commit()
    clients.source.meta = SimpleNamespace(
        endpoint_url=endpoint, events=HierarchicalEmitter()
    )


async def next_inventory(world, *, count=2, complete=False):
    """Seal a distinct report without changing the frozen source scope."""
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        inv, job, reused = await service.inventories.start(db, actor)
        assert not reused
        await service.inventories.claim(db, job.id, inv.task_id)
        inv.operation_id = uuid.uuid4()
        raw = object_report(inv, count, complete)
        assert await service.inventories.publish(db, job.id, inv.task_id, report=raw)
        assert inv.report_sha256 == service.sha(raw)
        await db.commit()
        return job.id


async def derive(world, inventory_id, *, parent_id=None):
    async with world.maker() as db:
        row, job, reused = await service.generation(
            db,
            parent_id or world.copy_id,
            await db.get(User, world.actor_id),
            StorageMigrationCopyGenerationRequest(inventory_job_id=inventory_id),
        )
        await db.commit()
        return (
            row.id,
            job.id if job else None,
            job.celery_task_id if job else None,
            reused,
        )


async def test_generation_prepares_pending_inheritance_and_rechecks_old_receipt(
    world, clients, tmp_path
):
    await add_objects(world, clients, count=1, complete=False)
    assert await execute(world)
    await attempt(world, "run")
    clients.target.fail_get = True
    assert await execute(world)
    assert clients.target.puts == 1
    clients.target.fail_get = False
    parent_id = world.copy_id
    async with world.maker() as db:
        parent = await service.owner(db, parent_id, private=True)
        assert parent.summary["counts"]["failed"] == 1 and not parent.source_complete
        frozen = parent.baseline_sha256, parent.delta_sequence, parent.delta_bytes
        expected = await asyncio.to_thread(
            service.evidence, await service.input_values(db, parent)
        )
    inventory_id = await next_inventory(world, count=2, complete=False)
    child_id, world.job_id, world.task_id, reused = await derive(world, inventory_id)
    assert not reused
    async with world.maker() as db:
        parent = await service.owner(db, parent_id)
        child = await service.owner(db, child_id)
        public = await service.item(db, child)
        assert public.generation == 2 and public.parent_copy_id == parent_id
        assert public.parent_evidence_sha256 == expected
        assert public.operation == "prepare" and not public.prepared
        assert child.target == parent.target and child.handling == parent.handling
        assert child.limits == parent.limits and child.source == parent.source
        assert (await service.item(db, parent)).read_only
        assert (await service.item(db, parent)).successor_copy_id == child_id
    assert await execute(world)
    world.copy_id = child_id
    async with world.maker() as db:
        child = await service.owner(db, child_id)
        value = await service.item(db, child)
        assert value.counts.pending == 2 and value.counts.verified == 0
        assert value.generation_summary.reused_objects == 1
        assert value.generation_summary.new_objects == 1
        assert value.status == "completed" and not value.copy_subset_complete
        values = await service.inputs(db, child_id)
        with service.Checkpoint(service.materialize(tmp_path, values)) as state:
            inherited = next(r for r in state.rows() if r["receipt"])
            identity = (
                inherited["creation_plan_uuid"],
                inherited["target_key"],
                inherited["nonce"],
            )
        from app.services.storage_migration_copy import CopyError

        tampered = dict(
            inherited, creation_plan_uuid="00000000-0000-0000-0000-000000000000"
        )
        bad = {
            **values,
            "deltas": [(tampered["id"], tampered, service.row_digest(tampered))],
        }
        bad_path = tmp_path / "tampered"
        bad_path.mkdir(mode=0o700)
        with pytest.raises(CopyError, match="copy_checkpoint_integrity"):
            service.materialize(bad_path, bad)
        bad_path = tmp_path / "wrong-generation"
        bad_path.mkdir(mode=0o700)
        with pytest.raises(CopyError, match="copy_checkpoint_integrity"):
            service.materialize(bad_path, {**values, "generation": 3})
        actor = await db.get(User, world.actor_id)
        parent = await service.owner(db, parent_id)
        for call in (
            lambda: service.attempt(
                db,
                parent_id,
                actor,
                StorageMigrationCopyAttemptRequest(operation="run"),
            ),
            lambda: service.redispatch(db, parent_id, actor),
        ):
            with pytest.raises(HTTPException) as caught:
                await call()
            assert caught.value.detail == {"code": "copy_read_only"}
        raw, _ = await service.download(db, parent_id, actor)
        assert raw.startswith(b"SQLite format 3")
        parent = await service.owner(db, parent_id)
        assert (
            parent.baseline_sha256,
            parent.delta_sequence,
            parent.delta_bytes,
        ) == frozen
    clients.source.calls.clear()
    await attempt(world, "run")
    assert await execute(world) and clients.target.puts == 2
    async with world.maker() as db:
        value = await service.item(db, await service.owner(db, child_id))
        assert value.counts.verified == 2 and value.status == "completed"
        assert value.io["source_bytes"] == value.io["upload_bytes"] == 3
        assert len(clients.source.calls) == 3  # HEAD+GET+HEAD only for the new object.
        deltas = list(await db.scalars(select(Delta).where(Delta.copy_id == child_id)))
        saved = next(
            d.data for d in deltas if d.data["creation_plan_uuid"] == identity[0]
        )
        assert (
            saved["creation_plan_uuid"],
            saved["target_key"],
            saved["nonce"],
        ) == identity
        # Original inventory/attempt Jobs can expire without erasing either plan.
        await db.execute(delete(AsyncJob).where(AsyncJob.user_id == world.actor_id))
        await db.commit()
        parent = await service.owner(db, parent_id)
        child = await service.owner(db, child_id)
        assert parent.source_inventory_job_id is child.source_inventory_job_id is None
        assert (await service.item(db, parent)).successor_copy_id == child_id
        assert (await service.item(db, child)).parent_evidence_sha256 == expected
    grand_inventory = await next_inventory(world, count=3)
    grand_id, _, _, reused = await derive(world, grand_inventory)
    assert not reused
    async with world.maker() as db:
        grand = await service.owner(db, grand_id)
        assert grand.generation == 3 and grand.parent_copy_id == child_id
        assert (await service.item(db, await service.owner(db, child_id))).read_only


async def test_generation_atomic_single_successor_reuse_and_report_identity(
    world, clients
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    inventory_id = await next_inventory(world, count=2)
    other_id = await next_inventory(world, count=3)
    first, second = await asyncio.gather(
        derive(world, inventory_id), derive(world, inventory_id)
    )
    assert first[:3] == second[:3] and sorted([first[3], second[3]]) == [False, True]
    with pytest.raises(HTTPException) as caught:
        await derive(world, other_id)
    assert caught.value.detail == {"code": "copy_successor_exists"}
    async with world.maker() as db:
        children = list(
            await db.scalars(select(Copy).where(Copy.parent_copy_id == world.copy_id))
        )
        assert len(children) == 1
        parent = await service.owner(db, world.copy_id)
        # Even a stale pending worker delivery cannot claim a frozen parent.
        a = await service.last(db, parent)
        a.status = "pending"
        job = await db.get(AsyncJob, a.async_job_id)
        job.status = "pending"
        await db.commit()
        assert await service.claim(db, job.id, a.task_id) is None
        await db.commit()
        assert (await service.by_job(db, job.id)).error_code == "copy_read_only"


async def test_generation_rejects_same_report_unprepared_and_unsettled_parent(
    world, clients
):
    inventory_id = await next_inventory(world, count=2)
    with pytest.raises(HTTPException) as caught:
        await derive(world, inventory_id)
    assert caught.value.detail == {"code": "copy_not_prepared"}
    await add_objects(world, clients, count=1)
    assert await execute(world)
    same_id = await next_inventory(world, count=1)
    with pytest.raises(HTTPException) as caught:
        await derive(world, same_id)
    assert caught.value.detail == {"code": "copy_generation_unchanged"}
    await attempt(world, "run")
    with pytest.raises(HTTPException) as caught:
        await derive(world, inventory_id)
    assert caught.value.detail == {"code": "copy_active_conflict"}
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        row = await service.owner(db, world.copy_id)
        permit = await storage_access.acquire(db, row.target_location_id, service.KIND)
        a.target_operation_id = permit.id
        a.status = "failed"
        (await db.get(AsyncJob, a.async_job_id)).status = "failed"
        await db.commit()
    with pytest.raises(HTTPException) as caught:
        await derive(world, inventory_id)
    assert caught.value.detail == {"code": "copy_execution_unsettled"}


async def test_generation_cannot_freeze_before_cancelled_native_thread_drains(
    world, clients, monkeypatch
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    inventory_id = await next_inventory(world, count=2)
    await attempt(world, "run")
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def native(*args):
        entered.set()
        try:
            release.wait(10)
            raise CopyStopped("copy_cancelled")
        finally:
            closed.set()

    monkeypatch.setattr(worker, "native", native)
    task = asyncio.create_task(execute(world))
    assert await asyncio.to_thread(entered.wait, 5)
    async with world.maker() as db:
        assert await service.cancel(db, world.job_id) == "cancel_requested"
        await db.commit()
    try:
        with pytest.raises(HTTPException) as caught:
            await derive(world, inventory_id)
        assert caught.value.detail == {"code": "copy_active_conflict"}
        assert not closed.is_set()
    finally:
        release.set()
        await task
    assert closed.is_set()
    assert not (await derive(world, inventory_id))[3]


async def test_cancelled_generation_digest_drains_before_releasing_parent_lock(
    world, clients, monkeypatch
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    inventory_id = await next_inventory(world, count=2)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = service.evidence

    def evidence(values):
        entered.set()
        try:
            release.wait(10)
            return original(values)
        finally:
            closed.set()

    monkeypatch.setattr(service, "evidence", evidence)
    generating = asyncio.create_task(derive(world, inventory_id))
    assert await asyncio.to_thread(entered.wait, 5)
    competing = asyncio.create_task(attempt(world, "run"))
    generating.cancel()
    await asyncio.sleep(0.02)
    assert not generating.done() and not competing.done() and not closed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await generating
    await competing
    assert closed.is_set()
    async with world.maker() as db:
        assert not await service.successor(db, world.copy_id)


async def test_two_failed_then_only_one_selected_retry_and_safe_history(world, clients):
    await add_objects(world, clients)
    assert await execute(world)
    await attempt(world, "run")
    clients.source.fail_get = True
    assert await execute(world) and clients.target.puts == 0
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        page = await service.failed_objects(db, world.copy_id, actor, limit=1)
        assert page["total"] == 2 and len(page["items"]) == 1
        selected = [page["items"][0]["object_id"]]
        assert set(page["items"][0]) == {"object_id", "size_bytes", "error_code"}
        with pytest.raises(HTTPException) as caught:
            await service.attempt(
                db,
                world.copy_id,
                actor,
                StorageMigrationCopyAttemptRequest(
                    operation="run", retry_failed=True, retry_object_ids=["f" * 64]
                ),
            )
        assert caught.value.detail == {"code": "selection_changed"}
    clients.source.fail_get = False
    await attempt(world, "run", retry_failed=True, retry_object_ids=selected)
    assert await execute(world) and clients.target.puts == 1
    async with world.maker() as db:
        value = await service.item(db, await service.owner(db, world.copy_id))
        assert value.status == "completed" and value.attempt_subset_complete
        assert value.selected_count == value.selected_verified_count == 1
        assert (
            value.counts.verified == value.counts.failed == 1
            and not value.copy_subset_complete
        )
        deltas = list(
            await db.scalars(select(Delta).where(Delta.copy_id == world.copy_id))
        )
        assert len(deltas) == 2 and len({d.sequence for d in deltas}) == 2


@pytest.mark.parametrize("late", ["cancel", "revoke"])
async def test_actual_ack_commits_even_after_late_cancel_or_revocation(
    world, clients, late
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    await attempt(world, "run")
    loop = asyncio.get_running_loop()
    original = clients.target.put_object

    async def change():
        async with world.maker() as db:
            if late == "cancel":
                await service.cancel(db, world.job_id)
            else:
                (await db.get(User, world.actor_id)).role = "annotator"
            await db.commit()

    def put(**kwargs):
        response = original(**kwargs)
        asyncio.run_coroutine_threadsafe(change(), loop).result()
        return response

    clients.target.put_object = put
    assert await execute(world)
    async with world.maker() as db:
        row = await service.owner(db, world.copy_id)
        a = await service.by_job(db, world.job_id)
        delta = await db.scalar(select(Delta).where(Delta.copy_id == row.id))
        assert (
            delta.data["receipt"]["put_acknowledged"]
            and delta.data["receipt"]["version_id"] == "actual-target-1"
        )
        assert a.status == ("cancelled" if late == "cancel" else "failed")
        assert row.summary["counts"]["writing"] == 1
        assert (
            clients.source.closed and clients.target.closed and clients.target.puts == 1
        )
        assert not await db.get(StorageOperation, a.source_operation_id)
        assert not await db.get(StorageOperation, a.target_operation_id)


@pytest.mark.parametrize("failure", ["lost_ack", "readback"])
async def test_lost_response_or_failed_readback_resumes_without_second_put(
    world, clients, failure
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    await attempt(world, "run")
    if failure == "lost_ack":
        clients.target.lose_ack = True
    else:
        clients.target.fail_get = True
    assert await execute(world)
    async with world.maker() as db:
        delta = await db.scalar(select(Delta).where(Delta.copy_id == world.copy_id))
        assert delta.data["state"] == (
            "uncertain" if failure == "lost_ack" else "failed"
        )
        assert bool(delta.data["receipt"]) == (failure == "readback")
    clients.target.lose_ack = clients.target.fail_get = False
    await attempt(world, "run", retry_failed=failure == "readback")
    assert await execute(world)
    assert clients.target.puts == 1
    await attempt(world, "verify")
    assert await execute(world) and clients.target.puts == 1
    async with world.maker() as db:
        value = await service.item(db, await service.owner(db, world.copy_id))
        assert value.status == "completed" and value.counts.verified == 1
        assert value.io["source_bytes"] == value.io["upload_bytes"] == 0
        assert value.io["verify_bytes"] == 3


async def test_refused_intent_never_calls_put(world, clients, monkeypatch):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    await attempt(world, "run")

    async def refuse(*args):
        raise HTTPException(503, detail={"code": "fixture_commit_refused"})

    monkeypatch.setattr(service, "save_delta", refuse)
    assert await execute(world)
    assert clients.target.puts == 0
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        assert a.status == "failed" and a.error_code == "copy_checkpoint_commit_failed"
        assert (await service.owner(db, world.copy_id)).summary["counts"][
            "pending"
        ] == 1


async def test_uncertain_publish_keeps_pending_same_task_and_explicit_redispatch(
    world, monkeypatch
):
    sent = []

    def unavailable(job_id, task_id):
        sent.append((job_id, task_id))
        raise OSError("private broker credential")

    monkeypatch.setattr(worker, "_publish", unavailable)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        await worker.dispatch(db, job)
        row, same = await service.redispatch(
            db, world.copy_id, await db.get(User, world.actor_id)
        )
        await worker.dispatch(db, same)
        assert same.id == world.job_id and same.status == "pending"
        assert sent == [(world.job_id, world.task_id)] * 2
        assert await service.cancel(db, world.job_id) == "cancelled"
        await db.commit()
        assert not await service.claim(db, world.job_id, world.task_id)


async def test_source_name_revision_may_change_between_attempts(world):
    assert await execute(world)
    async with world.maker() as db:
        location = await db.get(StorageLocation, service.inventories.LEGACY_LOCATION_ID)
        original = location.name, location.config_revision
        location.name = "Same physical configured source"
        location.config_revision += 1
        await db.commit()
    try:
        await attempt(world, "run")
        async with world.maker() as db:
            a = await service.by_job(db, world.job_id)
            assert a.scope["source"] == original[1] + 1
            assert await service.claim(db, world.job_id, world.task_id)
            await db.commit()
    finally:
        async with world.maker() as db:
            location = await db.get(
                StorageLocation, service.inventories.LEGACY_LOCATION_ID
            )
            location.name, location.config_revision = original
            await db.commit()


async def test_source_constructor_unknown_closure_keeps_both_permits(
    world, clients, monkeypatch
):
    await add_objects(world, clients, count=1)
    assert await execute(world)
    await attempt(world, "run")

    def failed(**kwargs):
        raise OSError("client constructor may own a native handle")

    monkeypatch.setattr(worker, "StorageService", failed)
    assert await execute(world)
    async with world.maker() as db:
        a = await service.by_job(db, world.job_id)
        assert a.error_code == "storage_operation_unsettled"
        assert await db.get(StorageOperation, a.source_operation_id)
        assert await db.get(StorageOperation, a.target_operation_id)


async def test_recover_clones_old_selection_including_verified_without_broadening(
    world, clients
):
    import subprocess
    import sys

    await add_objects(world, clients, count=3)
    assert await execute(world)
    await attempt(world, "run")
    clients.source.fail_get = True
    assert await execute(world)
    clients.source.fail_get = False
    async with world.maker() as db:
        ids = list(
            await db.scalars(
                select(Delta.object_id)
                .where(Delta.copy_id == world.copy_id)
                .order_by(Delta.object_id)
            )
        )
    selected = ids[:2]
    await attempt(world, "run", retry_failed=True, retry_object_ids=selected)
    assert await execute(world) and clients.target.puts == 2
    # Model the exact persisted selection left by a dead worker after its first
    # object verification. Physical receipt identity is retained for object two.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proof = service.process_identity()
        proof.update(
            pid=child.pid,
            start=Path(f"/proc/{child.pid}/stat")
            .read_text()
            .rsplit(")", 1)[1]
            .split()[19],
        )
        async with world.maker() as db:
            a = await service.by_job(db, world.job_id)
            row = await service.owner(db, world.copy_id)
            delta = await db.get(Delta, (row.id, selected[1]))
            data = dict(delta.data)
            data.update(state="uncertain", error="put_outcome_unknown")
            a.status, a.process, a.completed_at = "running", proof, None
            job = await db.get(AsyncJob, world.job_id)
            job.status, job.completed_at = "running", None
            await db.commit()
            await service.save_delta(
                db, world.job_id, world.task_id, data, service.row_digest(data)
            )
        child.kill()
        child.wait(timeout=5)
        _, reused = await attempt(world, "recover")
        assert not reused
        async with world.maker() as db:
            a = await service.by_job(db, world.job_id)
            assert (
                a.operation == "run"
                and a.retry_failed
                and a.retry_object_ids == selected
            )
        assert await execute(world) and clients.target.puts == 2
        async with world.maker() as db:
            value = await service.item(db, await service.owner(db, world.copy_id))
            assert (
                value.status == "completed"
                and value.selected_count == value.selected_verified_count == 2
            )
            assert (
                value.counts.verified == 2
                and value.counts.failed == 1
                and not value.copy_subset_complete
            )
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


async def test_frozen_evidence_refuses_destructive_schema_downgrade(world, test_db_url):
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", test_db_url)
    async with world.maker() as db:
        original_revision = await db.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    with pytest.raises(RuntimeError, match="evidence must be retained"):
        await asyncio.to_thread(command.downgrade, config, "0255")
    async with world.maker() as db:
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == original_revision
        )
        assert await service.owner(db, world.copy_id)
