"""Independent publication transactions and exact UUID physical ownership."""

import asyncio
import gzip
import hashlib
import io
import threading
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models.raster_mask_object import (
    RasterMaskObject as Object,
    RasterMaskPublication as Publication,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import (
    raster_mask_objects as owners,
    raster_object_storage as physical,
)
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.raster_mask_storage import (
    build_rle_gzip_reference,
    build_rle_reference,
    canonical_rle_bytes,
)


class Objects:
    def __init__(self):
        self.bucket = settings.minio_bucket
        self.client = SimpleNamespace(
            meta=SimpleNamespace(
                endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
            ),
            head_object=self.head,
            get_object=self.get,
            put_object=self.put,
            delete_object=self.delete,
        )
        self.objects, self.reads, self.puts, self.deleted, self.bodies = (
            {},
            [],
            [],
            [],
            [],
        )
        self.fail_put = self.fail_delete = False
        self.before_put = None
        self.body_factory = io.BytesIO

    def head(self, *, Bucket, Key):  # noqa: N803
        try:
            data = self.objects[Bucket, Key]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject") from None
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get(self, *, Bucket, Key, IfMatch, **kwargs):  # noqa: N803
        metadata = self.head(Bucket=Bucket, Key=Key)
        assert IfMatch == metadata["ETag"]
        self.reads.append((Bucket, Key))
        body = self.body_factory(self.objects[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def put(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):  # noqa: N803
        assert IfNoneMatch == "*"
        if self.before_put is not None:
            self.before_put(Bucket, Key)
        if self.fail_put:
            raise OSError("private candidate write failure")
        if (Bucket, Key) in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Bucket, Key] = bytes(Body)
        self.puts.append((Bucket, Key))
        return {}

    def delete(self, *, Bucket, Key, **kwargs):  # noqa: N803
        if self.fail_delete:
            raise OSError("private candidate delete failure")
        self.objects.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}


class Harness:
    def __init__(self, engine, blobs):
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.blobs, self.keys, self.identifiers = blobs, set(), set()
        self.references = set()

    def content(self):
        counts = [int(value, 16) + 1 for value in uuid.uuid4().hex]
        rle = {"encoding": "coco_rle", "size": [1, sum(counts)], "counts": counts}
        reference, data = build_rle_reference(rle), canonical_rle_bytes(rle)
        self.keys.add(reference["object_key"])
        return rle, reference, data

    async def rows(self):
        async with self.factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(Object).where(Object.logical_key.in_(self.keys))
                    )
                ).all()
            )
            self.identifiers.update(row.id for row in rows)
            return rows

    async def guards(self):
        async with self.factory() as db:
            return list(
                (
                    await db.scalars(
                        select(Publication)
                        .join(Object)
                        .where(Object.logical_key.in_(self.keys))
                    )
                ).all()
            )

    async def operations(self):
        await self.rows()
        identifiers = self.identifiers
        purposes = [
            owners.purpose(operation, identifier)
            for identifier in identifiers
            for operation in ("read", "write", "delete")
        ]
        async with self.factory() as db:
            return list(
                (
                    await db.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose.in_(purposes)
                        )
                    )
                ).all()
            )


@pytest.fixture
async def harness(test_engine, monkeypatch):
    blobs = Objects()
    result = Harness(test_engine, blobs)
    monkeypatch.setattr(physical, "storage_service", blobs)

    async def referenced_keys(db):
        return set(result.references)

    async def is_referenced(db, key):
        return key in result.references

    monkeypatch.setattr(owners, "_referenced_keys", referenced_keys)
    monkeypatch.setattr(owners, "_is_referenced", is_referenced)
    async with result.factory() as db:
        await ensure_legacy_bindings(db)
        await db.commit()
        await storage_access.ensure(db, LEGACY_LOCATION_ID)
    try:
        yield result
    finally:
        # Every body and exact receipt is owned by this fixture, including the
        # deliberately failed-close cases. Never scan a shared bucket prefix.
        for body in blobs.bodies:
            io.BytesIO.close(body)
        await result.rows()
        identifiers = result.identifiers
        purposes = [
            owners.purpose(operation, identifier)
            for identifier in identifiers
            for operation in ("read", "write", "delete")
        ]
        async with result.factory() as db:
            await db.execute(
                delete(StorageOperation).where(StorageOperation.purpose.in_(purposes))
            )
            await db.execute(
                delete(Publication).where(Publication.object_id.in_(identifiers))
            )
            await db.execute(delete(Object).where(Object.id.in_(identifiers)))
            gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
            if gate is not None and gate.state != "active":
                gate.state = "active"
                gate.revision += 1
            await db.commit()
        blobs.objects.clear()


async def test_pending_and_guard_are_durable_before_put_without_business_commit(
    harness, monkeypatch
):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        await caller.execute(
            text(
                "CREATE TEMP TABLE raster_business_sentinel (value int) ON COMMIT DROP"
            )
        )
        await caller.execute(text("INSERT INTO raster_business_sentinel VALUES (7)"))
        # The independent allocation must not reacquire the caller's content lock.
        await caller.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "aap:raster-mask:" + reference["object_key"]},
        )
        original = owners._io

        async def io_call(db, receipt, revision, operation, storage, *args, **kwargs):
            assert operation == "write"
            rows, guards = await harness.rows(), await harness.guards()
            assert len(rows) == len(guards) == 1
            assert rows[0].state == "pending" and guards[0].object_id == rows[0].id
            assert not harness.blobs.puts
            return await original(
                db, receipt, revision, operation, storage, *args, **kwargs
            )

        monkeypatch.setattr(owners, "_io", io_call)
        assert (
            await asyncio.wait_for(
                owners.store(reference, data, db=caller, storage=harness.blobs), 5
            )
            == reference
        )
        assert (
            await caller.scalar(text("SELECT value FROM raster_business_sentinel")) == 7
        )
        await caller.rollback()
    assert (await harness.rows())[0].state == "ready"
    assert len(await harness.guards()) == 1


async def test_progress_commit_and_binding_rollback_preserve_exact_guard(harness):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        original = {row.id for row in await harness.guards()}
        await caller.commit()
        assert {row.id for row in await harness.guards()} == original
        assert await owners.bind_publications(caller, {"mask": reference}) == 1
        await caller.rollback()
        assert {row.id for row in await harness.guards()} == original
        assert await owners.bind_publications(caller, {"mask": reference}) == 1
        await caller.commit()
        assert not await harness.guards()
        assert await owners.bind_publications(caller, reference) == 0
        await caller.rollback()


async def test_read_after_binding_in_same_transaction_does_not_upgrade_owner_lock(
    harness,
):
    rle, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert await owners.bind_publications(caller, reference) == 1
        assert await owners.load(reference, db=caller, storage=harness.blobs) == rle
        assert (
            await owners.store(reference, data, db=caller, storage=harness.blobs)
            == reference
        )
        assert await owners.bind_publications(caller, reference) == 1
        await caller.commit()
    assert not await harness.guards()


async def test_explicit_null_storage_encoding_preserves_identity_reference(harness):
    rle, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        value = {**reference, "storage_encoding": None}
        assert await owners.load(value, db=caller, storage=harness.blobs) == rle
        assert await owners.bind_publications(caller, value) == 1
        await caller.commit()


async def test_binding_does_not_delete_another_callers_guard(harness):
    _, reference, data = harness.content()
    async with harness.factory() as first, harness.factory() as second:
        await owners.store(reference, data, db=first, storage=harness.blobs)
        await owners.store(reference, data, db=second, storage=harness.blobs)
        first_id = next(
            iter(first.info[owners.PUBLICATIONS_INFO][reference["object_key"]])
        )
        assert len(await harness.guards()) == 2
        assert await owners.bind_publications(first, reference) == 1
        await first.commit()
        remaining = await harness.guards()
        assert len(remaining) == 1 and remaining[0].id != first_id
        assert await owners.bind_publications(second, reference) == 1
        await second.commit()


async def test_binding_one_key_preserves_same_callers_other_key_guard(harness):
    _, first, first_data = harness.content()
    _, second, second_data = harness.content()
    async with harness.factory() as caller:
        await owners.store(first, first_data, db=caller, storage=harness.blobs)
        await owners.store(second, second_data, db=caller, storage=harness.blobs)
        assert await owners.bind_publications(caller, first) == 1
        await caller.commit()
        remaining = await harness.guards()
        assert len(remaining) == 1
        rows = {row.id: row for row in await harness.rows()}
        assert rows[remaining[0].object_id].logical_key == second["object_key"]
        assert await owners.bind_publications(caller, [first, second]) == 1
        await caller.commit()


async def test_failed_put_keeps_pending_and_guard_and_never_retries_it(harness):
    _, reference, data = harness.content()
    harness.blobs.fail_put = True
    async with harness.factory() as caller:
        with pytest.raises(physical.RasterObjectStorageError):
            await owners.store(reference, data, db=caller, storage=harness.blobs)
        pending = (await harness.rows())[0]
        assert pending.state == "pending" and len(await harness.guards()) == 1
        harness.blobs.fail_put = False
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        rows = await harness.rows()
        assert len(rows) == 2 and {row.state for row in rows} == {"pending", "ready"}
        assert len(await harness.guards()) == 2
        assert harness.blobs.puts[0][1] != pending.storage_ref["object_key"]


async def test_unknown_legacy_reference_and_retired_only_owner_have_no_fallback_io(
    harness,
):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        with pytest.raises(physical.RasterObjectStorageError) as error:
            await owners.load(reference, db=caller, storage=harness.blobs)
        assert error.value.code == "raster_object_storage_unpinned"
        assert not harness.blobs.reads and not harness.blobs.puts
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        await owners.bind_publications(caller, reference)
        await caller.commit()
        async with harness.factory() as db:
            row = await db.scalar(
                select(Object).where(Object.logical_key == reference["object_key"])
            )
            row.state, row.retired_at = "retired", owners.now()
            await db.commit()
        before = len(harness.blobs.reads)
        with pytest.raises(physical.RasterObjectStorageError) as error:
            await owners.load(reference, db=caller, storage=harness.blobs)
        assert error.value.code == "raster_object_storage_unpinned"
        assert len(harness.blobs.reads) == before


async def test_ready_reuse_reads_saved_bucket_and_never_allocates_new_candidate(
    harness,
):
    rle, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        receipt = (await harness.rows())[0]
        harness.blobs.bucket = "different-current-bucket"
        assert (
            await owners.store(reference, data, db=caller, storage=harness.blobs)
            == reference
        )
        assert await owners.load(reference, db=caller, storage=harness.blobs) == rle
    assert len(await harness.rows()) == 1 and len(harness.blobs.puts) == 1
    assert all(
        bucket == receipt.storage_ref["bucket"] for bucket, _ in harness.blobs.reads
    )
    assert all(body.closed for body in harness.blobs.bodies)


async def test_gzip_reuse_verifies_winners_own_stored_bytes_not_new_wrapper(harness):
    rle, _, data = harness.content()
    reference = build_rle_gzip_reference(rle)
    harness.keys.add(reference["object_key"])
    first, second = gzip.compress(data, mtime=1), gzip.compress(data, mtime=2)
    assert first != second
    async with harness.factory() as caller:
        await owners.store(reference, first, db=caller, storage=harness.blobs)
        assert (
            await owners.store(reference, second, db=caller, storage=harness.blobs)
            == reference
        )
        row = (await harness.rows())[0]
        assert row.stored_sha256 == hashlib.sha256(first).hexdigest()
        assert (
            harness.blobs.objects[
                row.storage_ref["bucket"], row.storage_ref["object_key"]
            ]
            == first
        )
        assert len(harness.blobs.puts) == 1
        assert await owners.load(reference, db=caller, storage=harness.blobs) == rle


async def test_corrupt_ready_cannot_be_reused_or_overwritten(harness):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        row = (await harness.rows())[0]
        locator = row.storage_ref["bucket"], row.storage_ref["object_key"]
        corrupt = b"x" * len(data)
        harness.blobs.objects[locator] = corrupt
        with pytest.raises(physical.RasterObjectStorageError):
            await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert harness.blobs.objects[locator] == corrupt
        assert len(harness.blobs.puts) == 1 and not harness.blobs.deleted
        assert len(await harness.guards()) == 2


async def test_candidate_competition_transfers_guard_and_retires_known_loser(
    harness, monkeypatch
):
    _, reference, data = harness.content()
    entered, release = threading.Event(), threading.Event()
    count, lock = 0, threading.Lock()

    def before_put(bucket, key):
        nonlocal count
        with lock:
            count += 1
            first = count == 1
        if first:
            entered.set()
            assert release.wait(10)

    harness.blobs.before_put = before_put
    async with harness.factory() as first, harness.factory() as second:
        task = asyncio.create_task(
            owners.store(reference, data, db=first, storage=harness.blobs)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert (
                await owners.store(reference, data, db=second, storage=harness.blobs)
                == reference
            )
            release.set()
            assert await task == reference
            rows, guards = await harness.rows(), await harness.guards()
            ready = next(row for row in rows if row.state == "ready")
            loser = next(row for row in rows if row.state == "retired")
            assert ready.id != loser.id and len(harness.blobs.puts) == 2
            assert len(guards) == 2 and {row.object_id for row in guards} == {ready.id}
            assert len(harness.blobs.reads) >= 3
            assert await owners.bind_publications(first, reference) == 1
            await first.commit()
            assert len(await harness.guards()) == 1
        finally:
            release.set()
            if not task.done():
                await storage_access.finish(task)


async def test_repeated_cancel_drains_write_before_exiting_and_keeps_pending(harness):
    _, reference, data = harness.content()
    entered, release = threading.Event(), threading.Event()

    def before_put(bucket, key):
        entered.set()
        assert release.wait(10)

    harness.blobs.before_put = before_put
    async with harness.factory() as caller:
        task = asyncio.create_task(
            owners.store(reference, data, db=caller, storage=harness.blobs)
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert len(await harness.operations()) == 1
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await harness.rows())[0].state == "pending"
            assert len(await harness.guards()) == 1
            assert not await harness.operations()
            assert all(body.closed for body in harness.blobs.bodies)
        finally:
            release.set()
            if not task.done():
                try:
                    await storage_access.finish(task)
                except asyncio.CancelledError:
                    pass


async def test_unknown_close_preserves_uuid_operation_and_pending(harness):
    class Unclosed(io.BytesIO):
        def close(self):
            raise OSError("private close failed")

    _, reference, data = harness.content()
    harness.blobs.body_factory = Unclosed
    async with harness.factory() as caller:
        with pytest.raises(physical.RasterObjectStorageError) as error:
            await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert error.value.storage_operation_unsettled
    assert (await harness.rows())[0].state == "pending"
    assert len(await harness.guards()) == len(await harness.operations()) == 1


@pytest.mark.parametrize("operation", ["write", "read"])
async def test_gate_revision_changed_after_physical_io_blocks_handoff(
    harness, monkeypatch, operation
):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        if operation == "read":
            await owners.store(reference, data, db=caller, storage=harness.blobs)
        original = owners._io

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            async with harness.factory() as db:
                gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
                gate.revision += 1
                await db.commit()
            return result

        monkeypatch.setattr(owners, "_io", changed)
        with pytest.raises(ValueError) as error:
            if operation == "read":
                await owners.load(reference, db=caller, storage=harness.blobs)
            else:
                await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert error.value.code == "storage_maintenance_changed"
        assert (await harness.rows())[0].state == (
            "ready" if operation == "read" else "pending"
        )
        assert len(await harness.guards()) == 1
        assert all(body.closed for body in harness.blobs.bodies)


async def test_binding_changed_gate_keeps_guard_on_rollback(harness):
    _, reference, data = harness.content()
    async with harness.factory() as caller:
        await owners.store(reference, data, db=caller, storage=harness.blobs)
        async with harness.factory() as db:
            gate = await db.get(StorageMaintenanceGate, LEGACY_LOCATION_ID)
            gate.revision += 1
            await db.commit()
        with pytest.raises(ValueError) as error:
            await owners.bind_publications(caller, reference)
        assert error.value.code == "storage_maintenance_changed"
        await caller.rollback()
    assert len(await harness.guards()) == 1


@pytest.mark.parametrize("stage", ["pending", "published"])
async def test_unknown_commit_never_starts_later_io_or_clears_responsibility(
    harness, monkeypatch, stage
):
    _, reference, data = harness.content()
    original = owners._commit
    calls = 0

    async def commit(db):
        nonlocal calls
        calls += 1
        await original(db)
        # Initial allocation, acquired callback, publication are separate commits.
        if calls == (1 if stage == "pending" else 3):
            raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(owners, "_commit", commit)
    async with harness.factory() as caller:
        with pytest.raises(OSError):
            await owners.store(reference, data, db=caller, storage=harness.blobs)
        assert not caller.info.get(owners.PUBLICATIONS_INFO)
    assert len(await harness.guards()) == 1
    assert (await harness.rows())[0].state == (
        "pending" if stage == "pending" else "ready"
    )
    assert len(harness.blobs.puts) == (0 if stage == "pending" else 1)


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_cancelled_commit_drains_acknowledgement_before_return(
    harness, monkeypatch, stage
):
    _, reference, data = harness.content()
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.commit
    calls = 0

    async def delayed_commit(db):
        nonlocal calls
        calls += 1
        current = calls
        result = await original(db)
        if current == stage:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(AsyncSession, "commit", delayed_commit)
    async with harness.factory() as caller:
        task = asyncio.create_task(
            owners.store(reference, data, db=caller, storage=harness.blobs)
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            row = (await harness.rows())[0]
            assert row.state == ("ready" if stage == 3 else "pending")
            assert len(await harness.guards()) == 1
            assert len(harness.blobs.puts) == (1 if stage == 3 else 0)
            assert not await harness.operations()
        finally:
            release.set()
            if not task.done():
                try:
                    await storage_access.finish(task)
                except asyncio.CancelledError:
                    pass
