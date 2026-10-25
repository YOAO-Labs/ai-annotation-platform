"""Real independent ledger commits live only inside each test's owned schema."""

import asyncio
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import threading
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
)
from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.db.models.storage_maintenance import StorageOperation
from app.services import managed_original_creation as service
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from tests.test_managed_original_writer import MemoryS3


def migration(connection, filename="0219_managed_original_creations.py"):
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "creation_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def creation_db(test_engine):
    # The test scaffold already verifies a disposable database. Keep truly
    # committed immutable facts in an owned schema, then remove that namespace
    # as a whole; never disable guards or DELETE creation history for cleanup.
    schema = "creation_test_" + uuid.uuid4().hex
    tables = (
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
        "storage_usage_mutations",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "managed_original_retirements",
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
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: migration(sync).upgrade())
            await connection.run_sync(
                lambda sync: migration(
                    sync, "0230_managed_original_task_scope.py"
                ).upgrade()
            )
            # Keep this historical scope fixture usable by the current writer;
            # the complete staging migration and its guards have separate tests.
            await connection.execute(
                text(
                    "ALTER TABLE managed_original_creations ADD COLUMN staging_ref jsonb"
                )
            )
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


class Client(MemoryS3):
    def __init__(self):
        super().__init__()
        self.closed = 0
        self.close_failure = False

    def close(self):
        self.closed += 1
        if self.close_failure:
            raise OSError("private close detail")


@pytest.fixture
def client(monkeypatch):
    value = Client()
    monkeypatch.setattr(service, "create_client", lambda **_: value)
    stage_client(monkeypatch, value)
    return value


def stage_client(monkeypatch, value):
    scheme = "https" if service.settings.minio_use_ssl else "http"
    value.meta = SimpleNamespace(
        endpoint_url=f"{scheme}://{service.settings.minio_endpoint}"
    )
    monkeypatch.setattr(service.storage_service, "client", value)


async def allocation(db, **fields):
    return await service.allocate(
        db,
        **(
            {
                "dataset_id": uuid.uuid4(),
                "logical_path": "images/original.bin",
                "origin": "zip",
                "max_bytes": 10,
                "content_type": "application/octet-stream",
            }
            | fields
        ),
    )


async def create(db, row, data=b"original"):
    return await service.create_stream(
        db, row, io.BytesIO(data), content_type=row["content_type"]
    )


async def operations(db):
    async with service.storage_access.engine(db).connect() as connection:
        return list(await connection.scalars(select(StorageOperation.id)))


async def test_allocate_survives_business_rollback_without_committing_it(creation_db):
    db = creation_db
    pending = DatasetItem(
        dataset_id=uuid.uuid4(),
        file_name="pending",
        file_path="pending",
        file_type="image",
    )
    db.add(pending)
    await db.flush()
    identifier = pending.id
    row = await allocation(db)
    await db.rollback()
    assert await db.get(DatasetItem, identifier) is None
    assert (await service._read(db, row["id"]))["status"] == "allocated"
    assert row["staging_key"] is None
    assert (
        await service.for_key(
            db, dataset_id=row["dataset_id"], object_key="not-allocated"
        )
        is None
    )


async def test_success_records_ack_before_readback_closes_and_retries_without_io(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    loop = asyncio.get_running_loop()
    observed = []

    def hook(operation, params):
        if operation in {"part", "complete", "head"}:
            current = asyncio.run_coroutine_threadsafe(
                service._read(db, row["id"]), loop
            ).result(2)
            observed.append((operation, current))

    client.hook = hook
    result = await create(db, row)
    current = await service._read(db, row["id"])
    assert current["status"] == "verified" and current["operation_id"]
    assert current["receipt"]["etag"] == result.etag
    assert current["verification"]["sha256"] == result.sha256
    assert any(
        op == "part" and state["status"] == "preparing" and state["operation_id"]
        for op, state in observed
    )
    assert any(
        op == "complete"
        and state["status"] == "writing"
        and state["prepared"]["sha256"]
        for op, state in observed
    )
    assert all(state["receipt"] for op, state in observed if op == "head")
    assert client.closed == 1 and not await operations(db)
    calls = len(client.calls)
    repeated = await create(db, row)
    assert repeated.creation_id == result.creation_id and repeated.head_bytes == b""
    assert len(client.calls) == calls and client.closed == 1


async def test_direct_replay_changes_staging_only_and_keeps_true_version(
    creation_db, client
):
    db = creation_db
    client.versioned = True
    row = await allocation(db, origin="direct", staging=True)
    client.store(row["staging_bucket"], row["staging_key"], b"original")
    result = await service.complete_direct(db, row)
    current = await service._read(db, row["id"])
    assert result.version_id and current["source"]["version_id"]
    assert row["staging_key"].startswith(service.STAGING_PREFIX + "/")
    assert row["object_key"] != row["staging_key"]
    client.store(row["staging_bucket"], row["staging_key"], b"replayed")
    repeated = await service.complete_direct(db, row)
    assert repeated.sha256 == result.sha256
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"
    assert len([call for call in client.calls if call[0] == "complete"]) == 1


async def test_zero_budget_allows_only_empty_and_budget_error_is_413(
    creation_db, client
):
    db = creation_db
    empty = await allocation(db, max_bytes=0)
    assert (await create(db, empty, b"")).size_bytes == 0
    limited = await allocation(db, max_bytes=0)
    with pytest.raises(ContentError) as error:
        await create(db, limited, b"x")
    assert (
        error.value.status == 413
        and error.value.code == "creation_byte_budget_exceeded"
    )
    assert (await service._read(db, limited["id"]))["status"] == "failed"
    assert (limited["bucket"], limited["object_key"]) not in client.objects


async def test_collision_never_adopts_same_sha_or_deletes_current_object(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    client.store(row["bucket"], row["object_key"], b"original")
    with pytest.raises(ContentError) as error:
        await create(db, row)
    current = await service._read(db, row["id"])
    assert current["status"] == "conflict" and current["receipt"] is None
    assert error.value.code == "creation_write_conflict"
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"
    with pytest.raises(ContentError, match="managed_original_creation_terminal"):
        await create(db, row)


async def test_unknown_ack_retains_operation_and_never_retries_final(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    original = client.complete_multipart_upload

    def lost(**params):
        original(**params)
        raise TimeoutError("private response detail")

    client.complete_multipart_upload = lost
    with pytest.raises(ContentError) as error:
        await create(db, row)
    current = await service._read(db, row["id"])
    assert current["status"] == "unknown" and current["receipt"] is None
    assert current["operation_id"] in await operations(db)
    assert error.value.storage_operation_unsettled
    assert (row["bucket"], row["object_key"]) in client.objects
    with pytest.raises(ContentError):
        await create(db, row)
    assert len([call for call in client.calls if call[0] == "complete"]) == 1


async def test_lost_db_ack_preserves_actual_write_receipt_and_unsettled_responsibility(
    creation_db, client, monkeypatch
):
    db = creation_db
    row = await allocation(db)
    change = service._change

    async def lost(db, identifier, expected, **values):
        result = await change(db, identifier, expected, **values)
        if values.get("status") == "written":
            raise OSError("private commit acknowledgement")
        return result

    monkeypatch.setattr(service, "_change", lost)
    with pytest.raises(ContentError):
        await create(db, row)
    current = await service._read(db, row["id"])
    assert current["status"] == "unknown" and current["receipt"]
    assert current["operation_id"] in await operations(db)
    assert (row["bucket"], row["object_key"]) in client.objects


async def test_client_close_unknown_cannot_publish_verified(creation_db, client):
    db = creation_db
    row = await allocation(db)
    client.close_failure = True
    with pytest.raises(ContentError):
        await create(db, row)
    current = await service._read(db, row["id"])
    assert (
        current["status"] == "unknown"
        and current["receipt"]
        and current["verification"]
    )
    assert current["operation_id"] in await operations(db)


async def test_freeze_failure_aborts_exact_mpu_before_any_final_attempt(
    creation_db, client, monkeypatch
):
    db = creation_db
    row = await allocation(db)
    change = service._change

    async def failed(db, identifier, expected, **values):
        if values.get("status") == "prepared":
            raise OSError("private freeze failure")
        return await change(db, identifier, expected, **values)

    monkeypatch.setattr(service, "_change", failed)
    with pytest.raises(ContentError):
        await create(db, row)
    assert (await service._read(db, row["id"]))["status"] == "failed"
    assert [name for name, _ in client.calls].count("abort") == 1
    assert not client.uploads and not client.objects and not await operations(db)


async def test_repeated_cancellation_and_duplicate_delivery_cannot_abandon_or_repeat_write(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    entered, release = threading.Event(), threading.Event()

    def hook(operation, params):
        if operation == "part":
            entered.set()
            assert release.wait(5)

    client.hook = hook
    task = asyncio.create_task(create(db, row))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(ContentError, match="managed_original_creation_active"):
            await create(db, row)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await service._read(db, row["id"]))["status"] == "verified"
    assert len([call for call in client.calls if call[0] == "complete"]) == 1
    assert not await operations(db)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_bytes", 1000),
        ("content_type", "image/png"),
        ("object_key", "other"),
        ("origin", "connector"),
        ("configuration", {}),
    ],
)
async def test_untrusted_allocation_is_reloaded_and_rejected_before_io(
    creation_db, client, field, value
):
    row = await allocation(creation_db)
    with pytest.raises(ContentError):
        await create(creation_db, row | {field: value})
    assert not client.calls
    assert (await service._read(creation_db, row["id"]))["status"] == "allocated"


async def publication(db, row, result):
    item = DatasetItem(
        dataset_id=row["dataset_id"],
        file_name=row["logical_path"],
        file_path=row["object_key"],
        file_type="image",
        file_size=result.size_bytes,
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="image",
        checksum_algorithm="sha256",
        checksum_value=result.sha256,
        size_bytes=result.size_bytes,
        verification_state="verified",
    )
    db.add(asset)
    await db.flush()
    location = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=LEGACY_LOCATION_ID,
        bucket=row["bucket"],
        object_key=row["object_key"],
        role="managed_original",
        etag=result.etag,
    )
    db.add(location)
    await db.flush()
    return item, location


async def test_attach_is_transactional_and_preserves_source_provenance_on_bucket_remap(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    result = await create(db, row)
    item, location = await publication(db, row, result)
    await service.attach(db, item, row["id"])
    await db.commit()
    asset_id = location.asset_id
    await db.execute(
        update(DatasetAssetLocation)
        .where(DatasetAssetLocation.asset_id == asset_id)
        .values(bucket="offline-remapped-target")
    )
    await db.commit()
    source = await service._read(db, row["id"])
    assert source["bucket"] == row["bucket"] and source["receipt"]
    with pytest.raises(ContentError):
        await service.attach(db, item, row["id"])
    await db.rollback()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                update(DatasetAssetLocation)
                .where(DatasetAssetLocation.asset_id == asset_id)
                .values(creation_id=None)
            )


async def test_business_rollback_does_not_erase_verified_unlinked_creation(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    result = await create(db, row)
    item, _ = await publication(db, row, result)
    await service.attach(db, item, row["id"])
    identifier = item.id
    await db.rollback()
    assert await db.get(DatasetItem, identifier) is None
    assert (await service._read(db, row["id"]))["status"] == "verified"
    assert (row["bucket"], row["object_key"]) in client.objects


async def test_configuration_change_is_not_legacy_fallback_or_new_write(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(generation=2)
    )
    with pytest.raises(ContentError, match="managed_original_configuration_changed"):
        await service.for_key(
            db, dataset_id=row["dataset_id"], object_key=row["object_key"]
        )
    with pytest.raises(ContentError):
        await create(db, row)
    assert not client.calls


@pytest.mark.parametrize("legacy_snapshot", [False, True])
async def test_avatar_binding_generation_does_not_invalidate_original_creation(
    creation_db, client, monkeypatch, legacy_snapshot
):
    db = creation_db
    original_configuration = service._configuration
    if legacy_snapshot:
        # Allocate a real immutable ledger using the previously shipped snapshot
        # shape, rather than rewriting its facts after allocation.
        async def old_configuration(db, *, lock=False):
            configuration = await original_configuration(db, lock=lock)
            avatar_binding = await db.get(StorageBinding, "avatars")
            configuration["buckets"]["avatars"] = service.storage_service.avatars_bucket
            configuration["bindings"].append(
                service._json(
                    {
                        column.key: getattr(avatar_binding, column.key)
                        for column in StorageBinding.__table__.columns
                    }
                )
            )
            configuration["bindings"].sort(key=lambda binding: binding["purpose"])
            return configuration

        monkeypatch.setattr(service, "_configuration", old_configuration)
    row = await allocation(db)
    saved = deepcopy(row["configuration"])
    assert ("avatars" in saved["buckets"]) is legacy_snapshot
    monkeypatch.setattr(service, "_configuration", original_configuration)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "avatars")
        .values(generation=2)
    )
    await db.commit()
    result = await create(db, row)
    assert result.size_bytes == len(b"original")
    current = await service._read(db, row["id"])
    assert current["configuration"] == saved
    assert current["input_fingerprint"] == service._digest(saved)
    assert current["status"] == "verified"


async def test_saved_original_configuration_fingerprint_is_still_verified(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    # A caller-provided ledger copy must not be able to hide tampering in a
    # projected-away field; its original complete fingerprint is checked first.
    changed = deepcopy(row)
    changed["configuration"]["buckets"]["avatars"] = "forged-avatar-bucket"
    with pytest.raises(ContentError, match="managed_original_configuration_changed"):
        await service._validate(db, changed)
    assert not client.calls


@pytest.mark.parametrize("legacy_snapshot", [False, True])
@pytest.mark.parametrize("owner_kind", ["dataset", "task"])
async def test_report_attachment_switch_preserves_inflight_original_creation(
    creation_db, client, monkeypatch, legacy_snapshot, owner_kind
):
    db = creation_db
    configuration_for = service._configuration
    if legacy_snapshot:

        async def historical_configuration(db, *, lock=False):
            configuration = await configuration_for(db, lock=lock)
            bindings = list(await db.scalars(select(StorageBinding)))
            configuration["buckets"] = {
                purpose: getattr(service.storage_service, attr)
                for purpose, attr in service.PURPOSE_BUCKET_ATTRIBUTES.items()
            }
            configuration["bindings"] = sorted(
                [
                    service._json(
                        {
                            column.key: getattr(row, column.key)
                            for column in row.__table__.columns
                        }
                    )
                    for row in bindings
                ],
                key=lambda row: row["purpose"],
            )
            return configuration

        monkeypatch.setattr(service, "_configuration", historical_configuration)
    if owner_kind == "task":
        row = await service.allocate_task(
            db,
            project_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            logical_path="original.bin",
            max_bytes=10,
        )
        client.versioned = True
        client.store(
            row["staging_bucket"],
            row["staging_key"],
            b"original",
            content_type=row["content_type"],
        )
    else:
        row = await allocation(db)
    saved = deepcopy(row["configuration"])
    monkeypatch.setattr(service, "_configuration", configuration_for)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "bug-reports")
        .values(generation=2)
    )
    await db.commit()
    if owner_kind == "task":
        result = await service.complete_direct(db, row)
    else:
        result = await create(db, row)
    assert result.size_bytes == len(b"original")
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"
    current = await service._read(db, row["id"])
    assert current["configuration"] == saved
    assert current["input_fingerprint"] == service._digest(saved)
    assert current["status"] == "verified"


async def test_schema_guards_are_real_and_refuse_downgrade_with_allocated_fact(
    creation_db,
):
    db = creation_db
    row = await allocation(db)
    async with service.storage_access.engine(db).begin() as connection:
        with pytest.raises(RuntimeError, match="creation facts"):
            await connection.run_sync(lambda sync: migration(sync).downgrade())
    for values in (
        {"object_key": "changed"},
        {"max_bytes": 1000},
        {"status": "verified"},
    ):
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    update(Creation).where(Creation.id == row["id"]).values(**values)
                )
    async with service.storage_access.engine(db).connect() as connection:
        columns = await connection.run_sync(
            lambda sync: inspect(sync).get_foreign_keys("managed_original_creations")
        )
        assert columns == []


def test_operation_client_is_same_origin_nonretrying_and_uses_frozen_budget(
    monkeypatch,
):
    from types import SimpleNamespace

    calls = []
    handlers = []
    client = SimpleNamespace(
        meta=SimpleNamespace(
            events=SimpleNamespace(
                register=lambda event, handler: handlers.append((event, handler))
            )
        )
    )

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(service.boto3, "client", factory)
    assert service.create_client(timeout_seconds=640) is client
    configuration = calls[0][1]["config"]
    assert configuration.read_timeout == 640 and configuration.connect_timeout == 2
    assert configuration.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert configuration.proxies == {} and configuration.signature_version == "s3v4"
    event, guard = handlers[0]
    assert event == "before-send.s3"
    guard(SimpleNamespace(url=calls[0][1]["endpoint_url"] + "/bucket/key"))
    with pytest.raises(ContentError, match="managed_original_redirect_denied"):
        guard(SimpleNamespace(url="https://redirect.invalid/bucket/key"))
