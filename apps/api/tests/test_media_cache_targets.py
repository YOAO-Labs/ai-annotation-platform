"""Real short allocation transactions and nullable immutable owner migration."""

from datetime import timedelta
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import media_cache_storage as storage
from app.services import storage_usage_categories as categories
from app.services.original_cache import now
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.storage_location import register_connection, location_is_referenced
from tests.test_managed_original_creation import migration


@pytest.fixture
async def media_db(test_engine):
    schema = "media_targets_" + uuid.uuid4().hex
    tables = (
        "storage_connections",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "original_cache_entries",
        "original_cache_read_leases",
        "dataset_items",
        "tasks",
        "dataset_source_previews",
        "image_pyramid_outputs",
        "video_derivative_outputs",
    )
    async with test_engine.begin() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
        for table in ("dataset_items", "tasks", "dataset_source_previews"):
            await connection.execute(
                text(f'ALTER TABLE "{schema}".{table} DROP COLUMN thumbnail_output_id')
            )
        await connection.execute(
            text(
                f'ALTER TABLE "{schema}".original_cache_entries DROP COLUMN storage_ref'
            )
        )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: migration(sync, "0252_media_cache_targets.py").upgrade()
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def switch(db, monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="cache B",
        kind="s3",
        scope="global",
        config={
            "endpoint": "https://objects.example.test",
            "bucket": "saved-b",
            "base_prefix": "公司",
            "use_ssl": True,
        },
        secret_enc=encrypt_secret({"access_key": "private", "secret_key": "private"}),
        created_by=uuid.uuid4(),
    )
    db.add(connection)
    await db.flush()
    location = await register_connection(db, connection)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "media-cache")
        .values(location_id=location.id, bucket="saved-b", generation=2)
    )
    await db.commit()
    return connection, location


async def test_b_ref_owner_insert_and_caller_commit_are_one_short_transaction(
    media_db, monkeypatch
):
    db = media_db
    connection, location = await switch(db, monkeypatch)
    identifier = uuid.uuid4()
    ref = await storage.allocate(db, f"thumbnails/objects/{identifier}/")
    db.add(
        ThumbnailOutput(
            id=identifier,
            owner_kind="dataset_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={"sha256": "a" * 64},
            storage_ref=ref,
            writer_until=now() + timedelta(seconds=120),
            next_check_at=now(),
        )
    )
    await db.flush()
    async with async_sessionmaker(db.bind)() as other:
        assert await other.get(ThumbnailOutput, identifier) is None
    await db.commit()
    await db.execute(
        update(StorageConnection)
        .where(StorageConnection.id == connection.id)
        .values(
            revision=2,
            secret_enc=encrypt_secret(
                {"access_key": "rotated", "secret_key": "rotated"}
            ),
        )
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
            bucket=storage.storage_service.media_cache_bucket,
            generation=3,
        )
    )
    await db.commit()
    target = await storage.target_for_ref(db, ref)
    assert target.config_revision == 2 and target.connection_revision == 2
    assert target.bucket == "saved-b" and ref["config_revision"] == 1
    assert await location_is_referenced(db, location.id)
    assert (await db.get(ThumbnailOutput, identifier)).storage_ref == ref
    key = ref["prefix"] + "thumbnail.webp"
    monkeypatch.setattr(categories, "endpoint", lambda: "https://objects.example.test")
    for state in ("building", "ready", "failed", "retiring", "deleted"):
        await db.execute(
            update(ThumbnailOutput)
            .where(ThumbnailOutput.id == identifier)
            .values(
                state=state,
                **(
                    {
                        "sha256": "a" * 64,
                        "size_bytes": 5,
                        "etag": '"e"',
                        "ready_at": now(),
                    }
                    if state == "ready"
                    else {}
                ),
            )
        )
        await db.commit()
        assert await location_is_referenced(db, location.id)
        result = await categories.classify(
            db, "saved-b", [{"key": key, "size": 5}, {"key": key + ".other", "size": 7}]
        )
        assert result["categories"]["preview_cache"]["size_bytes"] == 5
        assert result["categories"]["unclassified"]["size_bytes"] == 7


async def test_busy_allocation_recovers_savepoint_and_keeps_pending_business(
    media_db, monkeypatch
):
    db = media_db
    _, location = await switch(db, monkeypatch)
    pending = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        file_name="pending",
        file_path="pending",
        file_type="image",
    )
    db.add(pending)
    await db.flush()
    async with async_sessionmaker(db.bind)() as other:
        await other.execute(
            select(StorageLocation.id)
            .where(StorageLocation.id == location.id)
            .with_for_update()
        )
        with pytest.raises(storage.MediaCacheStorageError, match="媒体缓存") as caught:
            await storage.allocate(db, f"thumbnails/objects/{uuid.uuid4()}/")
        assert caught.value.code == "media_cache_storage_busy"
        assert (
            await db.scalar(select(DatasetItem.id).where(DatasetItem.id == pending.id))
            == pending.id
        )
    await db.rollback()


async def test_thumbnail_immutable_owner_receipt_and_downgrade_refusal(media_db):
    db = media_db
    identifier = uuid.uuid4()
    ref = await storage.allocate(db, f"thumbnails/objects/{identifier}/")
    db.add(
        ThumbnailOutput(
            id=identifier,
            owner_kind="directory_preview",
            owner_id=uuid.uuid4(),
            path_digest="b" * 64,
            input_ref={"sha256": "a" * 64},
            storage_ref=ref,
            writer_until=now() + timedelta(seconds=120),
            next_check_at=now(),
        )
    )
    await db.commit()
    for values in (
        {"storage_ref": {**ref, "binding_generation": 2}},
        {"owner_id": uuid.uuid4()},
        {"writer_until": now() + timedelta(seconds=600)},
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(ThumbnailOutput)
                    .where(ThumbnailOutput.id == identifier)
                    .values(**values)
                )
    await db.execute(
        update(ThumbnailOutput)
        .where(ThumbnailOutput.id == identifier)
        .values(
            state="ready", sha256="a" * 64, size_bytes=5, etag='"etag"', ready_at=now()
        )
    )
    await db.commit()
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(ThumbnailOutput)
                .where(ThumbnailOutput.id == identifier)
                .values(size_bytes=6)
            )
    with pytest.raises(IntegrityError, match="cannot be deleted"):
        async with db.begin_nested():
            await db.execute(
                delete(ThumbnailOutput).where(ThumbnailOutput.id == identifier)
            )
    with pytest.raises(Exception, match="Saved media-cache responsibility"):
        async with db.begin_nested():
            await (await db.connection()).run_sync(
                lambda sync: migration(sync, "0252_media_cache_targets.py").downgrade()
            )


async def test_empty_nullable_migration_roundtrip_preserves_schema_one(media_db):
    db = media_db
    await (await db.connection()).run_sync(
        lambda sync: migration(sync, "0252_media_cache_targets.py").downgrade()
    )
    await (await db.connection()).run_sync(
        lambda sync: migration(sync, "0252_media_cache_targets.py").upgrade()
    )
    assert await db.scalar(text("SELECT to_regclass('thumbnail_outputs')"))
    assert (await db.get(StorageBinding, "media-cache")).generation == 1
