"""Real reader admission and publication remain stale across drain/resume."""

import asyncio
from contextlib import asynccontextmanager
import io
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from app.db.models.async_job import AsyncJob
from app.db.models.dataset_quality import DatasetQualityRun
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.schemas.dataset_quality import DatasetQualityConfig
from app.services import content_snapshot as snapshots, dataset_quality as quality
from app.services import dataset_references as references, original_cache as cache
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.reference_bindings import (
    reference_binding,
    task_binding,
    lock_reference_bindings,
)
from app.services.training_dataset_sources import (
    lock_source_maintenance,
    training_reference_snapshot,
)
from app.workers import dataset_quality as quality_worker
from tests.test_original_cache import (
    cache_world as cache_world,
    snapshot as input_snapshot,
)
from tests.test_reference_original_cache import referenced_cache as referenced_cache


async def set_gate(world, state, revision):
    async with world.factory() as db:
        await db.execute(
            insert(Gate)
            .values(
                location_id=world.location_id,
                state=state,
                revision=revision,
            )
            .on_conflict_do_update(
                index_elements=[Gate.location_id],
                set_={"state": state, "revision": revision},
            )
        )
        await db.commit()


async def clean_gate(world):
    async with world.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(StorageOperation)
                .where(
                    StorageOperation.location_id == world.location_id,
                )
            )
            == 0
        )
        await db.execute(delete(Gate).where(Gate.location_id == world.location_id))
        await db.commit()


async def test_persistent_reference_resumes_but_old_binding_cannot_publish(
    referenced_cache,
):
    world = referenced_cache
    try:
        async with world.factory() as db:
            old = {
                **task_binding(world.task),
                "reference": await reference_binding(db, world.task),
            }
            # A historical delegation still works at initial epoch 1.
            old["reference"].pop("maintenance_revision")
            await lock_reference_bindings(db, [old], "test")
            await db.rollback()
        await set_gate(world, "draining", 2)
        async with world.factory() as db:
            with pytest.raises(snapshots.ContentError, match="storage_maintenance"):
                await references.reference_snapshot(
                    db, world.item_id, world.actor, task_id=world.task.id
                )
        await set_gate(world, "active", 3)
        async with world.factory() as db:
            snapshot = await references.reference_snapshot(
                db, world.item_id, world.actor, task_id=world.task.id
            )
            try:
                assert snapshot.file.read() == world.data
            finally:
                snapshot.close()
            assert (await reference_binding(db, world.task))[
                "maintenance_revision"
            ] == 3
            with pytest.raises(
                snapshots.ContentError, match="storage_maintenance_changed"
            ):
                await lock_reference_bindings(db, [old], "test")
    finally:
        await clean_gate(world)


async def test_cache_hit_denies_maintenance_and_reads_existing_generation_after_resume(
    cache_world,
):
    world = cache_world
    snapshot = await input_snapshot(world)
    try:
        assert (
            await cache.store(world.factory, world.source_id, world.asset_id, snapshot)
            == "stored"
        )
        put_count = len(world.puts)
        await set_gate(world, "draining", 2)
        with pytest.raises(snapshots.ContentError, match="storage_maintenance"):
            await cache.read(world.factory, world.source_id, world.asset_id)
        await set_gate(world, "active", 3)
        cached = await cache.read(world.factory, world.source_id, world.asset_id)
        assert cached is not None
        cached.close()
        with pytest.raises(snapshots.ContentError, match="storage_maintenance_changed"):
            await cache.store(
                world.factory,
                world.source_id,
                world.asset_id,
                snapshot,
                expected_revision=1,
            )
        assert len(world.puts) == put_count
    finally:
        snapshot.close()
        await clean_gate(world)


async def test_cancelled_cache_read_keeps_physical_operation_until_thread_ends(
    cache_world,
):
    world = cache_world
    snapshot = await input_snapshot(world)
    task = None
    try:
        assert (
            await cache.store(world.factory, world.source_id, world.asset_id, snapshot)
            == "stored"
        )
        world.get_started, world.get_release = threading.Event(), threading.Event()
        task = asyncio.create_task(
            cache.read(world.factory, world.source_id, world.asset_id)
        )
        assert await asyncio.to_thread(world.get_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        await set_gate(world, "draining", 2)
        async with world.factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(StorageOperation)
                    .where(
                        StorageOperation.location_id == world.location_id,
                    )
                )
                == 1
            )
        world.get_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if world.get_release:
            world.get_release.set()
        if task and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        snapshot.close()
        await clean_gate(world)


@pytest.mark.parametrize("legacy", [False, True])
async def test_old_quality_run_stops_before_any_member_read_after_resume(
    referenced_cache,
    legacy,
):
    world = referenced_cache
    try:
        async with world.factory() as db:
            run = await quality.start(
                db, world.dataset_id, world.manager, DatasetQualityConfig()
            )
            await db.commit()
            assert run.authority["storage_maintenance"] == {str(world.location_id): 1}
            if legacy:
                run.authority = {
                    key: value
                    for key, value in run.authority.items()
                    if key != "storage_maintenance"
                }
                await db.commit()
            job = await db.get(AsyncJob, run.async_job_id)
            task_id = job.celery_task_id
        await set_gate(world, "active", 3)
        async with world.factory() as db:
            await quality_worker.execute(db, run.id, task_id, "worker:test")
            stopped = await db.get(DatasetQualityRun, run.id, populate_existing=True)
            assert stopped.status == "failed"
            assert stopped.error_code == "storage_maintenance_changed"
            assert stopped.processed == 0
        assert not world.bodies
    finally:
        await clean_gate(world)


async def test_old_training_source_proof_is_rejected_before_read_and_publication(
    referenced_cache,
):
    world = referenced_cache
    sample = {
        "task_id": str(world.task.id),
        "dataset_id": str(world.dataset_id),
        "dataset_item_id": str(world.item_id),
        "source_key": world.task.file_path,
    }
    try:
        async with world.factory() as db:
            snapshot, identity, lineage = await training_reference_snapshot(
                db,
                sample,
                world.actor,
                freeze_lineage=True,
            )
            snapshot.close()
            sample.update(asset_reference=identity, source_lineage=lineage)
        await set_gate(world, "active", 3)
        async with world.factory() as db:
            with pytest.raises(
                snapshots.ContentError, match="storage_maintenance_changed"
            ):
                await lock_source_maintenance(db, {"samples": [sample]})
        from app.services.training_dataset_bridge import TrainingDatasetError

        async with world.factory() as db:
            with pytest.raises(TrainingDatasetError) as error:
                await training_reference_snapshot(db, sample, world.actor)
            assert error.value.code == "storage_maintenance_changed"
    finally:
        await clean_gate(world)


async def test_permit_settlement_failure_disposes_captured_snapshot(monkeypatch):
    closed = []

    @asynccontextmanager
    async def failing_permit(*args, **kwargs):
        yield None
        raise snapshots.ContentError("storage_operation_unsettled", 503)

    async def acquire(*args, **kwargs):
        return failing_permit()

    async def capture(*args, **kwargs):
        class Result:
            def close(self):
                closed.append(True)

        return Result()

    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(snapshots, "_capture_bounded", capture)
    with pytest.raises(snapshots.ContentError, match="storage_operation_unsettled"):
        await snapshots.capture_from_location(None, uuid.uuid4(), lambda: io.BytesIO())
    assert closed == [True]


async def test_retained_training_copy_does_not_check_source_maintenance(monkeypatch):
    from types import SimpleNamespace

    from app.services import training_dataset_storage as owners
    from app.workers import training_dataset as worker
    from tests.test_training_storage_targets import target

    project_id, version_id = uuid.uuid4(), uuid.uuid4()
    version = SimpleNamespace(
        id=version_id,
        project_id=project_id,
        storage_ref=owners.make_ref(
            target(legacy=True), 1, owners.prefix_for(project_id, version_id)
        ),
    )
    owner = owners.resolve(version)
    retained = {"task_id": "frozen", "sha256": "a" * 64}
    monkeypatch.setattr(worker, "materialize_image", lambda *args, **kwargs: retained)

    async def forbidden(*args, **kwargs):
        pytest.fail("Retained copies must not depend on current source access")

    monkeypatch.setattr(storage_access, "check", forbidden)
    monkeypatch.setattr(worker, "assert_dataset_authority", forbidden)
    assert (
        await worker.materialize_sample(
            None,
            version,
            {"task_id": retained["task_id"]},
            owner.prefix.rstrip("/"),
            "digest",
            None,
            None,
        )
        is retained
    )


async def test_cache_publication_rejects_fill_from_before_resume(cache_world):
    from app.db.models.dataset_asset import DatasetAssetLocation
    from app.db.models.original_cache import OriginalCacheEntry

    world, storage = cache_world, cache.make_storage()
    record = None
    try:
        record, status = await cache.reserve(
            world.factory, world.source_id, world.asset_id, storage
        )
        assert status == "reserved"
        await set_gate(world, "active", 3)
        with pytest.raises(snapshots.ContentError, match="storage_maintenance_changed"):
            await cache.publish(world.factory, record, storage)
        async with world.factory() as db:
            assert (await db.get(OriginalCacheEntry, record.id)).state == "uploading"
            assert (
                await db.get(
                    DatasetAssetLocation,
                    (record.asset_id, record.location_id, record.bucket),
                )
                is None
            )
    finally:
        if record:
            await cache.retire(world.factory, record.id, uncertain=False)
        storage.close()
        await clean_gate(world)


async def test_uncertain_source_close_stays_visible_to_permit(monkeypatch):
    seen = []

    @asynccontextmanager
    async def permit():
        try:
            yield None
        except BaseException as exc:
            seen.append(getattr(exc, "storage_operation_unsettled", False))
            raise

    async def acquire(*args, **kwargs):
        return permit()

    async def capture(*args, **kwargs):
        error = OSError("source close failed")
        storage_access.mark_unsettled(error)
        raise error

    monkeypatch.setattr(storage_access, "acquire", acquire)
    monkeypatch.setattr(snapshots, "_capture_bounded", capture)
    with pytest.raises(
        snapshots.ContentError, match="storage_operation_unsettled"
    ) as error:
        await snapshots.capture_from_location(None, uuid.uuid4(), lambda: None)
    assert seen == [True] and error.value.storage_operation_unsettled


def test_asyncio_shutdown_waits_for_physical_read_before_settlement(monkeypatch):
    from contextlib import contextmanager
    from tests.test_content_snapshot import image_bytes

    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    settled = []
    monkeypatch.setattr(snapshots, "_slots", threading.BoundedSemaphore(1))

    @asynccontextmanager
    async def permit():
        try:
            yield None
        finally:
            settled.append(closed.is_set())

    async def acquire(*args, **kwargs):
        return permit()

    class Blocked(io.BytesIO):
        def read(self, count):
            entered.set()
            assert release.wait(3)
            return super().read(count)

    @contextmanager
    def factory():
        try:
            with Blocked(image_bytes()) as body:
                yield body, {"size": len(image_bytes())}
        finally:
            closed.set()

    timer = threading.Timer(0.1, release.set)
    monkeypatch.setattr(storage_access, "acquire", acquire)

    async def main():
        task = asyncio.create_task(
            snapshots.capture_from_location(None, uuid.uuid4(), factory)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        timer.start()
        return task  # asyncio.run cancels this Task in its shutdown sweep.

    try:
        pending = asyncio.run(main())
        assert pending.cancelled()
        assert settled == [True] and snapshots._slots._value == 1
    finally:
        release.set()
        if timer.ident is not None:
            timer.join(1)


@pytest.mark.parametrize("failure", ["body", "client", "read"])
def test_cache_marks_only_unproved_handle_closure(monkeypatch, failure):
    from types import SimpleNamespace

    settled = threading.Event()

    class Body(io.BytesIO):
        def close(self):
            if failure == "body":
                raise OSError("body close failed")
            super().close()

    class Client:
        meta = SimpleNamespace(endpoint_url="https://cache.example")

        def get_object(self, **kwargs):
            return {"Body": body, "ContentLength": 1}

        def close(self):
            if failure == "client":
                raise OSError("client close failed")

    body = Body(b"x")
    record = SimpleNamespace(
        storage_identity="same",
        asset_id=uuid.uuid4(),
        id=uuid.uuid4(),
        sha256="a" * 64,
        size_bytes=1,
        bucket="cache",
        location_id=LEGACY_LOCATION_ID,
        _target=SimpleNamespace(endpoint_url=Client.meta.endpoint_url),
    )
    monkeypatch.setattr(
        cache,
        "make_storage",
        lambda: SimpleNamespace(client=Client(), media_cache_bucket="cache"),
    )
    try:
        with pytest.raises(OSError) as error:
            with cache.stored_object(record, settled) as (stream, _):
                assert stream.read() == b"x"
                if failure == "read":
                    raise OSError("read failed")
        assert getattr(error.value, "storage_operation_unsettled", False) == (
            failure != "read"
        )
        assert settled.is_set() == (failure == "read")
    finally:
        io.BytesIO.close(body)


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("revision", [1, 3])
async def test_legacy_native_mask_provenance_uses_initial_epoch(
    monkeypatch, replay, revision
):
    from types import SimpleNamespace
    from app.services import (
        reference_inference as inference,
        ai_mask_accept,
        raster_mask_storage,
    )

    identity = {
        "schema": 1,
        "item_id": str(uuid.uuid4()),
        "asset_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
    }
    decision = (
        SimpleNamespace(response_json={"_reference_input": identity})
        if replay
        else None
    )
    task = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
    actor = SimpleNamespace(id=uuid.uuid4())

    class Owner:
        binding = {
            "reference": {
                "source_id": "source",
                "source_revision": 1,
                "accepted_revision_id": None,
            }
        }

        def __init__(self, *args):
            pass

        async def capture(self, **kwargs):
            pass

        async def final(self):
            pass

        async def close(self):
            pass

        def identity(self):
            return {**identity, "maintenance_revision": revision}

    async def scalar(query):
        return decision

    async def get(*args, **kwargs):
        return object()

    monkeypatch.setattr(inference, "ReferenceImageOperation", Owner)
    monkeypatch.setattr(inference, "resolve_project_access", get)
    monkeypatch.setattr(
        ai_mask_accept,
        "_validate_receipt",
        lambda *args, **kwargs: {"reference_input": identity},
    )
    monkeypatch.setattr(
        ai_mask_accept, "_validate_replay", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        ai_mask_accept, "ai_mask_accept_request_digest", lambda data: "request"
    )
    monkeypatch.setattr(
        raster_mask_storage, "build_rle_reference", lambda data: {"sha256": "a" * 64}
    )
    db = SimpleNamespace(
        scalar=scalar,
        get=get,
        sync_session=SimpleNamespace(
            in_nested_transaction=lambda: False, get_transaction=lambda: object()
        ),
    )
    data = SimpleNamespace(
        target=SimpleNamespace(mode="create"),
        idempotency_key=uuid.uuid4(),
        candidate=SimpleNamespace(
            candidate=SimpleNamespace(
                value=SimpleNamespace(
                    rle=SimpleNamespace(model_dump=lambda **kwargs: {})
                )
            )
        ),
    )
    if revision == 1:
        receipt = await inference.qualify_native_acceptance(db, task, actor, None, data)
        assert receipt.task_id == task.id
    else:
        with pytest.raises(
            snapshots.ContentError, match="prediction_requires_reference_provenance"
        ):
            await inference.qualify_native_acceptance(db, task, actor, None, data)
