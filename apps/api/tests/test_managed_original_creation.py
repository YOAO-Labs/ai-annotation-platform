"""Real independent ledger commits live only inside each test's owned schema."""

import asyncio
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import threading
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

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
from tests.managed_original_client_support import Client, stage_client


def migration(connection, filename="0219_managed_original_creations.py"):
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "creation_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


_CREATION_TABLES = (
    "storage_connections",
    "storage_locations",
    "storage_bindings",
    "storage_maintenance_gates",
    "storage_operations",
    "storage_usage_mutations",
    "dataset_items",
    "dataset_assets",
    "dataset_asset_locations",
    "managed_original_retirements",
    "managed_original_creations",
    "managed_original_reclaim_claims",
    "dataset_auxiliary_originals",
    "scenes",
)
_CREATION_TRIGGERS = {
    ("managed_original_creations", "trg_managed_original_creation_history"),
    ("managed_original_creations", "trg_managed_original_creation_scope"),
    ("dataset_asset_locations", "trg_managed_original_creation_attachment"),
    ("managed_original_retirements", "trg_managed_original_retirement_creation"),
    ("dataset_asset_locations", "trg_dataset_asset_locations_storage_ref"),
    ("dataset_auxiliary_originals", "trg_dataset_auxiliary_originals_storage_ref"),
    ("dataset_auxiliary_originals", "trg_auxiliary_original"),
    ("dataset_auxiliary_originals", "trg_auxiliary_original_retirement"),
    ("dataset_items", "trg_auxiliary_raw_pointer"),
    ("scenes", "trg_auxiliary_map_pointer"),
    ("dataset_asset_locations", "trg_auxiliary_creation_asset"),
}


async def _clone_creation_schema(connection, schema):
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    for table in _CREATION_TABLES:
        await connection.execute(
            text(
                f'CREATE TABLE "{schema}"."{table}" '
                f'(LIKE public."{table}" INCLUDING ALL)'
            )
        )
    await connection.execute(
        text(
            f'CREATE TABLE "{schema}".auxiliary_retirement_guard '
            "(LIKE yoao_fixture_v1.auxiliary_retirement_guard INCLUDING ALL)"
        )
    )
    await connection.execute(text(f'SET LOCAL search_path = "{schema}", pg_catalog'))
    triggers = list(
        await connection.execute(
            text(
                "SELECT relation.relname, trigger.tgname, "
                "function_namespace.nspname || '.' || function.proname, "
                "pg_catalog.pg_get_triggerdef(trigger.oid), "
                "pg_catalog.pg_get_functiondef(function.oid) "
                "FROM pg_catalog.pg_trigger AS trigger "
                "JOIN pg_catalog.pg_class AS relation ON relation.oid=trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid=relation.relnamespace "
                "JOIN pg_catalog.pg_proc AS function ON function.oid=trigger.tgfoid "
                "JOIN pg_catalog.pg_namespace AS function_namespace "
                "ON function_namespace.oid=function.pronamespace "
                "WHERE NOT trigger.tgisinternal AND namespace.nspname='public' "
                "AND function_namespace.nspname IN ('public', 'yoao_fixture_v1') "
                "AND trigger.tgname = ANY(CAST(:names AS text[])) "
                "ORDER BY relation.relname, trigger.tgname"
            ),
            {"names": sorted(name for _, name in _CREATION_TRIGGERS)},
        )
    )
    assert {(table, name) for table, name, *_ in triggers} == _CREATION_TRIGGERS
    functions = {name: definition for _, _, name, _, definition in triggers}
    objects = {
        **{f"public.{name}": name for name in _CREATION_TABLES},
        "yoao_fixture_v1.auxiliary_retirement_guard": "auxiliary_retirement_guard",
        **{name: name.rsplit(".", 1)[1] for name in functions},
    }
    for definition in functions.values():
        for source, name in objects.items():
            definition = definition.replace(source, f'"{schema}".{name}')
        await connection.exec_driver_sql(definition)
    for _, _, _, definition, _ in triggers:
        for source, name in objects.items():
            definition = definition.replace(source, f'"{schema}".{name}')
        await connection.exec_driver_sql(definition)
    foreign_keys = await connection.execute(
        text(
            "SELECT child.relname, constraint_row.conname, "
            "pg_catalog.pg_get_constraintdef(constraint_row.oid) "
            "FROM pg_catalog.pg_constraint AS constraint_row "
            "JOIN pg_catalog.pg_class AS child ON child.oid=constraint_row.conrelid "
            "JOIN pg_catalog.pg_class AS parent ON parent.oid=constraint_row.confrelid "
            "JOIN pg_catalog.pg_namespace AS child_namespace "
            "ON child_namespace.oid=child.relnamespace "
            "JOIN pg_catalog.pg_namespace AS parent_namespace "
            "ON parent_namespace.oid=parent.relnamespace "
            "WHERE constraint_row.contype='f' AND constraint_row.conparentid=0 "
            "AND child_namespace.nspname='public' "
            "AND parent_namespace.nspname='public' "
            "AND parent.relname IN ('managed_original_creations','dataset_auxiliary_originals') "
            "AND child.relname = ANY(CAST(:tables AS text[]))"
        ),
        {"tables": list(_CREATION_TABLES)},
    )
    for table, name, definition in foreign_keys:
        for parent in ("managed_original_creations", "dataset_auxiliary_originals"):
            definition = definition.replace(
                f"REFERENCES public.{parent}", f'REFERENCES "{schema}".{parent}'
            )
        await connection.exec_driver_sql(
            f'ALTER TABLE "{schema}"."{table}" ADD CONSTRAINT "{name}" {definition}'
        )


@pytest.fixture
async def creation_db(managed_database):
    from app.db.models.dataset import Dataset
    from app.db.models.user import User

    async with managed_database() as engine:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            owner = User(
                id=uuid.uuid4(),
                name="Creation fixture owner",
                email=f"creation-{uuid.uuid4().hex}@example.test",
                password_hash="unused",
                role="project_admin",
                is_active=True,
            )
            db.add(owner)
            await db.flush()
            dataset = Dataset(
                id=uuid.uuid4(),
                display_id=f"cr-{uuid.uuid4().hex[:12]}",
                name="Creation fixture dataset",
                created_by=owner.id,
            )
            db.add(dataset)
            await ensure_legacy_bindings(db)
            await db.commit()
            db.info["dataset_id"] = dataset.id
            yield db


@pytest.fixture
def client(monkeypatch):
    value = Client()
    monkeypatch.setattr(service, "create_client", lambda **_: value)
    stage_client(monkeypatch, value)
    return value


async def allocation(db, **fields):
    return await service.allocate(
        db,
        **(
            {
                "dataset_id": db.info.get("dataset_id") or uuid.uuid4(),
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
        dataset_id=db.info["dataset_id"],
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
    with pytest.raises(ExceptionGroup) as error:
        await create(db, row)
    assert error.value.storage_operation_unsettled
    assert any(isinstance(item, OSError) for item in error.value.exceptions)
    current = await service._read(db, row["id"])
    assert (
        current["status"] == "unknown"
        and current["receipt"]
        and current["verification"]
    )
    assert current["operation_id"] in await operations(db)


async def test_creation_and_close_failures_keep_first_error_and_anchor(
    creation_db, client, monkeypatch
):
    row = await allocation(creation_db)
    change = service._change
    first = OSError("freeze failed")

    async def failed(db, identifier, expected, **values):
        if values.get("status") == "prepared":
            raise first
        return await change(db, identifier, expected, **values)

    monkeypatch.setattr(service, "_change", failed)
    client.close_failure = True
    with pytest.raises(ExceptionGroup) as error:
        await create(creation_db, row)
    assert error.value.exceptions[0] is first
    assert error.value.storage_operation_unsettled
    current = await service._read(creation_db, row["id"])
    assert current["status"] == "unknown"
    assert current["operation_id"] in await operations(creation_db)


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


async def test_cancelled_creation_reports_late_physical_failure(creation_db, client):
    row = await allocation(creation_db)
    entered, release = threading.Event(), threading.Event()

    def hook(operation, params):
        if operation == "part":
            entered.set()
            assert release.wait(5)
            raise OSError("write failed after cancellation")

    client.hook = hook
    task = asyncio.create_task(create(creation_db, row))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(BaseExceptionGroup) as error:
        await task
    assert isinstance(error.value.exceptions[0], asyncio.CancelledError)
    assert isinstance(error.value.exceptions[1], ContentError)
    assert error.value.exceptions[1].__cause__ is not None
    assert (row["bucket"], row["object_key"]) not in client.objects


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
        storage_ref=service.original_ref(row),
    )
    db.add(location)
    await db.flush()
    return item, location


async def test_attach_is_transactional_and_preserves_source_provenance_on_bucket_remap(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    row = deepcopy(row)
    row["id"] = uuid.uuid4()
    row["object_key"] += "-historical"
    row["configuration"].pop("original_ref")
    row["configuration"]["schema"] = 1
    row["input_fingerprint"] = service._digest(row["configuration"])
    await db.execute(Creation.__table__.insert().values(**row))
    await db.commit()
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


async def test_binding_generation_change_preserves_saved_original_allocation(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    await db.execute(
        update(StorageBinding)
        .where(StorageBinding.purpose == "datasets")
        .values(generation=2)
    )
    assert await service.for_key(
        db, dataset_id=row["dataset_id"], object_key=row["object_key"]
    )
    created = await create(db, row)
    assert created.size_bytes == len(b"original")
    assert client.objects[row["bucket"], row["object_key"]]["data"] == b"original"


@pytest.mark.parametrize("legacy_snapshot", [False, True])
async def test_avatar_binding_generation_does_not_invalidate_original_creation(
    creation_db, client, monkeypatch, legacy_snapshot
):
    db = creation_db
    original_configuration = service._configuration
    if legacy_snapshot:

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


async def test_current_schema_guards_preserve_immutable_allocated_fact(creation_db):
    db = creation_db
    row = await allocation(db)
    for values in (
        {"object_key": "changed"},
        {"max_bytes": 1000},
        {"status": "verified"},
        {"staging_ref": {}},
        {
            "failure_evidence": {
                "physical_settled": True,
                "physical_outcome": "not_written",
            }
        },
    ):
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    update(Creation).where(Creation.id == row["id"]).values(**values)
                )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(delete(Creation).where(Creation.id == row["id"]))
    await db.rollback()
    assert await service._read(db, row["id"]) == row
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

    monkeypatch.setattr("boto3.client", factory)
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


async def test_observed_receipt_survives_failed_independent_persistence(
    creation_db, client
):
    db = creation_db
    row = await allocation(db)
    client.versioned = True
    async with service.storage_access.engine(db).begin() as connection:
        await connection.exec_driver_sql("""
            CREATE FUNCTION reject_test_creation_receipt() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'receipt storage unavailable' USING ERRCODE='23514';
            END
            $$;
        """)
        await connection.exec_driver_sql("""
            CREATE TRIGGER test_creation_receipt_persistence
            BEFORE UPDATE ON managed_original_creations
            FOR EACH ROW WHEN (NEW.status='written')
            EXECUTE FUNCTION reject_test_creation_receipt()
        """)
    with pytest.raises(
        ContentError, match="managed_original_receipt_persistence_unknown"
    ):
        await create(db, row)

    await db.rollback()
    current = await service._read(db, row["id"])
    assert current["status"] == "unknown" and current["receipt"] is None
    assert current["error_code"] == "managed_original_receipt_persistence_unknown"
    assert current["acknowledged_at"] is None and current["settled_at"] is None
    assert current["verification"] is None
    proof = current["failure_evidence"]
    assert proof["physical_settled"] is False
    assert proof["physical_outcome"] == "unknown"
    physical = client.objects[row["bucket"], row["object_key"]]
    assert physical["data"] == b"original"
    assert proof["receipt"] == {
        "etag": physical["ETag"],
        "version_id": physical["VersionId"],
        "request_id": "request",
        "response": {
            "ETag": physical["ETag"],
            "VersionId": physical["VersionId"],
            "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "request"},
        },
    }
    assert current["operation_id"] in await operations(db)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                update(Creation)
                .where(Creation.id == row["id"])
                .values(failure_evidence={**proof, "physical_settled": True})
            )
    await db.rollback()
    assert await service._read(db, row["id"]) == current
    assert client.closed == 1
    assert len([call for call in client.calls if call[0] == "complete"]) == 1
    with pytest.raises(ContentError, match="managed_original_creation_terminal"):
        await create(db, row)
    assert len([call for call in client.calls if call[0] == "complete"]) == 1
