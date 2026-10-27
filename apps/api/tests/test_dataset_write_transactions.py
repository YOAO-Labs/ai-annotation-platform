"""Physical work precedes the existing request-level metadata publication."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.dataset import DatasetItem, Scene
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.services import managed_original_creation as creations
from tests.test_datasets_upload_zip import (
    _FakeDatasetSourceStorage,
    _bearer,
    _create_dataset,
    _make_zip,
    _patch_storage_clients,
)
from tests.test_dataset_assets import local_creation_client as local_creation_client


async def test_zip_seals_all_bytes_before_metadata_and_rolls_back_late_authority(
    httpx_client, db_session, super_admin, monkeypatch
):
    from app.api.v1 import datasets as api

    fake = _FakeDatasetSourceStorage()
    _patch_storage_clients(monkeypatch, fake)
    dataset = await _create_dataset(
        httpx_client,
        super_admin[1],
        "zip-transaction-" + uuid.uuid4().hex[:8],
        "point_cloud",
    )
    identifier = uuid.UUID(dataset["id"])
    original = creations.create_stream
    sealed = []

    async def seal(db, allocation, stream, **kwargs):
        # No Item from this ZIP may be committed or even published while a
        # later entry is still performing provider I/O.
        assert not await db.scalar(
            select(DatasetItem.id).where(DatasetItem.dataset_id == identifier)
        )
        result = await original(db, allocation, stream, **kwargs)
        sealed.append(result.creation_id)
        return result

    monkeypatch.setattr(creations, "create_stream", seal)
    recheck = api.recheck_directory_write

    async def authority(request, db, dataset_id):
        await recheck(request, db, dataset_id)
        if await db.scalar(
            select(DatasetItem.id).where(DatasetItem.dataset_id == identifier)
        ):
            raise HTTPException(403, "late authority changed")

    monkeypatch.setattr(api, "recheck_directory_write", authority)
    result = await httpx_client.post(
        f"/api/v1/datasets/{identifier}/items/upload-zip",
        headers=_bearer(super_admin[1]),
        files={
            "file": (
                "scene.zip",
                _make_zip({"lidar/0.pcd": b"first", "lidar/1.pcd": b"second"}),
                "application/zip",
            )
        },
    )
    assert result.status_code == 403, result.text
    assert len(sealed) == 2
    assert not await db_session.scalar(
        select(DatasetItem.id).where(DatasetItem.dataset_id == identifier)
    )
    assert not await db_session.scalar(
        select(Scene.id).where(Scene.dataset_id == identifier)
    )
    for creation_id in sealed:
        row = await creations._read(db_session, creation_id)
        assert row["status"] == "verified"
        assert (
            await db_session.get(ManagedOriginalCreation, creation_id)
        ).id == creation_id


@pytest.fixture
async def copy_db(test_engine):
    """A disposable namespace permits real commits without deleting owner history."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    schema = "copy_transaction_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        tables = list(
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname=:source"),
                {"source": source},
            )
        )
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def test_same_s3_location_copy_takes_target_before_final_source_locks(
    copy_db, monkeypatch
):
    import asyncio
    from contextlib import contextmanager
    from sqlalchemy import text, update
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.user import User
    from app.db.models.dataset import Dataset
    from app.db.models.dataset_asset import (
        StorageBinding,
        StorageLocation,
        DatasetAssetLocation,
    )
    from app.db.models.storage_connection import StorageConnection
    from app.db.models.dataset_source import DatasetSource, DatasetScanEntry
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.services.dataset_references import register_entry
    from app.services.source_readers.s3 import S3Access
    from tests.test_s3_references import s3_world, scan
    from tests.test_managed_original_creation import Client

    db = copy_db
    actor = User(
        id=uuid.uuid4(),
        name="copy",
        email=uuid.uuid4().hex + "@test.invalid",
        password_hash="unused",
        role="super_admin",
        is_active=True,
    )
    db.add(actor)
    await db.flush()
    world = await s3_world.__wrapped__(db, (actor, ""), monkeypatch)
    memory, actor, dataset, location, source = world
    await ensure_legacy_bindings(db)
    await db.execute(
        update(StorageConnection)
        .where(StorageConnection.id == location.connection_id)
        .values(scope="global")
    )
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(location_id=location.id, bucket="external", generation=2)
    )
    source.ownership_mode = "managed_copy"
    await db.commit()
    run = await scan(db, world)
    entry = await db.scalar(
        select(DatasetScanEntry).where(
            DatasetScanEntry.run_id == run.id, DatasetScanEntry.kind == "file"
        )
    )

    class Hybrid(Client):
        def head_object(self, **params):
            return (
                memory.head_object(**params)
                if params["Key"] in memory.objects
                else super().head_object(**params)
            )

        def get_object(self, **params):
            return (
                memory.get_object(**params)
                if params["Key"] in memory.objects
                else super().get_object(**params)
            )

        def list_objects_v2(self, **params):
            return memory.list_objects_v2(**params)

    physical = Hybrid()

    @contextmanager
    def transport(self, *, read_timeout=None):
        yield physical

    monkeypatch.setattr(S3Access, "client", transport)
    loop = asyncio.get_running_loop()
    observed = []

    async def check():
        async with async_sessionmaker(db.bind)() as other:
            await other.execute(text("SET LOCAL lock_timeout='500ms'"))
            for model, condition in (
                (StorageConnection, StorageConnection.id == location.connection_id),
                (StorageLocation, StorageLocation.id == location.id),
                (StorageBinding, StorageBinding.purpose == "datasets"),
                (DatasetSource, DatasetSource.id == source.id),
                (Dataset, Dataset.id == dataset.id),
            ):
                await other.execute(select(model).where(condition).with_for_update())

    def hook(operation, params):
        asyncio.run_coroutine_threadsafe(check(), loop).result(2)
        observed.append(operation)

    physical.hook = hook
    result = await register_entry(
        db, run.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    assert result["created"] and "complete" in observed
    owner = await db.scalar(
        select(DatasetAssetLocation).where(
            DatasetAssetLocation.location_id == location.id
        )
    )
    assert owner.storage_ref["binding_generation"] == 2
    assert owner.object_key.startswith("root/_aap_managed/datasets/")
