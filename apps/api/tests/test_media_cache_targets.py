"""Real short allocation transactions and nullable immutable owner migration."""

from datetime import timedelta
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.thumbnail_output import ThumbnailOutput
from app.db.models.user import User
from app.services import media_cache_storage as storage
from app.services import storage_usage_categories as categories
from app.services.original_cache import now
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.storage_location import register_connection, location_is_referenced
from tests.test_managed_original_creation import migration


@pytest.fixture
async def media_db(managed_database):
    async with managed_database() as engine:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            owner = User(
                id=uuid.uuid4(),
                name="Media cache fixture owner",
                email=f"media-cache-{uuid.uuid4().hex}@example.test",
                password_hash="unused",
                role="project_admin",
                is_active=True,
            )
            db.add(owner)
            await db.flush()
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id=f"mc-{uuid.uuid4().hex[:12]}",
                name="Media cache fixture dataset",
                created_by=owner.id,
            )
            db.add(dataset)
            await ensure_legacy_bindings(db)
            await db.commit()
            db.info["owner_id"] = owner.id
            db.info["dataset_id"] = dataset.id
            yield db


async def switch(db, monkeypatch):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    owner_id = db.info.get("owner_id")
    if owner_id is None:
        owner_id = await db.scalar(select(User.id).limit(1))
        if owner_id is None:
            owner = User(
                id=uuid.uuid4(),
                name="Media cache switch owner",
                email=f"media-cache-switch-{uuid.uuid4().hex}@example.test",
                password_hash="unused",
                role="project_admin",
                is_active=True,
            )
            db.add(owner)
            await db.flush()
            owner_id = owner.id
        db.info["owner_id"] = owner_id
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
        created_by=owner_id,
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
        dataset_id=db.info["dataset_id"],
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
