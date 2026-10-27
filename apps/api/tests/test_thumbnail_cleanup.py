"""Saved-target cleanup keeps active, referenced and uncertain owners protected."""

from datetime import timedelta
from types import SimpleNamespace
import uuid

from botocore.config import Config
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services import (
    storage_access,
    thumbnail_cleanup as cleanup,
    thumbnail_outputs as outputs,
)
from tests.test_media_cache_targets import media_db as media_db, switch
from tests.test_thumbnail_output_creation import client as client


async def create(db):
    return await outputs.create(
        db,
        owner_kind="dataset_thumbnail",
        owner_id=uuid.uuid4(),
        input_ref={"source": {}},
        body=b"webp",
    )


async def retire(db, output, monkeypatch):
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    clock = outputs.now() + timedelta(seconds=7200)
    monkeypatch.setattr(outputs, "now", lambda: clock)
    assert await cleanup.reconcile(db) == 0
    async with factory() as current:
        value = await current.get(ThumbnailOutput, output.id)
        assert value.state == "retiring" and value.retired_at == clock
    clock += timedelta(seconds=3601)
    return clock


@pytest.fixture(autouse=True)
def deletion(client):
    client.meta = SimpleNamespace(config=Config(retries={"max_attempts": 3}))

    def delete(**params):
        client.record("delete", params)
        if params.get("VersionId"):
            value = client.versions.pop(
                (params["Bucket"], params["Key"], params["VersionId"]), None
            )
            current = client.objects.get((params["Bucket"], params["Key"]))
            if value is current:
                client.objects.pop((params["Bucket"], params["Key"]), None)
        else:
            client.objects.pop((params["Bucket"], params["Key"]), None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    client.delete_object = delete


async def test_saved_b_cleanup_after_return_to_a_and_location_disable(
    media_db, client, monkeypatch
):
    _, location = await switch(media_db, monkeypatch)
    output = await create(media_db)
    saved = outputs.owner(output)
    # A separate unrelated object is outside this owner's one-file prefix.
    client.store(saved.bucket, "other/keep", b"preserve")
    clock = await retire(media_db, output, monkeypatch)
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as current:
        binding = await current.get(StorageBinding, "media-cache")
        binding.location_id = LEGACY_LOCATION_ID
        binding.bucket = outputs.storage_service.media_cache_bucket
        binding.generation += 1
        target = await current.get(StorageLocation, location.id)
        target.enabled = False
        await current.commit()
    assert await cleanup.reconcile(media_db) == 1
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as current:
        assert (await current.get(ThumbnailOutput, output.id)).state == "deleted"
    assert (saved.bucket, saved.key("thumbnail.webp")) not in client.objects
    assert client.objects[saved.bucket, "other/keep"]["data"] == b"preserve"
    deletes = [params for op, params in client.calls if op == "delete"]
    assert deletes == [
        {
            "Bucket": saved.bucket,
            "Key": saved.key("thumbnail.webp"),
            "VersionId": output.version_id,
            "IfMatch": output.etag,
        }
    ]
    assert clock > output.writer_until


@pytest.mark.parametrize("read_location", ["source", "published"])
async def test_read_operation_blocks_retiring_collection(
    media_db, client, monkeypatch, read_location
):
    output = await create(media_db)
    await retire(media_db, output, monkeypatch)
    saved = outputs.owner(output)
    location_id = saved.location_id if read_location == "source" else uuid.uuid4()
    async with await storage_access.acquire(
        media_db, location_id, outputs.purpose(output.id)
    ):
        assert await cleanup.reconcile(media_db) == 0
        assert (saved.bucket, saved.key("thumbnail.webp")) in client.objects
    assert await cleanup.reconcile(media_db) == 1


async def test_business_pointer_prevents_retirement(media_db, client, monkeypatch):
    output = await create(media_db)
    media_db.add(
        DatasetItem(
            id=output.owner_id,
            dataset_id=uuid.uuid4(),
            file_name="preview.png",
            file_path="original",
            file_type="image",
            file_size=4,
            thumbnail_output_id=output.id,
        )
    )
    await media_db.commit()
    clock = outputs.now() + timedelta(seconds=7200)
    monkeypatch.setattr(outputs, "now", lambda: clock)
    assert await cleanup.reconcile(media_db) == 0
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as current:
        assert (await current.get(ThumbnailOutput, output.id)).state == "ready"


async def test_uncertain_owner_is_never_blindly_deleted(media_db, client, monkeypatch):
    output = await create(media_db)
    factory = async_sessionmaker(media_db.bind, expire_on_commit=False)
    async with factory() as current:
        row = await current.get(ThumbnailOutput, output.id)
        row.uncertain_write = True
        row.state = "failed"
        await current.commit()
    clock = outputs.now() + timedelta(seconds=7200)
    monkeypatch.setattr(outputs, "now", lambda: clock)
    assert await cleanup.reconcile(media_db) == 0
    assert not [params for op, params in client.calls if op == "delete"]
    async with factory() as current:
        assert (await current.scalar(select(ThumbnailOutput))).state == "failed"


async def test_refused_write_leaves_tombstone_without_deleting_collision(
    media_db, client, monkeypatch
):
    from tests.test_managed_original_writer import error

    def refused(operation, params):
        if operation == "put":
            raise error("PreconditionFailed", 412)

    client.hook = refused
    with pytest.raises(Exception):
        await create(media_db)
    client.hook = None
    clock = outputs.now() + timedelta(seconds=7200)
    monkeypatch.setattr(outputs, "now", lambda: clock)
    assert await cleanup.reconcile(media_db) == 0
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as current:
        output = await current.scalar(select(ThumbnailOutput))
        assert output.state == "deleted" and output.sha256 is None
    assert not [params for op, params in client.calls if op == "delete"]


async def test_cleanup_permission_denial_keeps_receipt_but_releases_operation(
    media_db, client, monkeypatch
):
    from tests.test_managed_original_writer import error
    from app.db.models.storage_maintenance import StorageOperation

    output = await create(media_db)
    await retire(media_db, output, monkeypatch)

    def denied(**params):
        raise error("AccessDenied", 403)

    client.delete_object = denied
    assert await cleanup.reconcile(media_db) == 0
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as current:
        stored = await current.get(ThumbnailOutput, output.id)
        assert stored.state == "retiring" and stored.sha256 == output.sha256
        assert not await current.scalar(select(StorageOperation.id))
