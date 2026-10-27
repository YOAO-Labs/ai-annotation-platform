"""Saved A/B cache addresses outlive binding changes and credential rotation."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, select, update

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset_asset import (
    StorageBinding,
    StorageLocation,
    DatasetAssetLocation,
)
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease as ReadLease,
)
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate, StorageOperation
from app.services import original_cache as cache
from app.services import storage_usage_categories as categories
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.source_readers.s3 import S3Access
from app.services.storage_location import register_connection
from tests.test_original_cache import (
    cache_world as cache_world,
    MemoryStorage,
    fill,
    record,
)


@pytest.fixture
async def cache_target(cache_world, monkeypatch):
    world = cache_world
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    async with world.factory() as db:
        before = await db.get(StorageBinding, "media-cache")
        original = before.location_id, before.bucket, before.generation
        connection = StorageConnection(
            id=uuid.uuid4(),
            name="cache-target-" + uuid.uuid4().hex,
            kind="s3",
            scope="global",
            config={
                "endpoint": "https://objects.example.test",
                "bucket": "cache-b",
                "base_prefix": "公司/root",
                "use_ssl": True,
            },
            secret_enc=encrypt_secret({"access_key": "first", "secret_key": "first"}),
            created_by=world.user_id,
        )
        db.add(connection)
        await db.flush()
        location = await register_connection(db, connection)
        await db.execute(
            update(StorageBinding)
            .where(StorageBinding.purpose == "media-cache")
            .values(location_id=location.id, bucket="cache-b", generation=2)
        )
        await db.commit()
    observed = []

    @contextmanager
    def transport(self, *, read_timeout=None):
        assert read_timeout == 15
        observed.append((self.endpoint, self.config["bucket"], self.secret_enc))
        client = MemoryStorage(world)
        try:
            yield client
        finally:
            client.close()

    monkeypatch.setattr(S3Access, "client", transport)
    try:
        yield world, connection, location, observed
    finally:
        async with world.factory() as db:
            await db.execute(
                update(StorageBinding)
                .where(StorageBinding.purpose == "media-cache")
                .values(
                    location_id=original[0], bucket=original[1], generation=original[2]
                )
            )
            await db.execute(
                delete(DatasetAssetLocation).where(
                    DatasetAssetLocation.location_id == location.id,
                    DatasetAssetLocation.role == "original_cache",
                )
            )
            await db.execute(delete(Entry).where(Entry.location_id == location.id))
            assert not await db.scalar(
                select(StorageOperation.id).where(
                    StorageOperation.location_id == location.id
                )
            )
            await db.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location.id
                )
            )
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location.id)
            )
            await db.execute(
                delete(StorageConnection).where(StorageConnection.id == connection.id)
            )
            await db.commit()


async def test_inflight_b_cache_seals_reads_and_cleans_saved_target_after_switch_back_and_rotation(
    cache_target, monkeypatch
):
    world, connection, location, observed = cache_target
    reserve = cache.reserve
    secret = encrypt_secret({"access_key": "rotated", "secret_key": "rotated"})
    saved = []

    async def rotate(*args, **kwargs):
        entry, status = await reserve(*args, **kwargs)
        if entry is not None:
            saved.append(deepcopy(entry.storage_ref))
            async with world.factory() as db:
                await db.execute(
                    update(StorageConnection)
                    .where(StorageConnection.id == connection.id)
                    .values(revision=2, secret_enc=secret)
                )
                await db.execute(
                    update(StorageLocation)
                    .where(StorageLocation.id == location.id)
                    .values(config_revision=2)
                )
                await db.execute(
                    update(StorageBinding)
                    .where(StorageBinding.purpose == "media-cache")
                    .values(
                        location_id=LEGACY_LOCATION_ID,
                        bucket=world.bucket,
                        generation=3,
                    )
                )
                await db.commit()
        return entry, status

    monkeypatch.setattr(cache, "reserve", rotate)
    assert await fill(world) == "stored"
    entry = await record(world)
    assert entry.storage_ref == saved[0]
    assert entry.location_id == location.id and entry.location_revision == 1
    key = cache.object_key(entry)
    assert key.startswith(
        "公司/root/_aap_managed/media-cache/original-cache/v1/"
    ) and key.endswith("/payload")
    assert world.objects["cache-b", key] == world.data
    assert observed and all(
        endpoint == "https://objects.example.test"
        and bucket == "cache-b"
        and actual == secret
        for endpoint, bucket, actual in observed
    )
    async with world.factory() as db:
        listed = [{"key": key, "size": len(world.data)}]
        assert (await categories.classify(db, "cache-b", listed))["categories"][
            "unclassified"
        ]["object_count"] == 1
        with monkeypatch.context() as context:
            context.setattr(
                categories, "endpoint", lambda: "https://objects.example.test"
            )
            assert (await categories.classify(db, "cache-b", listed))["categories"][
                "preview_cache"
            ]["size_bytes"] == len(world.data)
    result = await cache.read(world.factory, world.source_id, world.asset_id)
    assert result.file.read() == world.data
    result.close()
    assert (await record(world)).expires_at == entry.expires_at
    assert await cache.retire(world.factory, entry.id)
    assert await cache.purge(world.factory, entry.id) == "removed"
    assert world.deletions == [("cache-b", key)]
    assert not world.objects


async def test_legacy_null_cache_keeps_exact_a_key_while_binding_is_b(cache_target):
    world, _, _, observed = cache_target
    checked = cache.now()
    entry = Entry(
        id=uuid.uuid4(),
        scope_location_id=world.location_id,
        source_id=world.source_id,
        asset_id=world.asset_id,
        sha256=world.sha,
        size_bytes=len(world.data),
        source_revision=1,
        policy_revision=1,
        location_revision=1,
        location_id=LEGACY_LOCATION_ID,
        bucket=world.bucket,
        binding_generation=1,
        storage_identity=managed_storage_identity(world.endpoint, world.bucket),
        storage_ref=None,
        state="ready",
        uncertain_write=False,
        verified_at=checked,
        last_accessed_at=checked,
        expires_at=checked + timedelta(hours=1),
        writer_until=checked + timedelta(seconds=120),
        retain_until=checked + timedelta(days=7),
        next_check_at=checked,
    )
    key = cache.object_key(entry)
    assert key == f"original-cache/v1/{world.asset_id}/{world.sha}/{entry.id}"
    async with world.factory() as db:
        db.add(entry)
        db.add(
            DatasetAssetLocation(
                asset_id=world.asset_id,
                location_id=LEGACY_LOCATION_ID,
                bucket=world.bucket,
                object_key=key,
                role="original_cache",
                reference_state="present",
                verified_at=checked,
            )
        )
        await db.commit()
    world.objects[world.bucket, key] = world.data
    result = await cache.read(world.factory, world.source_id, world.asset_id)
    assert result.file.read() == world.data
    result.close()
    assert not observed
    assert await cache.retire(world.factory, entry.id)
    assert await cache.purge(world.factory, entry.id) == "removed"
    assert world.deletions == [(world.bucket, key)]


@pytest.mark.parametrize("blocked", ["source", "target"])
async def test_source_and_saved_target_gates_independently_block_cache_read(
    cache_target, blocked
):
    world, _, location, observed = cache_target
    assert await fill(world) == "stored"
    before = len(observed)
    async with world.factory() as db:
        await db.execute(
            update(StorageMaintenanceGate)
            .where(
                StorageMaintenanceGate.location_id
                == (world.location_id if blocked == "source" else location.id)
            )
            .values(state="stopped")
        )
        await db.commit()
    with pytest.raises(ContentError) as caught:
        await cache.read(world.factory, world.source_id, world.asset_id)
    assert caught.value.code == "storage_maintenance"
    assert len(observed) == before
    assert (await record(world)).state == "ready"


async def test_target_admission_failure_before_io_releases_temporary_read_lease(
    cache_target, monkeypatch
):
    world, _, _, observed = cache_target
    assert await fill(world) == "stored"
    entry = await record(world)
    before = len(observed)
    acquire = cache.storage_access.acquire

    async def reject(db, location_id, purpose, **kwargs):
        if purpose == cache.read_purpose(entry.id):
            raise ContentError("storage_maintenance_changed", 409)
        return await acquire(db, location_id, purpose, **kwargs)

    monkeypatch.setattr(cache.storage_access, "acquire", reject)
    with pytest.raises(ContentError) as caught:
        await cache.read(world.factory, world.source_id, world.asset_id)
    assert caught.value.code == "storage_maintenance_changed"
    assert len(observed) == before
    entry = await record(world)
    assert entry.state == "ready"
    async with world.factory() as db:
        assert not await db.scalar(
            select(ReadLease.id).where(ReadLease.entry_id == entry.id)
        )
