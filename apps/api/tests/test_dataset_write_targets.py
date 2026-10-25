"""New allocations move once; saved write owners survive binding changes."""

from contextlib import contextmanager
from copy import deepcopy
import uuid

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAssetLocation,
    StorageBinding,
    StorageLocation,
)
from app.db.models.storage_connection import StorageConnection
from app.services import dataset_storage, managed_original_creation as creation
from app.services.dataset_assets import register_item_asset, LEGACY_LOCATION_ID
from app.services.dataset_storage_keys import managed_dataset_key
from app.services.sources.s3_reference import S3Access
from app.services.storage_location import register_connection
from tests.test_auxiliary_originals import auxiliary_db as auxiliary_db
from tests.test_managed_original_creation import (
    allocation,
    client as client,
    create,
    creation_db as creation_db,
    migration,
)


async def switch(db, monkeypatch, client, *, base="company"):
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    connection = StorageConnection(
        id=uuid.uuid4(),
        name="new dataset writes",
        kind="s3",
        scope="global",
        config={
            "endpoint": "http://objects.example.test",
            "bucket": "saved-b",
            "base_prefix": base,
            "use_ssl": False,
        },
        secret_enc=encrypt_secret({"access_key": "private", "secret_key": "private"}),
        created_by=uuid.uuid4(),
    )
    db.add(connection)
    await db.flush()
    location = await register_connection(db, connection)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(location_id=location.id, bucket="saved-b", generation=2)
    )
    await db.commit()
    timeouts = []

    @contextmanager
    def transport(self, *, read_timeout=None):
        timeouts.append(read_timeout)
        yield client

    monkeypatch.setattr(S3Access, "client", transport)
    return connection, location, timeouts


async def test_first_allocation_bootstraps_independently_without_business_commit(
    creation_db,
):
    db = creation_db
    await db.execute(delete(StorageBinding))
    await db.execute(delete(StorageLocation))
    await db.commit()
    pending = DatasetItem(
        dataset_id=uuid.uuid4(),
        file_name="pending",
        file_path="pending",
        file_type="image",
    )
    db.add(pending)
    await db.flush()
    identifier = pending.id
    row = await allocation(db, origin="direct", staging=True)
    await db.rollback()
    assert await db.get(DatasetItem, identifier) is None
    assert (await creation._read(db, row["id"]))["status"] == "allocated"
    assert await db.get(StorageBinding, "datasets") is not None
    assert creation.original_ref(row)["binding_generation"] == 1
    assert creation.staging_owner(row).bucket == creation.storage_service.import_bucket


async def test_new_b_write_saved_a_and_task_independence_and_short_target_locks(
    auxiliary_db, client, monkeypatch
):
    db = auxiliary_db
    old = await allocation(db)
    task = await creation.allocate_task(
        db,
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        logical_path="task.bin",
        max_bytes=10,
    )
    client.store(
        task["staging_bucket"],
        task["staging_key"],
        b"original",
        content_type=task["content_type"],
    )
    connection, location, timeouts = await switch(db, monkeypatch, client)
    new = await allocation(db)
    reference = creation.original_ref(new)
    assert new["location_id"] == location.id and new["bucket"] == "saved-b"
    assert new["object_key"].startswith("company/_aap_managed/datasets/")
    assert reference["binding_generation"] == 2

    async def independent_locks():
        async with async_sessionmaker(db.bind)() as other:
            await other.execute(text("SET LOCAL lock_timeout='500ms'"))
            for model, condition in (
                (StorageConnection, StorageConnection.id == connection.id),
                (StorageLocation, StorageLocation.id == location.id),
                (StorageBinding, StorageBinding.purpose == "datasets"),
            ):
                await other.execute(select(model).where(condition).with_for_update())

    # Every provider call can independently lock the target registry rows.
    import asyncio

    loop = asyncio.get_running_loop()

    def check_locks(operation, params):
        asyncio.run_coroutine_threadsafe(independent_locks(), loop).result(2)

    client.hook = check_locks
    result = await create(db, new)
    await create(db, old)
    await creation.complete_direct(db, task)
    assert timeouts and all(value >= 10 for value in timeouts if value is not None)
    assert (old["bucket"], old["object_key"]) in client.objects
    assert (task["bucket"], task["object_key"]) in client.objects
    item = DatasetItem(
        dataset_id=new["dataset_id"],
        file_name="original.bin",
        file_path=new["object_key"],
        file_type="image",
        file_size=result.size_bytes,
    )
    db.add(item)
    await db.flush()
    await register_item_asset(
        db,
        item,
        binding=creation.original_locator(new),
        storage_ref=reference,
        sha256=result.sha256,
        size_bytes=result.size_bytes,
        etag=result.etag,
    )
    await creation.attach(db, item, new["id"])
    await db.commit()
    owner = await db.scalar(
        select(DatasetAssetLocation).where(
            DatasetAssetLocation.creation_id == new["id"]
        )
    )
    assert owner.storage_ref == reference
    # Credential revisions are current; the saved address and allocation revisions remain exact.
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
    await db.commit()
    target = await dataset_storage.target_for_ref(db, reference)
    assert target.config_revision == 2 and target.connection_revision == 2
    assert (
        await creation.for_key(
            db, dataset_id=new["dataset_id"], object_key=new["object_key"]
        )
    )["configuration"]["original_ref"] == reference
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(DatasetAssetLocation)
                .where(DatasetAssetLocation.asset_id == owner.asset_id)
                .values(storage_ref=None)
            )


async def test_historical_schema_one_full_fingerprint_survives_datasets_switch(
    creation_db, client, monkeypatch
):
    db = creation_db
    row = await allocation(db)
    historical = deepcopy(row)
    historical["id"] = uuid.uuid4()
    historical["object_key"] = managed_dataset_key(row["dataset_id"], "historical.bin")
    historical["configuration"].pop("original_ref")
    historical["configuration"]["schema"] = 1
    historical["input_fingerprint"] = creation._digest(historical["configuration"])
    await db.execute(insert(creation.Creation).values(**historical))
    await db.commit()
    await switch(db, monkeypatch, client)
    result = await create(db, historical)
    assert result.size_bytes == len(b"original")
    assert historical["location_id"] == LEGACY_LOCATION_ID
    forged = deepcopy(historical)
    forged["configuration"]["buckets"]["avatars"] = "forged"
    with pytest.raises(creation.ContentError, match="configuration_changed"):
        await creation._validate(db, forged)


async def test_saved_dataset_facts_block_destructive_downgrade(auxiliary_db):
    db = auxiliary_db
    await allocation(db)
    with pytest.raises(Exception, match="Saved dataset original responsibility"):
        async with db.begin_nested():
            await (await db.connection()).run_sync(
                lambda sync: migration(
                    sync, "0251_dataset_original_targets.py"
                ).downgrade()
            )


async def test_auxiliary_b_saved_ref_fits_full_key_budget_and_keeps_fact_contract(
    auxiliary_db, client, monkeypatch
):
    from app.services import auxiliary_originals
    from app.services.dataset_storage_target import MANAGED_NAMESPACE
    from tests.test_auxiliary_originals import saved

    base = "x" * (891 - len(MANAGED_NAMESPACE) - 1)
    _, location, _ = await switch(auxiliary_db, monkeypatch, client, base=base)
    owner, row, _ = await saved(auxiliary_db)
    assert owner.location_id == location.id
    assert owner.storage_ref == creation.original_ref(row)
    assert len(owner.object_key) == 1000 and len(owner.object_key.encode()) == 1000
    assert auxiliary_originals.resolve(owner).object_key == owner.object_key
    assert "storage_ref" not in auxiliary_originals.FACTS
    assert (
        tuple(auxiliary_originals.snapshot(owner).__dataclass_fields__)
        == auxiliary_originals.FACTS
    )
    with pytest.raises(creation.ContentError, match="invalid_allocation"):
        await allocation(auxiliary_db, logical_path="ab")


async def test_nullable_migration_roundtrip_preserves_legacy_current_address(
    auxiliary_db,
):
    db = auxiliary_db
    identifier = uuid.uuid4()
    await db.execute(
        insert(DatasetAssetLocation).values(
            asset_id=identifier,
            location_id=LEGACY_LOCATION_ID,
            bucket="historical-saved-bucket",
            object_key="legacy/name.png",
            role="managed_original",
            reference_state="unverified",
        )
    )
    connection = await db.connection()
    await connection.run_sync(
        lambda sync: migration(sync, "0251_dataset_original_targets.py").downgrade()
    )
    await connection.run_sync(
        lambda sync: migration(sync, "0251_dataset_original_targets.py").upgrade()
    )
    row = (
        (
            await db.execute(
                select(DatasetAssetLocation.__table__).where(
                    DatasetAssetLocation.asset_id == identifier
                )
            )
        )
        .mappings()
        .one()
    )
    assert row["storage_ref"] is None
    assert (
        row["bucket"] == "historical-saved-bucket"
        and row["object_key"] == "legacy/name.png"
    )


async def test_attach_rejects_ref_from_another_allocation_generation(
    auxiliary_db, client, monkeypatch
):
    db = auxiliary_db
    await switch(db, monkeypatch, client)
    row = await allocation(db)
    result = await create(db, row)
    ref = deepcopy(creation.original_ref(row))
    ref["binding_generation"] += 1
    item = DatasetItem(
        dataset_id=row["dataset_id"],
        file_name="wrong.bin",
        file_path=row["object_key"],
        file_type="image",
        file_size=result.size_bytes,
    )
    db.add(item)
    await db.flush()
    await register_item_asset(
        db,
        item,
        storage_ref=ref,
        sha256=result.sha256,
        size_bytes=result.size_bytes,
        etag=result.etag,
    )
    with pytest.raises(creation.ContentError, match="attachment_mismatch"):
        await creation.attach(db, item, row["id"])


async def test_saved_current_owner_classifies_only_its_recorded_endpoint(
    auxiliary_db, client, monkeypatch
):
    from app.services import storage_usage_categories as categories
    from app.services.dataset_storage_target import target_for
    from app.services.storage_location import location_is_referenced

    db = auxiliary_db
    _, location, _ = await switch(db, monkeypatch, client)
    target = await target_for(db, location)
    key = target.key_prefix + "restored-owner"
    ref = dataset_storage.make_ref(target, 2, key)
    await db.execute(
        insert(DatasetAssetLocation).values(
            asset_id=uuid.uuid4(),
            location_id=location.id,
            bucket=target.bucket,
            object_key=key,
            role="managed_original",
            reference_state="present",
            storage_ref=ref,
        )
    )
    assert await location_is_referenced(db, location.id)
    objects = [{"key": key, "size": 7}]
    monkeypatch.setattr(categories, "endpoint", lambda: target.endpoint_url)
    matched = await categories.classify(db, target.bucket, objects)
    assert matched["categories"]["managed_original"]["object_count"] == 1
    monkeypatch.setattr(categories, "endpoint", lambda: "https://other.example.test")
    unmatched = await categories.classify(db, target.bucket, objects)
    assert unmatched["categories"]["managed_original"]["object_count"] == 0
    assert unmatched["categories"]["unclassified"]["object_count"] == 1
