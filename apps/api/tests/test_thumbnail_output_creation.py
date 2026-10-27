"""Committed thumbnail ownership is independent of business publication."""

import asyncio
from contextlib import contextmanager
import hashlib
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.thumbnail_output import ThumbnailOutput
from app.services import thumbnail_outputs as service
from app.services import storage as storage_module
from app.services import media_cache_accounting, storage_usage_accounting
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.source_readers.s3 import S3Access
from tests.test_managed_original_writer import MemoryS3, error, response
from tests.test_media_cache_targets import media_db as media_db, switch


class Client(MemoryS3):
    def put_object(self, **params):
        self.record("put", params)
        assert params["IfNoneMatch"] == "*"
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        value = self.store(
            params["Bucket"],
            params["Key"],
            params["Body"],
            content_type=params["ContentType"],
        )
        return response(ETag=value["ETag"], VersionId=value["VersionId"])


@pytest.fixture
def client(monkeypatch):
    client = Client()
    client.versioned = True
    monkeypatch.setattr(service.storage_service, "client", client)

    class OwnedStorage(storage_module.StorageService):
        def __init__(self, **kwargs):
            self.client = client

    monkeypatch.setattr(storage_module, "StorageService", OwnedStorage)
    client.close = lambda: None

    @contextmanager
    def transport(self, **kwargs):
        yield client

    monkeypatch.setattr(S3Access, "client", transport)
    return client


async def test_building_owner_commits_before_put_without_committing_business(
    media_db, client
):
    db = media_db
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID)
    old_name = location.name
    location.name = "uncommitted business mutation"
    loop = asyncio.get_running_loop()
    observed = []

    async def prove():
        async with factory() as fresh:
            row = await fresh.scalar(select(ThumbnailOutput))
            observed.append(
                (row.id, row.state, row.sha256, row.size_bytes, row.ready_at)
            )
            assert (
                await fresh.get(StorageLocation, LEGACY_LOCATION_ID)
            ).name == old_name

    def hook(operation, params):
        if operation == "put":
            asyncio.run_coroutine_threadsafe(prove(), loop).result(timeout=5)

    client.hook = hook
    output = await service.create(
        db,
        owner_kind="dataset_thumbnail",
        owner_id=uuid.uuid4(),
        input_ref={"source": {"sha256": "a" * 64}},
        body=b"webp",
    )
    assert observed == [(output.id, "building", None, None, None)]
    assert output.state == "ready" and not output.uncertain_write
    assert output.sha256 == hashlib.sha256(b"webp").hexdigest()
    assert output.input_ref["encoded"] == {"sha256": output.sha256, "size_bytes": 4}
    await db.rollback()
    async with factory() as fresh:
        row = await fresh.get(ThumbnailOutput, output.id)
        assert row is not None and row.state == "ready"
        assert not await fresh.scalar(select(StorageOperation.id))


@pytest.mark.parametrize(
    "code,uncertain", [("AccessDenied", False), ("InternalError", True)]
)
async def test_failed_put_keeps_durable_precise_responsibility(
    media_db, client, code, uncertain
):
    def fail(operation, params):
        if operation == "put":
            raise error(code, 403 if code == "AccessDenied" else 500)

    client.hook = fail
    with pytest.raises(Exception):
        await service.create(
            media_db,
            owner_kind="task_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={"source": {}},
            body=b"webp",
        )
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as fresh:
        output = await fresh.scalar(select(ThumbnailOutput))
        assert output.state == "failed" and output.uncertain_write is uncertain
        assert output.sha256 is None and output.ready_at is None
        assert bool(await fresh.scalar(select(StorageOperation.id))) is uncertain


async def test_b_allocation_keeps_saved_address_after_return_to_a(
    media_db, client, monkeypatch
):
    db = media_db
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    _, location = await switch(db, monkeypatch)
    loop = asyncio.get_running_loop()

    async def return_to_a():
        async with factory() as current:
            binding = await current.get(StorageBinding, "media-cache")
            binding.location_id, binding.bucket = (
                LEGACY_LOCATION_ID,
                service.storage_service.media_cache_bucket,
            )
            binding.generation += 1
            await current.commit()

    def hook(operation, params):
        if operation == "put":
            asyncio.run_coroutine_threadsafe(return_to_a(), loop).result(timeout=5)

    client.hook = hook
    output = await service.create(
        db,
        owner_kind="dataset_thumbnail",
        owner_id=uuid.uuid4(),
        input_ref={"source": {}},
        body=b"webp",
    )
    assert output.storage_ref["location_id"] == str(location.id)
    assert output.storage_ref["bucket"] == "saved-b"
    assert output.storage_ref["prefix"].startswith("公司/_aap_managed/media-cache/")
    assert ("saved-b", service.owner(output).key("thumbnail.webp")) in client.objects
    async with factory() as current:
        assert (
            await current.get(StorageBinding, "media-cache")
        ).location_id == LEGACY_LOCATION_ID
        target = await service.media_cache_storage.target_for_ref(
            current, output.storage_ref
        )
        service._verified_bytes(target, output)


async def test_invalid_body_does_not_allocate_owner(media_db):
    with pytest.raises(ContentError):
        await service.create(
            media_db,
            owner_kind="dataset_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={},
            body=b"",
        )
    assert not await media_db.scalar(select(ThumbnailOutput.id))


async def test_read_owner_contention_is_recoverable_before_provider_io(
    media_db, client
):
    output = await service.create(
        media_db,
        owner_kind="dataset_thumbnail",
        owner_id=uuid.uuid4(),
        input_ref={"source": {}},
        body=b"webp",
    )
    before = list(client.calls)
    factory = async_sessionmaker(media_db.bind, expire_on_commit=False)
    async with factory() as locked:
        await locked.get(ThumbnailOutput, output.id, with_for_update=True)
        with pytest.raises(ContentError, match="storage_original_busy"):
            await service.read_bytes(media_db, output.id)
        assert client.calls == before
    async with factory() as current:
        assert not await current.scalar(select(StorageOperation.id))


async def test_accounting_intent_failure_is_known_pre_put_refusal(
    media_db, client, monkeypatch
):
    async def fail(*args, **kwargs):
        raise OSError("intent unavailable")

    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", fail)
    with pytest.raises(OSError):
        await service.create(
            media_db,
            owner_kind="dataset_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={},
            body=b"webp",
        )
    assert not any(operation == "put" for operation, _ in client.calls)
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as fresh:
        output = await fresh.scalar(select(ThumbnailOutput))
        assert output.state == "failed" and not output.uncertain_write
        assert not await fresh.scalar(select(StorageOperation.id))


async def test_cancel_during_accounting_still_persists_verified_owner(
    media_db, client, monkeypatch
):
    started, release = asyncio.Event(), asyncio.Event()
    publish = storage_usage_accounting.publish

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        await publish(*args, **kwargs)

    monkeypatch.setattr(storage_usage_accounting, "publish", blocked)
    task = asyncio.create_task(
        service.create(
            media_db,
            owner_kind="dataset_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={},
            body=b"webp",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with async_sessionmaker(media_db.bind, expire_on_commit=False)() as fresh:
        output = await fresh.scalar(select(ThumbnailOutput))
        assert output.state == "ready" and not output.uncertain_write
        assert output.size_bytes == len(b"webp") and output.ready_at is not None
        assert not await fresh.scalar(select(StorageOperation.id))
