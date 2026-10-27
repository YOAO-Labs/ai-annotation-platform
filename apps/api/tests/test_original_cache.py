"""Real independent transactions exercise accounting, publication and recovery."""

import asyncio
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import io
import threading
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageLocation,
)
from app.db.models.dataset_source import DatasetSource
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.db.models.user import User
from app.services import original_cache as cache
from app.services.content_snapshot import capture
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage import storage_service


class MemoryStorage:
    def __init__(self, world):
        self.world = world
        self.client = self
        self.meta = SimpleNamespace(endpoint_url=world.endpoint)
        self.media_cache_bucket = world.bucket
        self.closed = False
        world.clients.append(self)

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        if self.world.put_started:
            self.world.put_started.set()
            assert self.world.put_release.wait(5)
        content = Body.read()
        self.world.objects[Bucket, Key] = (
            b"x" * len(content) if self.world.corrupt_put else content
        )
        self.world.puts.append(Key)
        if self.world.fail_put:
            raise OSError("synthetic private signed request")

    def get_object(self, *, Bucket, Key):
        if self.world.get_started:
            self.world.get_started.set()
            assert self.world.get_release.wait(5)
        content = self.world.objects[Bucket, Key]
        body = io.BytesIO(content)
        self.world.bodies.append(body)
        return {"Body": body, "ContentLength": len(content)}

    def delete_object(self, *, Bucket, Key):
        self.world.deletions.append((Bucket, Key))
        if self.world.fail_delete:
            raise OSError("synthetic private storage failure")
        self.world.objects.pop((Bucket, Key), None)

    def close(self):
        self.closed = True


@pytest.fixture
async def cache_world(test_engine, monkeypatch):
    raw = io.BytesIO()
    with Image.new("RGB", (16, 12), (15, 31, 47)) as image:
        image.save(raw, "PNG")
    data = raw.getvalue()
    raw.close()
    world = SimpleNamespace(
        factory=async_sessionmaker(test_engine, expire_on_commit=False),
        source_id=uuid.uuid4(),
        location_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        data=data,
        sha=hashlib.sha256(data).hexdigest(),
        endpoint=storage_service.client.meta.endpoint_url,
        bucket=storage_service.media_cache_bucket,
        objects={},
        puts=[],
        deletions=[],
        clients=[],
        bodies=[],
        fail_put=False,
        corrupt_put=False,
        fail_delete=False,
        put_started=None,
        put_release=None,
        get_started=None,
        get_release=None,
    )
    monkeypatch.setattr(cache, "make_storage", lambda: MemoryStorage(world))
    async with world.factory() as db:
        await ensure_legacy_bindings(db)
        db.add(
            User(
                id=world.user_id,
                email=f"cache-{world.user_id}@example.test",
                name="Cache test",
                password_hash="unused",
            )
        )
        await db.flush()
        db.add(
            Dataset(
                id=world.dataset_id,
                display_id=f"ds-{world.dataset_id.hex[:12]}",
                name="Cache test",
                created_by=world.user_id,
            )
        )
        db.add(
            StorageLocation(
                id=world.location_id,
                name=f"cache-{world.location_id}",
                kind="filesystem",
                config={
                    "cache_policy": "on_demand",
                    "original_cache_quota_bytes": len(data) * 2,
                    "original_cache_ttl_seconds": 3600,
                },
            )
        )
        await db.flush()
        db.add(
            DatasetSource(
                id=world.source_id,
                dataset_id=world.dataset_id,
                location_id=world.location_id,
                name="Cache test",
                config={},
                cache_policy="on_demand",
            )
        )
        db.add(
            DatasetItem(
                id=world.item_id,
                dataset_id=world.dataset_id,
                file_name="image.png",
                file_path="owned/source.png",
            )
        )
        await db.flush()
        db.add(
            DatasetAsset(
                id=world.asset_id,
                dataset_item_id=world.item_id,
                media_type="image",
                size_bytes=len(data),
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=world.sha,
            )
        )
        await db.commit()
    try:
        yield world
    finally:
        for gate in (world.put_release, world.get_release):
            if gate:
                gate.set()
        async with world.factory() as db:
            await db.execute(
                delete(Entry).where(Entry.scope_location_id == world.location_id)
            )
            await db.execute(
                delete(DatasetSource).where(DatasetSource.id == world.source_id)
            )
            await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == world.location_id)
            )
            await db.execute(delete(User).where(User.id == world.user_id))
            await db.commit()
        assert all(body.closed for body in world.bodies)
        assert all(client.closed for client in world.clients)


async def snapshot(world, data=None):
    data = world.data if data is None else data

    @contextmanager
    def source():
        yield io.BytesIO(data), {"size": len(data)}

    return await capture(source)


async def fill(world):
    content = await snapshot(world)
    try:
        return await cache.store(
            world.factory, world.source_id, world.asset_id, content
        )
    finally:
        content.close()


async def record(world):
    async with world.factory() as db:
        return await db.scalar(
            select(Entry)
            .where(Entry.asset_id == world.asset_id)
            .order_by(Entry.created_at.desc())
        )


async def alter(world, model, identity, **changes):
    async with world.factory() as db:
        row = await db.get(model, identity)
        for key, value in changes.items():
            setattr(row, key, value)
        await db.commit()


async def test_independent_reservation_precedes_put_and_actual_bytes_precede_publication(
    cache_world,
):
    world = cache_world
    content = await snapshot(world)
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        cache.store(world.factory, world.source_id, world.asset_id, content)
    )
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        entry = await record(world)
        assert entry.state == "uploading" and entry.uncertain_write
        async with world.factory() as db:
            assert (await cache.usage(db, world.location_id))["uploading"][
                "bytes"
            ] == len(world.data)
            assert (
                await db.get(
                    DatasetAssetLocation,
                    (world.asset_id, entry.location_id, world.bucket),
                )
                is None
            )
        world.put_release.set()
        assert await pending == "stored"
        assert not content._closed and content.file.tell() == 0
        entry = await record(world)
        assert (
            entry.state == "ready" and not entry.uncertain_write and entry.verified_at
        )
        async with world.factory() as db:
            locator = await db.get(
                DatasetAssetLocation, (world.asset_id, entry.location_id, world.bucket)
            )
            assert (
                locator.role == "original_cache"
                and locator.object_key == cache.object_key(entry)
            )
        read = await cache.read(world.factory, world.source_id, world.asset_id)
        try:
            assert read.sha256 == world.sha and read.file.read() == world.data
        finally:
            read.close()
        assert len(world.puts) == 1
    finally:
        world.put_release.set()
        await pending
        content.close()


async def test_concurrent_fill_deduplicates_by_asset_across_committed_sessions(
    cache_world,
):
    world = cache_world
    world.put_started, world.put_release = threading.Event(), threading.Event()
    first = asyncio.create_task(fill(world))
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        results = await asyncio.gather(
            *(fill(world) for _ in range(3)), return_exceptions=True
        )
        assert results == ["filling"] * 3
        world.put_release.set()
        assert await first == "stored"
        assert await fill(world) == "exists" and len(world.puts) == 1
    finally:
        world.put_release.set()
        await first


@pytest.mark.parametrize("policy_owner", [DatasetSource, StorageLocation])
@pytest.mark.parametrize("policy", ["none", "preview"])
async def test_disabled_policy_creates_no_reservation_or_object(
    cache_world, policy_owner, policy
):
    world = cache_world
    if policy_owner is DatasetSource:
        await alter(world, DatasetSource, world.source_id, cache_policy=policy)
    else:
        await alter(
            world,
            StorageLocation,
            world.location_id,
            config={"cache_policy": policy, "original_cache_quota_bytes": 99999},
        )
    assert await fill(world) == "disabled"
    assert await cache.read(world.factory, world.source_id, world.asset_id) is None
    assert await record(world) is None and not world.puts


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"original_cache_quota_bytes": 0},
        {"original_cache_quota_bytes": -1},
        {"original_cache_quota_bytes": True},
        {"original_cache_quota_bytes": 1000, "original_cache_ttl_seconds": 0},
    ],
)
async def test_budget_is_explicit_and_malformed_limits_fail_closed(cache_world, config):
    world = cache_world
    await alter(
        world,
        StorageLocation,
        world.location_id,
        config={"cache_policy": "on_demand", **config},
    )
    assert await fill(world) == "disabled" and not world.puts


async def test_quota_full_returns_without_upload_and_delete_failure_keeps_charge(
    cache_world, monkeypatch, caplog
):
    world = cache_world
    await alter(
        world,
        StorageLocation,
        world.location_id,
        config={
            "cache_policy": "on_demand",
            "original_cache_quota_bytes": len(world.data) - 1,
        },
    )
    assert await fill(world) == "quota_too_small" and not world.puts
    await alter(
        world,
        StorageLocation,
        world.location_id,
        config={
            "cache_policy": "on_demand",
            "original_cache_quota_bytes": len(world.data),
        },
    )
    assert await fill(world) == "stored"
    identity = await cache.evict_lru(world.factory, world.location_id)
    world.fail_delete = True
    assert await cache.purge(world.factory, identity) == "deferred"
    async with world.factory() as db:
        assert await cache.charged_bytes(db, world.location_id) == len(world.data)
    assert await fill(world) == "quota_full"
    assert "synthetic private" not in caplog.text
    entry = await record(world)
    assert entry.error_code == "storage_cleanup_failed"
    monkeypatch.setattr(
        cache, "now", lambda: entry.next_check_at + timedelta(seconds=1)
    )
    world.fail_delete = False
    assert (await cache.maintain(world.factory))["removed"] >= 1
    assert await fill(world) == "stored"


@pytest.mark.parametrize("cause", ["put_error", "corrupt_put"])
async def test_unconfirmed_objects_never_publish_and_late_puts_remain_accounted(
    cache_world, monkeypatch, cause
):
    world = cache_world
    setattr(world, "fail_put" if cause == "put_error" else "corrupt_put", True)
    assert await fill(world) == "fill_failed"
    entry = await record(world)
    assert entry.state == "deleting" and entry.uncertain_write == (cause == "put_error")
    assert await cache.read(world.factory, world.source_id, world.asset_id) is None
    if cause == "corrupt_put":
        assert await cache.purge(world.factory, entry.id) == "removed"
        assert not world.objects
        return
    assert await cache.purge(world.factory, entry.id) == "retained"
    world.objects[world.bucket, cache.object_key(entry)] = b"late write"
    monkeypatch.setattr(cache, "now", lambda: entry.retain_until + timedelta(seconds=1))
    assert (await cache.maintain(world.factory))["removed"] >= 1
    assert await record(world) is None
    assert not world.objects


async def test_crashed_writer_is_not_deleted_while_active_and_then_recovered(
    cache_world, monkeypatch
):
    world = cache_world
    client = MemoryStorage(world)
    try:
        entry, status = await cache.reserve(
            world.factory, world.source_id, world.asset_id, client
        )
    finally:
        client.close()
    assert status == "reserved"
    assert await cache.purge(world.factory, entry.id) == "skipped"
    world.objects[world.bucket, cache.object_key(entry)] = world.data
    monkeypatch.setattr(cache, "now", lambda: entry.writer_until + timedelta(seconds=1))
    assert (await cache.maintain(world.factory))["retained"] == 1
    assert not world.objects
    async with world.factory() as db:
        assert await cache.charged_bytes(db, world.location_id) == len(world.data)


async def test_policy_change_during_upload_prevents_publication(cache_world):
    world = cache_world
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(fill(world))
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        await alter(
            world,
            DatasetSource,
            world.source_id,
            cache_policy="none",
            policy_revision=2,
        )
        world.put_release.set()
        assert await pending == "configuration_changed"
        entry = await record(world)
        assert entry.state == "deleting" and not entry.uncertain_write
        assert await cache.purge(world.factory, entry.id) == "removed"
        assert not world.objects
    finally:
        world.put_release.set()
        await pending


async def test_read_cancellation_preserves_lease_until_physical_completion(cache_world):
    world = cache_world
    assert await fill(world) == "stored"
    world.get_started, world.get_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        cache.read(world.factory, world.source_id, world.asset_id)
    )
    try:
        assert await asyncio.to_thread(world.get_started.wait, 3)
        assert await cache.evict_lru(world.factory, world.location_id) is None
        pending.cancel()
        await asyncio.sleep(0.03)
        assert not pending.done()
        async with world.factory() as db:
            lease = await db.scalar(
                select(ReadLease).where(ReadLease.entry_id == (await record(world)).id)
            )
            assert lease
        assert await cache.evict_lru(world.factory, world.location_id) is None
    finally:
        world.get_release.set()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        assert all(body.closed for body in world.bodies)
        assert all(client.closed for client in world.clients)
    async with world.factory() as db:
        assert (
            await db.scalar(
                select(ReadLease).where(ReadLease.entry_id == lease.entry_id)
            )
            is None
        )
    identity = await cache.evict_lru(world.factory, world.location_id)
    assert identity is not None
    assert await cache.purge(world.factory, identity) == "removed"


async def test_policy_change_during_read_discards_captured_content(cache_world):
    world = cache_world
    assert await fill(world) == "stored"
    world.get_started, world.get_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        cache.read(world.factory, world.source_id, world.asset_id)
    )
    try:
        assert await asyncio.to_thread(world.get_started.wait, 3)
        await alter(world, DatasetSource, world.source_id, enabled=False)
        world.get_release.set()
        assert await pending is None
    finally:
        world.get_release.set()
        await pending


async def test_corrupt_cache_is_retired_and_never_returned_as_an_original(cache_world):
    world = cache_world
    assert await fill(world) == "stored"
    entry = await record(world)
    world.objects[world.bucket, cache.object_key(entry)] = b"x" * len(world.data)
    assert await cache.read(world.factory, world.source_id, world.asset_id) is None
    assert (await record(world)).state == "deleting"
    assert await cache.purge(world.factory, entry.id) == "removed"


async def test_expiry_does_not_slide_on_access_and_business_deletion_keeps_recovery(
    cache_world, monkeypatch
):
    world = cache_world
    assert await fill(world) == "stored"
    entry = await record(world)
    monkeypatch.setattr(cache, "now", lambda: entry.expires_at - timedelta(seconds=1))
    content = await cache.read(world.factory, world.source_id, world.asset_id)
    content.close()
    assert (await record(world)).expires_at == entry.expires_at
    async with world.factory() as db:
        await db.execute(delete(Dataset).where(Dataset.id == world.dataset_id))
        await db.execute(
            delete(DatasetSource).where(DatasetSource.id == world.source_id)
        )
        await db.commit()
    assert await record(world) is not None
    monkeypatch.setattr(cache, "now", lambda: entry.expires_at + timedelta(seconds=1))
    assert (await cache.maintain(world.factory))["removed"] == 1 and not world.objects


@pytest.mark.parametrize("change", ["endpoint", "bucket"])
async def test_cleanup_never_follows_a_new_storage_target(cache_world, change):
    world = cache_world
    assert await fill(world) == "stored"
    entry = await record(world)
    assert await cache.retire(world.factory, entry.id)
    setattr(world, change, getattr(world, change) + "-different")
    if change == "bucket":
        assert await cache.purge(world.factory, entry.id) == "removed"
        assert world.deletions == [(entry.bucket, cache.object_key(entry))]
    else:
        assert await cache.purge(world.factory, entry.id) == "deferred"
        assert not world.deletions and world.objects
        assert (await record(world)).error_code == "storage_identity_changed"


async def test_retirement_does_not_delete_managed_or_neighbor_objects(cache_world):
    world = cache_world
    assert await fill(world) == "stored"
    first = await record(world)
    neighbor = (world.bucket, cache.object_key(first) + "-neighbor")
    world.objects[neighbor] = b"managed original or frozen training artifact"
    assert await cache.retire(world.factory, first.id)
    assert await fill(world) == "stored"
    second = await record(world)
    assert first.id != second.id and cache.object_key(first) != cache.object_key(second)
    assert await cache.purge(world.factory, first.id) == "removed"
    async with world.factory() as db:
        locator = await db.get(
            DatasetAssetLocation, (world.asset_id, second.location_id, world.bucket)
        )
        assert locator.object_key == cache.object_key(second)
    assert (
        world.objects[neighbor]
        and world.objects[world.bucket, cache.object_key(second)] == world.data
    )


async def test_cancellation_settles_upload_without_closing_caller_snapshot(cache_world):
    world = cache_world
    content = await snapshot(world)
    world.put_started, world.put_release = threading.Event(), threading.Event()
    pending = asyncio.create_task(
        cache.store(world.factory, world.source_id, world.asset_id, content)
    )
    try:
        assert await asyncio.to_thread(world.put_started.wait, 3)
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done() and not content.file.closed
        world.put_release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not content._closed and content.file.tell() == 0
        entry = await record(world)
        assert entry.state == "deleting" and entry.uncertain_write
    finally:
        world.put_release.set()
        content.close()


async def test_independent_assets_compete_for_one_atomic_location_budget(cache_world):
    world = cache_world
    other_asset, other_item = uuid.uuid4(), uuid.uuid4()
    async with world.factory() as db:
        location = await db.get(StorageLocation, world.location_id)
        location.config = {
            **location.config,
            "original_cache_quota_bytes": len(world.data),
        }
        db.add(
            DatasetItem(
                id=other_item,
                dataset_id=world.dataset_id,
                file_name="other.png",
                file_path="owned/other.png",
            )
        )
        await db.flush()
        db.add(
            DatasetAsset(
                id=other_asset,
                dataset_item_id=other_item,
                media_type="image",
                size_bytes=len(world.data),
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=world.sha,
            )
        )
        await db.commit()
    first, second = await snapshot(world), await snapshot(world)
    try:
        results = await asyncio.gather(
            cache.store(world.factory, world.source_id, world.asset_id, first),
            cache.store(world.factory, world.source_id, other_asset, second),
        )
        assert sorted(results) == ["quota_full", "stored"]
        async with world.factory() as db:
            assert await cache.charged_bytes(db, world.location_id) == len(world.data)
        assert len(world.puts) == 1
    finally:
        first.close()
        second.close()


async def test_owned_cache_never_replaces_or_deletes_managed_original_locator(
    cache_world,
):
    world = cache_world
    async with world.factory() as db:
        binding = await ensure_legacy_bindings(db)
        original_location = binding.location_id
        db.add(
            DatasetAssetLocation(
                asset_id=world.asset_id,
                location_id=original_location,
                bucket=world.bucket,
                object_key="managed/keep.png",
                role="managed_original",
                reference_state="present",
            )
        )
        await db.commit()
    world.objects[world.bucket, "managed/keep.png"] = world.data
    assert await fill(world) == "configuration_changed"
    entry = await record(world)
    assert await cache.purge(world.factory, entry.id) == "removed"
    async with world.factory() as db:
        locator = await db.get(
            DatasetAssetLocation, (world.asset_id, original_location, world.bucket)
        )
        assert (
            locator.role == "managed_original"
            and locator.object_key == "managed/keep.png"
        )
    assert world.objects == {(world.bucket, "managed/keep.png"): world.data}


async def test_expired_read_lease_from_crashed_reader_is_recoverable(
    cache_world, monkeypatch
):
    world = cache_world
    assert await fill(world) == "stored"
    entry = await record(world)
    async with world.factory() as db:
        db.add(
            ReadLease(
                id=uuid.uuid4(),
                entry_id=entry.id,
                expires_at=cache.now() + timedelta(seconds=10),
            )
        )
        await db.commit()
    assert not await cache.retire(world.factory, entry.id)
    monkeypatch.setattr(cache, "now", lambda: entry.expires_at + timedelta(seconds=1))
    assert (await cache.maintain(world.factory))["removed"] == 1
    assert await record(world) is None


async def test_four_distinct_snapshots_fill_without_requesting_extra_snapshot_admission(
    cache_world,
):
    world = cache_world
    assets = [world.asset_id]
    async with world.factory() as db:
        location = await db.get(StorageLocation, world.location_id)
        location.config = {
            **location.config,
            "original_cache_quota_bytes": len(world.data) * 4,
        }
        for _ in range(3):
            item_id, asset_id = uuid.uuid4(), uuid.uuid4()
            db.add(
                DatasetItem(
                    id=item_id,
                    dataset_id=world.dataset_id,
                    file_name="other.png",
                    file_path=f"owned/{item_id}.png",
                )
            )
            await db.flush()
            db.add(
                DatasetAsset(
                    id=asset_id,
                    dataset_item_id=item_id,
                    media_type="image",
                    size_bytes=len(world.data),
                    verification_state="verified",
                    checksum_algorithm="sha256",
                    checksum_value=world.sha,
                )
            )
            assets.append(asset_id)
        await db.commit()
    contents = []
    try:
        for _ in range(4):
            contents.append(await snapshot(world))
        # Hold all snapshot slots; optional NOWAIT publication is independent.
        results = [
            await cache.store(world.factory, world.source_id, identity, content)
            for identity, content in zip(assets, contents)
        ]
        assert results == ["stored"] * 4
        assert len(world.puts) == 4
    finally:
        for content in contents:
            content.close()


@pytest.mark.parametrize("during_read", [False, True])
async def test_saved_target_is_independent_of_current_binding_before_and_after_read(
    cache_world, during_read
):
    from app.db.models.dataset_asset import StorageBinding

    world = cache_world
    assert await fill(world) == "stored"
    if during_read:
        world.get_started, world.get_release = threading.Event(), threading.Event()
        pending = asyncio.create_task(
            cache.read(world.factory, world.source_id, world.asset_id)
        )
        assert await asyncio.to_thread(world.get_started.wait, 3)
    try:
        await alter(world, StorageBinding, "media-cache", generation=2)
        if during_read:
            world.get_release.set()
            content = await pending
        else:
            content = await cache.read(world.factory, world.source_id, world.asset_id)
        assert content is not None
        content.close()
    finally:
        if world.get_release:
            world.get_release.set()
            await pending
        await alter(world, StorageBinding, "media-cache", generation=1)


async def test_publication_skips_busy_business_locks_without_waiting_for_its_caller(
    cache_world,
):
    world = cache_world
    async with world.factory() as caller:
        await caller.get(DatasetSource, world.source_id, with_for_update=True)
        assert await asyncio.wait_for(fill(world), timeout=3) == "fill_failed"
        entry = await record(world)
        assert entry.state == "deleting" and not entry.uncertain_write
    assert await cache.purge(world.factory, entry.id) == "removed"
