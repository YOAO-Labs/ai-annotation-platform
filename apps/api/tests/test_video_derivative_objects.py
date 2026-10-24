"""Independent committed video owners, durable access and precise GC regressions.

Only the repository's verified disposable test engine is used. The fake provider
owns all object I/O; every committed fixture and uncertain test permit is removed
after its fake physical client has actually closed.
"""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import timedelta
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem, VideoChunk, VideoFrameCache
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.user import User
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import storage_access
from app.services import video_derivative_objects as objects
from app.services import video_derivative_storage as physical
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from tests.factory import make_user_dict
from tests.test_video_derivative_storage import Provider, Storage


@pytest.fixture
async def owned(test_engine, monkeypatch):
    """Committed rows let the real output permit use an independent connection."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        await ensure_legacy_bindings(db)
        await db.commit()
        await storage_access.ensure(db, LEGACY_LOCATION_ID)
        user = User(
            **make_user_dict(
                "super_admin", f"video-owner-{uuid.uuid4()}@test.local", "Video owner"
            )
        )
        db.add(user)
        await db.flush()
        dataset = Dataset(
            display_id="D-VO-" + uuid.uuid4().hex[:10],
            name="Video object ownership",
            created_by=user.id,
            data_type="video",
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="owned.mp4",
            file_path="saved/" + uuid.uuid4().hex + ".mp4",
            file_type="video",
        )
        db.add(item)
        await db.commit()
        user_id, dataset_id, item_id = user.id, dataset.id, item.id
    provider = Provider()
    monkeypatch.setattr(physical, "StorageService", lambda: Storage(provider))
    context = SimpleNamespace(
        factory=factory,
        engine=test_engine,
        user_id=user_id,
        dataset_id=dataset_id,
        item_id=item_id,
        provider=provider,
    )
    try:
        yield context
    finally:
        # Failed close tests restore the fake method before their teardown, so
        # removing the owned durable test records never hides a live resource.
        provider.close()
        async with factory() as db:
            identifiers = list(
                await db.scalars(
                    select(Output.id).where(Output.dataset_item_id == item_id)
                )
            )
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            if identifiers:
                await db.execute(
                    delete(StorageOperation).where(
                        StorageOperation.purpose.in_(
                            [objects.purpose(identifier) for identifier in identifiers]
                        )
                    )
                )
                await db.execute(
                    update(Output)
                    .where(
                        Output.id.in_(identifiers),
                        Output.state.in_(("building", "ready")),
                    )
                    .values(state="retiring")
                )
                await db.execute(
                    update(Output)
                    .where(Output.id.in_(identifiers), Output.state == "retiring")
                    .values(state="deleted")
                )
                await db.execute(delete(Output).where(Output.id.in_(identifiers)))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def _new(db, owned, kind="metadata", *, old=False):
    row = await objects.allocate(
        db,
        dataset_item_id=owned.item_id,
        kind=kind,
        spec={"profile": "owned-test"},
        original_ref={"schema": 1, "sha256": "a" * 64},
        auth_ref={"schema": 1, "mode": "system"},
        metadata_output_id=None if kind == "metadata" else uuid.uuid4(),
    )
    if old:
        row.updated_at = objects.utcnow() - timedelta(days=60)
    await db.commit()
    return row


async def _ready(db, owned, kind="metadata", *, old=False):
    row = await _new(db, owned, kind)
    await objects.claim(db, row)
    owner = physical.resolve(row)
    body = b'{"schema":1}'
    row.inventory = {
        "schema": 1,
        "objects": {
            owner.key("metadata.json"): {
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
            }
        },
    }
    row.result = {"schema": 1}
    row.state = "ready"
    if old:
        row.updated_at = objects.utcnow() - timedelta(days=60)
    await db.commit()
    owned.provider.objects[(owner.bucket, owner.key("metadata.json"))] = body
    return row


async def _cached(db, owned, kind="frame", *, old=True):
    output = await _ready(db, owned, kind, old=old)
    values = {
        "dataset_item_id": owned.item_id,
        "output_id": output.id,
        "status": "ready",
        "storage_key": physical.resolve(output).key(
            "frame.webp" if kind == "frame" else "chunk.mp4"
        ),
        "byte_size": 17,
        "last_accessed_at": objects.utcnow() - timedelta(days=60)
        if old
        else objects.utcnow(),
    }
    row = (
        VideoFrameCache(frame_index=23, width=512, format="webp", **values)
        if kind == "frame"
        else VideoChunk(
            chunk_id=23,
            start_frame=230,
            end_frame=239,
            generation_mode="smart_copy",
            diagnostics={"codec": "h264"},
            **values,
        )
    )
    db.add(row)
    await db.commit()
    return output, row


def _unknown(identifier):
    return StorageOperation(
        id=uuid.uuid4(),
        location_id=LEGACY_LOCATION_ID,
        revision=1,
        purpose=objects.purpose(identifier),
        node_id="video-objects:test-unsettled",
    )


async def _old(db, output):
    output.updated_at = objects.utcnow() - timedelta(days=60)
    await db.commit()


async def test_allocation_is_metadata_only_and_never_commits_caller(owned, monkeypatch):
    async with owned.factory() as db:
        item = await db.get(DatasetItem, owned.item_id)
        item.file_name = "pending-uncommitted-name.mp4"
        commit = AsyncMock(side_effect=AssertionError("must not commit caller"))
        monkeypatch.setattr(db, "commit", commit)
        row = await objects.allocate(
            db,
            dataset_item_id=owned.item_id,
            kind="metadata",
            spec={"a": [1]},
            original_ref={"x": 1},
            auth_ref={"x": 2},
        )
        assert row.state == "building" and row.lease_token is None
        assert owned.provider.calls == []
        async with owned.factory() as observer:
            assert await observer.get(Output, row.id) is None
            assert (
                await observer.get(DatasetItem, owned.item_id)
            ).file_name == "owned.mp4"
        assert not commit.called
        await db.rollback()


async def test_snapshot_copies_all_model_facts_without_aliases(owned):
    async with owned.factory() as db:
        row = await _ready(db, owned)
        frozen = objects.snapshot(row)
        assert set(frozen.__dataclass_fields__) == set(Output.__table__.columns.keys())
        with pytest.raises(FrozenInstanceError):
            frozen.state = "deleted"
        frozen.spec["changed"] = True
        frozen.auth_ref["mode"] = "tampered"
        frozen.inventory["objects"].clear()
        assert row.spec == {"profile": "owned-test"}
        assert row.auth_ref["mode"] == "system"
        assert len(row.inventory["objects"]) == 1


async def test_lease_is_claimed_once_and_never_renewed(owned, monkeypatch):
    async with owned.factory() as db:
        row = await _new(db, owned)
        first = await objects.claim(db, row)
        expires = row.lease_expires_at
        assert isinstance(first, uuid.UUID) and expires > objects.utcnow()
        assert await objects.claim(db, row) is None
        monkeypatch.setattr(objects, "utcnow", lambda: expires + timedelta(seconds=1))
        assert await objects.claim(db, row) is None
        assert row.lease_token == first and row.lease_expires_at == expires
        await db.rollback()


@pytest.mark.parametrize("ttl", [True, 0, -1, 901])
async def test_invalid_lease_budget_cannot_change_attempt(owned, ttl):
    async with owned.factory() as db:
        row = await _new(db, owned)
        with pytest.raises(ContentError):
            await objects.claim(db, row, ttl_seconds=ttl)
        assert row.lease_token is None


async def test_access_keeps_client_inside_real_permit_and_does_not_commit_caller(owned):
    async with owned.factory() as db:
        row = await _new(db, owned)
        frozen = objects.snapshot(row)
        item = await db.get(DatasetItem, owned.item_id)
        item.file_name = "caller-dirty.mp4"
        phases = []

        async def guard(revision):
            async with owned.factory() as observer:
                operations = list(
                    await observer.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose == objects.purpose(row.id)
                        )
                    )
                )
                assert len(operations) == 1 and operations[0].revision == revision
                assert (
                    await observer.get(DatasetItem, owned.item_id)
                ).file_name == "owned.mp4"
            phases.append(owned.provider.closed)

        async with objects.access(db, frozen, guard) as client:
            assert client.client is owned.provider
            assert phases == [False] and not owned.provider.closed
        assert phases == [False, True]
        async with owned.factory() as observer:
            assert not list(
                await observer.scalars(
                    select(StorageOperation).where(
                        StorageOperation.purpose == objects.purpose(row.id)
                    )
                )
            )
        await db.rollback()


async def test_access_late_guard_rejects_after_actual_client_close(owned):
    async with owned.factory() as db:
        row = await _new(db, owned)
        checks = []

        async def guard(revision):
            checks.append(owned.provider.closed)
            if len(checks) == 2:
                raise ContentError("late_revoke", 403)

        with pytest.raises(ContentError) as caught:
            async with objects.access(db, objects.snapshot(row), guard):
                pass
        assert caught.value.code == "late_revoke" and checks == [False, True]
        assert not list(
            await db.scalars(
                select(StorageOperation).where(
                    StorageOperation.purpose == objects.purpose(row.id)
                )
            )
        )


async def test_unknown_client_close_retains_exact_operation(owned):
    original = owned.provider.close

    def broken():
        raise OSError("private unresolved close")

    owned.provider.close = broken
    try:
        async with owned.factory() as db:
            row = await _new(db, owned)
            with pytest.raises(physical.VideoDerivativeStorageError) as caught:
                async with objects.access(db, objects.snapshot(row), AsyncMock()):
                    pass
            assert caught.value.storage_operation_unsettled is True
            assert (
                len(
                    list(
                        await db.scalars(
                            select(StorageOperation).where(
                                StorageOperation.purpose == objects.purpose(row.id)
                            )
                        )
                    )
                )
                == 1
            )
    finally:
        owned.provider.close = original
        original()


async def test_cancelled_constructor_drains_and_closes_its_late_client(
    owned, monkeypatch
):
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), __import__("threading").Event()

    def delayed():
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise AssertionError("constructor was not released")
        return Storage(owned.provider)

    monkeypatch.setattr(physical, "StorageService", delayed)
    async with owned.factory() as db:
        row = await _new(db, owned)

        async def run():
            async with objects.access(db, objects.snapshot(row), AsyncMock()):
                pytest.fail("cancelled enter must not deliver the client")

        task = asyncio.create_task(run())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owned.provider.closed
        assert not list(
            await db.scalars(
                select(StorageOperation).where(
                    StorageOperation.purpose == objects.purpose(row.id)
                )
            )
        )


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_ttl_detaches_known_ready_refs_then_starts_owner_grace(owned, kind):
    async with owned.factory() as db:
        output, row = await _cached(db, owned, kind)
        assert await objects.reconcile(db) == 0
        await db.refresh(row)
        await db.refresh(output)
        assert (
            row.output_id is None
            and row.status == "pending"
            and row.storage_key is None
            and row.byte_size is None
        )
        assert (
            output.state == "retiring"
            and output.updated_at > objects.utcnow() - timedelta(seconds=10)
        )
        assert owned.provider.calls == []
        if kind == "chunk":
            assert row.generation_mode is None and row.diagnostics == {}


async def test_unknown_legacy_cache_key_is_never_deleted_or_detached(owned):
    async with owned.factory() as db:
        row = VideoFrameCache(
            dataset_item_id=owned.item_id,
            frame_index=0,
            width=512,
            format="webp",
            status="ready",
            storage_key="videos/legacy/frames/0.webp",
            last_accessed_at=objects.utcnow() - timedelta(days=60),
        )
        db.add(row)
        await db.commit()
        assert await objects.reconcile(db) == 0
        await db.refresh(row)
        assert (
            row.status == "ready" and row.storage_key == "videos/legacy/frames/0.webp"
        )
        assert owned.provider.calls == []


async def test_unknown_operations_are_filtered_before_ttl_and_orphan_limits(owned):
    async with owned.factory() as db:
        first, cache = await _cached(db, owned)
        blocker = _unknown(first.id)
        db.add(blocker)
        second = await _new(db, owned, old=True)
        second.state = "retiring"
        second.updated_at = objects.utcnow() - timedelta(days=59)
        await db.commit()
        owner = physical.resolve(second)
        owned.provider.objects[(owner.bucket, owner.key("poster.webp"))] = (
            b"owned partial"
        )
        assert await objects.reconcile(db, limit=1) == 1
        await db.refresh(cache)
        await db.refresh(first)
        await db.refresh(second)
        assert cache.output_id == first.id and first.state == "ready"
        assert second.state == "deleted"


async def test_active_metadata_is_not_expired_with_frame_ttl(owned):
    async with owned.factory() as db:
        row = await _ready(db, owned, old=True)
        item = await db.get(DatasetItem, owned.item_id)
        item.video_output_id = row.id
        await db.commit()
        assert await objects.reconcile(db) == 0
        await db.refresh(item)
        await db.refresh(row)
        assert item.video_output_id == row.id and row.state == "ready"
        assert owned.provider.calls == []


@pytest.mark.parametrize("kind", ["metadata", "frame", "chunk"])
async def test_expired_attempt_clears_only_matching_pending_pointer(
    owned, monkeypatch, kind
):
    async with owned.factory() as db:
        active = await _ready(db, owned)
        output = await _new(db, owned, kind)
        await objects.claim(db, output)
        item = await db.get(DatasetItem, owned.item_id)
        item.video_output_id = active.id
        if kind == "metadata":
            item.video_building_output_id = output.id
            row = None
        elif kind == "frame":
            row = VideoFrameCache(
                dataset_item_id=owned.item_id,
                output_id=output.id,
                frame_index=9,
                width=512,
                format="webp",
                status="pending",
            )
            db.add(row)
        else:
            row = VideoChunk(
                dataset_item_id=owned.item_id,
                output_id=output.id,
                chunk_id=9,
                start_frame=90,
                end_frame=99,
                status="pending",
            )
            db.add(row)
        await db.commit()
        now = objects.utcnow() + timedelta(hours=2)
        monkeypatch.setattr(objects, "utcnow", lambda: now)
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        await db.refresh(item)
        assert output.state == "deleted" and output.error_code == "lease_expired"
        assert (
            item.video_output_id == active.id and item.video_building_output_id is None
        )
        if row is not None:
            await db.refresh(row)
            assert row.output_id is None and row.status == "pending"


async def test_live_lease_and_unknown_operation_prevent_expiration(owned, monkeypatch):
    async with owned.factory() as db:
        output = await _new(db, owned)
        await objects.claim(db, output)
        item = await db.get(DatasetItem, owned.item_id)
        item.video_building_output_id = output.id
        await db.commit()
        assert await objects.reconcile(db) == 0
        db.add(_unknown(output.id))
        await db.commit()
        now = objects.utcnow() + timedelta(hours=2)
        monkeypatch.setattr(objects, "utcnow", lambda: now)
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        await db.refresh(item)
        assert output.state == "building" and item.video_building_output_id == output.id
        assert owned.provider.calls == []


async def test_published_orphan_waits_fresh_grace_before_saved_prefix_deletion(owned):
    async with owned.factory() as db:
        output = await _ready(db, owned, old=True)
        owner = physical.resolve(output)
        foreign_key = physical.prefix_for(uuid.uuid4()) + "metadata.json"
        owned.provider.objects[(owner.bucket, foreign_key)] = b"another owner"
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        assert (
            output.state == "retiring"
            and output.updated_at > objects.utcnow() - timedelta(seconds=10)
        )
        assert owned.provider.calls == []
        await _old(db, output)
        assert await objects.reconcile(db) == 1
        await db.refresh(output)
        assert output.state == "deleted"
        assert (
            owned.provider.closed
            and (owner.bucket, foreign_key) in owned.provider.objects
        )
        assert all(
            kwargs["Bucket"] == owner.bucket
            for kind, kwargs in owned.provider.calls
            if kind in {"list", "delete"}
        )
        assert await db.get(Output, output.id) is output


async def test_failed_physical_delete_preserves_retiring_responsibility(owned):
    owned.provider.ignore_delete = True
    async with owned.factory() as db:
        output = await _new(db, owned, old=True)
        owner = physical.resolve(output)
        owned.provider.objects[(owner.bucket, owner.key("poster.webp"))] = (
            b"owned partial"
        )
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        assert output.state == "retiring" and owned.provider.closed
        assert (owner.bucket, owner.key("poster.webp")) in owned.provider.objects


async def test_late_unknown_operation_blocks_gc_before_client_construction(
    owned, monkeypatch
):
    async with owned.factory() as db:
        output = await _new(db, owned, old=True)
        identifier = output.id
        original = storage_access.acquire

        async def raced(*args, **kwargs):
            permit = await original(*args, **kwargs)
            async with owned.factory() as independent:
                operation = _unknown(identifier)
                operation.revision = permit.revision
                independent.add(operation)
                await independent.commit()
            return permit

        monkeypatch.setattr(storage_access, "acquire", raced)
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        assert output.state == "retiring"
        assert owned.provider.calls == [] and not owned.provider.closed
        assert (
            len(
                list(
                    await db.scalars(
                        select(StorageOperation).where(
                            StorageOperation.purpose == objects.purpose(identifier)
                        )
                    )
                )
            )
            == 1
        )


async def test_gc_unknown_client_close_keeps_owner_and_operation_after_delete(owned):
    original = owned.provider.close

    def broken():
        raise OSError("private unresolved GC close")

    owned.provider.close = broken
    try:
        async with owned.factory() as db:
            output = await _new(db, owned, old=True)
            owner = physical.resolve(output)
            owned.provider.objects[(owner.bucket, owner.key("poster.webp"))] = (
                b"owned partial"
            )
            assert await objects.reconcile(db) == 0
            await db.refresh(output)
            assert output.state == "retiring"
            assert (
                owner.bucket,
                owner.key("poster.webp"),
            ) not in owned.provider.objects
            assert (
                len(
                    list(
                        await db.scalars(
                            select(StorageOperation).where(
                                StorageOperation.purpose == objects.purpose(output.id)
                            )
                        )
                    )
                )
                == 1
            )
    finally:
        owned.provider.close = original
        original()


async def test_business_cascade_retains_output_until_precise_collection(owned):
    async with owned.factory() as db:
        output, row = await _cached(db, owned)
        identifier = output.id
        await db.execute(delete(Dataset).where(Dataset.id == owned.dataset_id))
        await db.commit()
        assert await db.get(Output, identifier) is not None
        assert await objects.reconcile(db) == 0
        await db.refresh(output)
        assert output.state == "retiring"
        await _old(db, output)
        assert await objects.reconcile(db) == 1
        await db.refresh(output)
        assert output.state == "deleted"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 1001},
        {"grace_seconds": 3599},
        {"grace_seconds": True},
    ],
)
async def test_invalid_gc_budget_rejects_without_provider_io(owned, kwargs):
    async with owned.factory() as db:
        with pytest.raises(ContentError):
            await objects.reconcile(db, **kwargs)
        assert owned.provider.calls == []
