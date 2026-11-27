"""Physical work precedes the existing request-level metadata publication."""

import uuid

import pytest
from sqlalchemy import select

from app.db.models.dataset import DatasetItem, Scene
from app.db.models.managed_original_creation import ManagedOriginalCreation
from tests.test_datasets_upload_zip import (
    _bearer,
    _make_zip,
    _patch_storage_clients,
)
from tests.test_dataset_assets import local_creation_client as local_creation_client
from tests.fakes.managed_creation_storage import CreationStorage


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "boundary",
        "inactive",
        "key_rotate",
        "key_revoke",
        "key_scope",
        "key_expiry",
        "directory_access",
    ],
)
async def test_zip_seals_all_bytes_before_metadata_and_rolls_back_late_authority(
    copy_db, app_module, monkeypatch, change
):
    from datetime import UTC, datetime, timedelta

    import httpx
    from sqlalchemy import func, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from yoao_assets_source_runtime.ingest_workflow import IngestWorkflow
    from app.core.security import create_access_token, hash_password
    from app.db.models.api_key import ApiKey
    from app.db.models.dataset import Dataset
    from app.db.models.user import User
    from app.deps import get_db
    from app.services.dataset_assets import ensure_legacy_bindings
    from tests.factory import create_api_key
    from tests.test_dataset_upload_native import _seed_world
    from tests.test_datasets_upload_zip import _use_local_source_runtime
    from tests.test_s3_references import s3_world

    engine = copy_db.bind
    maker = async_sessionmaker(engine, expire_on_commit=False)
    world = await _seed_world(engine)
    async with maker.begin() as owner:
        await ensure_legacy_bindings(owner)
    if change == "directory_access":
        async with maker.begin() as owner:
            actor = await owner.get(User, world.actor_id)
            actor.role = "project_admin"
            await owner.flush()
            _, _, dataset, _, _ = await s3_world.__wrapped__(
                owner, (actor, ""), monkeypatch
            )
            world.dataset_id = dataset.id
    key_id = None
    token = create_access_token(
        str(world.actor_id),
        "project_admin" if change == "directory_access" else "super_admin",
    )
    if change.startswith("key_"):
        async with maker.begin() as owner:
            key, token = await create_api_key(
                owner, world.actor_id, "ZIP authority", ["datasets:write"]
            )
            key_id = key.id
            if change == "key_expiry":
                key.expires_at = datetime.now(UTC) + timedelta(minutes=10)
    fake = CreationStorage()
    _use_local_source_runtime(monkeypatch)
    _patch_storage_clients(monkeypatch, fake)
    monkeypatch.setattr("app.services.assets_source._managed_client", lambda _: fake)
    original = IngestWorkflow._finish_original
    sealed = []
    accepted = change in {"none", "boundary"}
    entries = (
        {f"lidar/{i:03}.pcd": f"content-{i}".encode() for i in range(201)}
        if change == "boundary"
        else {"lidar/0.pcd": b"first", "lidar/1.pcd": b"second"}
    )

    async def finish_original(workflow, target, proof):
        assert not await workflow._data._db.scalar(
            select(DatasetItem.id).where(DatasetItem.dataset_id == world.dataset_id)
        )
        await original(workflow, target, proof)
        sealed.append(target.creation_id)
        if not accepted and len(sealed) == 2:
            async with maker.begin() as observer:
                if change == "inactive":
                    await observer.execute(
                        update(User)
                        .where(User.id == world.actor_id)
                        .values(is_active=False)
                    )
                elif change == "directory_access":
                    await observer.execute(
                        update(Dataset)
                        .where(Dataset.id == world.dataset_id)
                        .values(created_by=world.other_actor_id)
                    )
                elif change == "key_rotate":
                    await observer.execute(
                        update(ApiKey)
                        .where(ApiKey.id == key_id)
                        .values(key_hash=hash_password("rotated test key"))
                    )
                elif change == "key_scope":
                    await observer.execute(
                        update(ApiKey)
                        .where(ApiKey.id == key_id)
                        .values(scopes=["datasets:write", "datasets:read"])
                    )
                elif change == "key_expiry":
                    await observer.execute(
                        update(ApiKey)
                        .where(ApiKey.id == key_id)
                        .values(expires_at=datetime.now(UTC) + timedelta(minutes=20))
                    )
                else:
                    await observer.execute(
                        update(ApiKey)
                        .where(ApiKey.id == key_id)
                        .values(revoked_at=datetime.now(UTC))
                    )

    monkeypatch.setattr(IngestWorkflow, "_finish_original", finish_original)

    async def request_db():
        async with maker() as session:
            yield session

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            result = await client.post(
                f"/api/v1/datasets/{world.dataset_id}/items/upload-zip",
                headers=_bearer(token),
                files={
                    "file": (
                        "scene.zip",
                        _make_zip(entries),
                        "application/zip",
                    )
                },
            )
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous

    assert result.status_code == (200 if accepted else 403), result.text
    if accepted:
        assert result.json()["added"] == len(entries)
    assert len(sealed) == len(entries)
    async with maker() as observer:
        count = await observer.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.dataset_id == world.dataset_id)
        )
        assert count == (len(entries) if accepted else 0)
        scene_count = await observer.scalar(
            select(func.count())
            .select_from(Scene)
            .where(Scene.dataset_id == world.dataset_id)
        )
        assert scene_count == (1 if accepted else 0)
        for creation_id in sealed:
            creation = await observer.get(ManagedOriginalCreation, creation_id)
            assert creation.status == "verified"
            assert creation.dataset_id == world.dataset_id
            assert creation.verification is not None
            assert creation.object_key in fake.objects


async def test_scan_defers_publication_and_authority_locks_until_after_last_batch(
    copy_db, app_module, monkeypatch
):
    import asyncio

    import httpx
    from sqlalchemy import func, text, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from yoao_assets_source_runtime.ingest_workflow import IngestWorkflow
    from app.core.security import create_access_token
    from app.db.models.dataset import Dataset
    from app.db.models.user import User
    from app.deps import get_db
    from app.services.dataset_assets import ensure_legacy_bindings
    from tests.test_dataset_upload_native import _seed_world
    from tests.test_datasets_upload_zip import _use_local_source_runtime

    engine = copy_db.bind
    maker = async_sessionmaker(engine, expire_on_commit=False)
    world = await _seed_world(engine)
    async with maker.begin() as owner:
        await ensure_legacy_bindings(owner)
        dataset = await owner.get(Dataset, world.dataset_id)
        prefix = dataset.name + "/"
    fake = CreationStorage()
    fake.objects.update(
        {f"{prefix}lidar/{i:03}.pcd": f"content-{i}".encode() for i in range(201)}
    )
    _use_local_source_runtime(monkeypatch)
    _patch_storage_clients(monkeypatch, fake)
    monkeypatch.setattr("app.services.assets_source._managed_client", lambda _: fake)
    original = IngestWorkflow._finish_original
    sealed = []

    async def finish_original(workflow, target, proof):
        assert not await workflow._data._db.scalar(
            select(DatasetItem.id).where(DatasetItem.dataset_id == world.dataset_id)
        )
        await original(workflow, target, proof)
        sealed.append(target.creation_id)
        if len(sealed) == 201:
            async with maker.begin() as observer:
                await observer.execute(text("SET LOCAL lock_timeout='2s'"))
                await observer.execute(
                    update(User)
                    .where(User.id == world.actor_id)
                    .values(is_active=False)
                )

    monkeypatch.setattr(IngestWorkflow, "_finish_original", finish_original)

    async def request_db():
        async with maker() as session:
            yield session

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            result = await asyncio.wait_for(
                client.post(
                    f"/api/v1/datasets/{world.dataset_id}/items/scan",
                    headers=_bearer(
                        create_access_token(str(world.actor_id), "super_admin")
                    ),
                ),
                timeout=60,
            )
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous

    assert result.status_code == 403, result.text
    assert len(sealed) == 201
    async with maker() as observer:
        assert (
            await observer.scalar(
                select(func.count())
                .select_from(DatasetItem)
                .where(DatasetItem.dataset_id == world.dataset_id)
            )
            == 0
        )
        for creation_id in sealed:
            creation = await observer.get(ManagedOriginalCreation, creation_id)
            assert creation.status == "verified"
            assert creation.dataset_id == world.dataset_id


@pytest.fixture
async def copy_db(managed_database):
    """Keep committed Native facts and independent sessions in one owned database."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with managed_database() as engine:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                yield db
            finally:
                await db.rollback()


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
        DatasetAsset,
        DatasetAssetLocation,
    )
    from app.db.models.storage_connection import StorageConnection
    from app.db.models.dataset_source import DatasetSource, DatasetScanEntry
    from app.services.dataset_assets import ensure_legacy_bindings
    from app.application.dataset_reference_registration import register_entry
    from app.services.source_readers.s3 import S3Access
    from tests.test_s3_references import s3_world, scan
    from tests.managed_original_client_support import Client

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

    from app.services import managed_s3_target, saved_media_io

    monkeypatch.setattr(saved_media_io, "registered", lambda _target: False)
    monkeypatch.setattr(S3Access, "client", transport)
    monkeypatch.setattr(
        managed_s3_target.Target,
        "client",
        contextmanager(lambda self, **kwargs: (yield physical)),
    )
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
        select(DatasetAssetLocation)
        .join(DatasetAsset, DatasetAsset.id == DatasetAssetLocation.asset_id)
        .where(
            DatasetAsset.dataset_item_id == result["item_id"],
            DatasetAssetLocation.location_id == location.id,
        )
    )
    assert owner.storage_ref["binding_generation"] == 2
    assert owner.object_key.startswith("root/_aap_managed/datasets/")
    assert owner.bucket == owner.storage_ref["bucket"] == "external"
    assert owner.object_key == owner.storage_ref["object_key"]
    creation = await db.get(ManagedOriginalCreation, owner.creation_id)
    assert creation.status == "verified"
    assert (creation.location_id, creation.bucket, creation.object_key) == (
        owner.location_id,
        owner.bucket,
        owner.object_key,
    )
    assert physical.objects[owner.bucket, owner.object_key]["data"] == memory.data
