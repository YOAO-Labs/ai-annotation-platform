"""Real independent publication transactions and historical migration fixtures."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creation
from app.services import task_originals as service
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from tests.factory import create_project
from tests.managed_image_inference_fixtures import managed_database
from tests.managed_original_client_support import Client, stage_client


@pytest.fixture
async def current_original_db(test_engine):
    """Use public Native owners in a freshly migrated, fixture-owned database."""
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.user import User

    async with managed_database(test_engine.url) as engine:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            actor = User(
                id=uuid.uuid4(),
                email=uuid.uuid4().hex + "@example.test",
                name="Original fixture owner",
                password_hash="unused",
                role="project_admin",
                is_active=True,
            )
            db.add(actor)
            await db.flush()
            project = await create_project(db, owner_id=actor.id)
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id="orig-" + uuid.uuid4().hex[:12],
                name="Original fixture dataset",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            item = DatasetItem(
                id=uuid.uuid4(),
                dataset_id=dataset.id,
                file_name="fixture.bin",
                file_path="fixture/" + uuid.uuid4().hex,
                file_type="other",
            )
            db.add(item)
            await ensure_legacy_bindings(db)
            await db.commit()
            db.info["original_fixture"] = (actor.id, project.id, item.id)
            try:
                yield db
            finally:
                await db.rollback()


def migration(connection, number):
    filename = {
        "0219": "0219_managed_original_creations.py",
        "0230": "0230_managed_original_task_scope.py",
        "0231": "0231_task_originals.py",
        "0253": "0253_annotations_storage_targets.py",
    }[number]
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "original_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def original_db(test_engine):
    schema = "task_original_test_" + uuid.uuid4().hex
    tables = (
        "storage_connections",
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "storage_migration_read_publications",
        "storage_migration_read_routes",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "managed_original_retirements",
        "tasks",
        "task_dataset_item_links",
        "comment_attachment_uploads",
        "guide_asset_uploads",
        "raster_mask_objects",
    )
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        source = await connection.scalar(text("SELECT current_schema()"))
        for table in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{table}" (LIKE "{source}"."{table}" INCLUDING ALL)'
                )
            )
        for table in ("dataset_asset_locations", "managed_original_retirements"):
            await connection.execute(
                text(
                    f'ALTER TABLE "{schema}"."{table}" DROP COLUMN IF EXISTS creation_id'
                )
            )
        await connection.execute(
            text(f'ALTER TABLE "{schema}".tasks DROP COLUMN IF EXISTS original_id')
        )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            for number in ("0219", "0230", "0231"):
                await connection.run_sync(
                    lambda sync, number=number: migration(sync, number).upgrade()
                )
            # The current writer also carries the independently tested staging
            # reference; these scope fixtures keep the historical owner guards.
            await connection.execute(
                text(
                    "ALTER TABLE managed_original_creations ADD COLUMN staging_ref jsonb"
                )
            )
            await connection.run_sync(lambda sync: migration(sync, "0253").upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
def original_client(monkeypatch):
    client = Client()
    monkeypatch.setattr(creation, "create_client", lambda **_: client)
    stage_client(monkeypatch, client)
    return client


async def allocated(db, *, commit=True, legacy=False):
    fixture = db.info.get("original_fixture")
    actor_id, project_id = fixture[:2] if fixture else (uuid.uuid4(), uuid.uuid4())
    task = Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id="TO-" + uuid.uuid4().hex[:20],
        file_name="original.bin",
        file_path="not-allocated",
        status="uploading",
        dataset_item_id=None,
    )
    if fixture is None:
        db.add(task)
        await db.flush()
    allocation = await creation.allocate_task(
        db,
        project_id=task.project_id,
        task_id=task.id,
        logical_path=task.file_name,
        max_bytes=100,
        requested_by=actor_id,
        authority={},
    )
    if legacy:
        allocation = deepcopy(allocation)
        allocation["id"] = uuid.uuid4()
        allocation["object_key"] = service.key_for(task.id, allocation["id"])
        allocation["configuration"].pop("original_ref")
        allocation["configuration"].pop("schema")
        allocation["configuration"]["buckets"]["import"] = allocation["staging_bucket"]
        allocation["input_fingerprint"] = creation._digest(allocation["configuration"])
        allocation["staging_ref"] = None
        allocation["staging_key"] = (
            f"{creation.STAGING_PREFIX}/{allocation['id']}/payload"
        )
        async with service.storage_access.engine(db).begin() as connection:
            await connection.execute(
                creation.Creation.__table__.insert().values(**allocation)
            )
    if fixture is not None:
        inserted = await db.scalar(
            text(
                "SELECT aap_labeling_v1.create_uploading_task("
                ":task_id, :project_id, :display_id, :file_name, "
                ":file_path, :file_type, :actor_id)"
            ),
            {
                "task_id": task.id,
                "project_id": task.project_id,
                "display_id": task.display_id,
                "file_name": task.file_name,
                "file_path": allocation["object_key"],
                "file_type": "other",
                "actor_id": actor_id,
            },
        )
        assert inserted is True
        task = await db.get(Task, task.id)
    else:
        task.file_path = allocation["object_key"]
        await db.flush()
    if commit:
        await db.commit()
    return task, allocation


async def verified(db, client, *, commit=True, data=b"original", legacy=False):
    task, allocation = await allocated(db, commit=commit, legacy=legacy)
    client.store(allocation["staging_bucket"], allocation["staging_key"], data)
    created = await creation.complete_direct(db, allocation)
    return task, allocation, created


async def owners(db):
    async with service.storage_access.engine(db).connect() as connection:
        return list(
            (await connection.execute(select(TaskOriginal.__table__))).mappings()
        )


async def test_owner_commits_before_pointer_without_committing_caller(
    current_original_db, original_client
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    identifier = task.id
    await db.scalar(
        select(Task).where(Task.id == identifier).with_for_update(nowait=True)
    )
    calls = list(original_client.calls)
    owner = await service.publish(
        db, task, allocation, created, actor_id=allocation["requested_by"]
    )
    assert owner.id == created.creation_id == task.original_id
    assert owner.creation_id == owner.id and owner.task_id == identifier
    assert owner.created_at == (await creation._read(db, owner.id))["completed_at"]
    assert len(await owners(db)) == 1 and original_client.calls == calls
    async with service.storage_access.engine(db).connect() as connection:
        assert (
            await connection.scalar(
                select(Task.original_id).where(Task.id == identifier)
            )
            is None
        )
    await db.rollback()
    assert (await db.get(Task, identifier)).original_id is None
    assert len(await owners(db)) == 1


async def test_uncommitted_task_can_publish_and_rollback_retains_owner(
    current_original_db, original_client
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client, commit=False)
    identifier = task.id
    await service.publish(
        db, task, allocation, created, actor_id=allocation["requested_by"]
    )
    await db.rollback()
    assert await db.get(Task, identifier) is None
    assert (await owners(db))[0]["task_id"] == identifier
    assert (await creation._read(db, created.creation_id))["status"] == "verified"


async def test_idempotent_publication_then_task_delete_preserves_evidence(
    current_original_db, original_client
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    first = await service.publish(
        db, task, allocation, created, actor_id=allocation["requested_by"]
    )
    await db.commit()
    calls = list(original_client.calls)
    second = await service.publish(
        db, task, allocation, created, actor_id=allocation["requested_by"]
    )
    assert service.same(first, second) and len(await owners(db)) == 1
    assert original_client.calls == calls
    await db.delete(task)
    await db.commit()
    assert len(await owners(db)) == 1
    assert (await creation._read(db, first.id))["status"] == "verified"
    assert (allocation["bucket"], allocation["object_key"]) in original_client.objects


@pytest.mark.parametrize(
    "changes",
    [
        {"creation_id": uuid.uuid4()},
        {"sha256": "f" * 64},
        {"size_bytes": 99},
        {"md5": "f" * 32},
        {"etag": '"forged"'},
        {"version_id": "wrong-version"},
    ],
)
async def test_caller_receipt_cannot_replace_verified_facts(
    current_original_db, original_client, changes
):
    task, allocation, created = await verified(current_original_db, original_client)
    calls = list(original_client.calls)
    with pytest.raises(ContentError, match="task_original_creation_conflict"):
        await service.publish(
            current_original_db,
            task,
            allocation,
            replace(created, **changes),
            actor_id=allocation["requested_by"],
        )
    assert not await owners(current_original_db) and original_client.calls == calls
    assert task.original_id is None


async def test_unfinished_creation_is_not_publishable(current_original_db):
    task, allocation = await allocated(current_original_db)
    fake = creation.CreatedOriginal(
        allocation["id"], "f" * 64, 0, "f" * 32, '"fake"', None, b""
    )
    with pytest.raises(ContentError, match="task_original_creation_conflict"):
        await service.publish(
            current_original_db,
            task,
            allocation,
            fake,
            actor_id=allocation["requested_by"],
        )
    assert not await owners(current_original_db) and task.original_id is None


@pytest.mark.parametrize("kind", ["dataset_item", "primary_lidar"])
async def test_null_item_alone_does_not_prove_standalone_scope(
    current_original_db, original_client, kind
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    if kind == "dataset_item":
        task.dataset_item_id = db.info["original_fixture"][2]
    else:
        db.add(
            TaskDatasetItemLink(
                task_id=task.id,
                dataset_item_id=db.info["original_fixture"][2],
                role="primary_lidar",
            )
        )
    await db.flush()
    with pytest.raises(ContentError, match="task_original_creation_conflict"):
        await service.publish(
            db, task, allocation, created, actor_id=allocation["requested_by"]
        )
    assert not await owners(db)


async def test_maintenance_change_rejects_before_owner_publication(
    current_original_db, original_client
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    await db.execute(
        update(StorageMaintenanceGate)
        .where(StorageMaintenanceGate.location_id == LEGACY_LOCATION_ID)
        .values(revision=2)
    )
    await db.commit()
    with pytest.raises(ContentError, match="storage_maintenance_changed"):
        await service.publish(
            db, task, allocation, created, actor_id=allocation["requested_by"]
        )
    assert not await owners(db) and task.original_id is None


async def test_unknown_commit_ack_keeps_owner_and_does_not_attach(
    current_original_db, original_client, monkeypatch
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    persist = service._persist

    async def unknown(*args):
        await persist(*args)
        raise OSError("lost commit acknowledgement")

    monkeypatch.setattr(service, "_persist", unknown)
    with pytest.raises(ContentError, match="task_original_publish_unknown") as error:
        await service.publish(
            db, task, allocation, created, actor_id=allocation["requested_by"]
        )
    assert error.value.status == 503
    assert len(await owners(db)) == 1 and task.original_id is None
    assert (allocation["bucket"], allocation["object_key"]) in original_client.objects


async def test_repeated_cancel_drains_independent_commit_and_keeps_owner(
    current_original_db, original_client, monkeypatch
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    persist = service._persist
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args):
        entered.set()
        await release.wait()
        return await persist(*args)

    monkeypatch.setattr(service, "_persist", blocked)
    operation = asyncio.create_task(
        service.publish(
            db, task, allocation, created, actor_id=allocation["requested_by"]
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
    assert len(await owners(db)) == 1 and task.original_id is None


async def test_duplicate_independent_publishers_share_one_receipt(
    current_original_db, original_client
):
    db = current_original_db
    task, allocation, created = await verified(db, original_client)
    source = await creation._read(db, created.creation_id)
    values = {
        field: source[field]
        for field in service.FACTS
        if field not in {"creation_id", "created_at"}
    }
    values.update(
        creation_id=created.creation_id,
        created_at=source["completed_at"],
        storage_ref=creation.original_ref(source),
    )
    left, right = await asyncio.gather(
        service._durable_owner(db, values), service._durable_owner(db, values)
    )
    assert service.same(left, right) and len(await owners(db)) == 1
    assert task.original_id is None


def receipt(**changes):
    identifier, task_id = uuid.uuid4(), uuid.uuid4()
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return SimpleNamespace(
        **(
            {
                "id": identifier,
                "creation_id": identifier,
                "task_id": task_id,
                "project_id": uuid.uuid4(),
                "location_id": LEGACY_LOCATION_ID,
                "storage_identity": managed_storage_identity(
                    endpoint, "saved-originals"
                ),
                "bucket": "saved-originals",
                "object_key": service.key_for(task_id, identifier),
                "sha256": hashlib.sha256(b"").hexdigest(),
                "size_bytes": 0,
                "etag": '"source-etag"',
                "version_id": "source-version",
                "created_at": datetime(2026, 10, 24, tzinfo=timezone.utc),
            }
            | changes
        )
    )


def test_frozen_source_receipt_uses_saved_bucket_and_explicit_endpoint():
    row = receipt()
    frozen = service.snapshot(row)
    assert service.same(row, frozen)
    with pytest.raises(FrozenInstanceError):
        frozen.bucket = "other"
    target_endpoint = "http://restored.invalid:9000"
    row.bucket = "restored-annotations"
    row.storage_identity = managed_storage_identity(target_endpoint, row.bucket)
    target = service.resolve(row, endpoint_url=target_endpoint)
    assert target.bucket == row.bucket and row.etag == frozen.etag
    assert row.version_id == frozen.version_id and not service.same(row, frozen)
    with pytest.raises(ContentError, match="task_original_invalid_location"):
        service.resolve(row)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "not-a-uuid"},
        {"creation_id": uuid.uuid4()},
        {"task_id": uuid.uuid4()},
        {"project_id": None},
        {"location_id": uuid.uuid4()},
        {"object_key": "old/path"},
        {"sha256": "F" * 64},
        {"size_bytes": True},
        {"size_bytes": -1},
        {"size_bytes": 2**63},
        {"etag": ""},
        {"etag": "bad\nheader"},
        {"version_id": "null"},
        {"created_at": datetime(2026, 1, 1)},
        {"bucket": "bad/path"},
        {"bucket": "bad\\path"},
        {"bucket": "bad bucket"},
        {"storage_identity": "f" * 64},
    ],
)
def test_strict_facts_do_not_adopt_unknown_paths(changes):
    with pytest.raises(ContentError):
        service.resolve(receipt(**changes))


def test_missing_owner_is_explicitly_unpinned():
    with pytest.raises(ContentError, match="task_original_unpinned") as error:
        service.resolve(None)
    assert error.value.status == 409
