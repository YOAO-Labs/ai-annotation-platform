"""Scope migration contracts in a disposable, independently committed schema."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, insert, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.managed_original_creation import ManagedOriginalCreation as Creation
from app.services import managed_original_creation as service
from app.services.dataset_assets import ensure_legacy_bindings


def migration(connection, revision="0230"):
    filename = {
        "0219": "0219_managed_original_creations.py",
        "0230": "0230_managed_original_task_scope.py",
    }[revision]
    path = Path(__file__).parents[1] / "alembic/versions" / filename
    spec = importlib.util.spec_from_file_location(
        "task_creation_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def legacy_creation_db(test_engine):
    schema = "task_creation_test_" + uuid.uuid4().hex
    tables = (
        "storage_locations",
        "storage_bindings",
        "storage_maintenance_gates",
        "storage_operations",
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
                text(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN creation_id')
            )
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: migration(sync, "0219").upgrade())
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await ensure_legacy_bindings(db)
            await db.commit()
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture
async def task_creation_db(legacy_creation_db):
    db = legacy_creation_db
    async with service.storage_access.engine(db).begin() as connection:
        await connection.run_sync(lambda sync: migration(sync).upgrade())
    return db


async def allocation(db, **fields):
    return await service.allocate_task(
        db,
        **(
            {
                "project_id": uuid.uuid4(),
                "task_id": uuid.uuid4(),
                "logical_path": "folder/original.bin",
                "max_bytes": 1024,
                "requested_by": uuid.uuid4(),
                "authority": {"source": "test"},
            }
            | fields
        ),
    )


async def test_dataset_history_is_unchanged_and_downgrade_preserves_it(
    legacy_creation_db,
):
    engine = service.storage_access.engine(legacy_creation_db)
    identifier, dataset_id = uuid.uuid4(), uuid.uuid4()
    # This is the exact old INSERT shape, including a terminal fact. Constant
    # column defaults must not UPDATE it through the terminal-history guard.
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO managed_original_creations
                    (id,dataset_id,location_id,storage_identity,bucket,object_key,
                     logical_path,origin,max_bytes,timeout_seconds,content_type,
                     authority,configuration,input_fingerprint,maintenance_revision,
                     status,completed_at,error_code)
                VALUES (:id,:dataset,:location,:identity,'historical','old/key',
                    'old.bin','zip',10,60,'application/octet-stream',
                    '{}'::jsonb,'{}'::jsonb,:identity,1,'failed',now(),'old_failure')
            """),
            {
                "id": identifier,
                "dataset": dataset_id,
                "location": uuid.uuid4(),
                "identity": "a" * 64,
            },
        )
        before = (
            (await connection.execute(text("SELECT * FROM managed_original_creations")))
            .mappings()
            .one()
        )
        await connection.run_sync(lambda sync: migration(sync).upgrade())
        after = (
            (await connection.execute(text("SELECT * FROM managed_original_creations")))
            .mappings()
            .one()
        )
        assert dict(before) == {key: after[key] for key in before}
        assert after["owner_kind"] == "dataset"
        assert after["project_id"] is after["task_id"] is None
        await connection.run_sync(lambda sync: migration(sync).downgrade())
        restored = (
            (await connection.execute(text("SELECT * FROM managed_original_creations")))
            .mappings()
            .one()
        )
        assert dict(restored) == dict(before)
        await connection.run_sync(lambda sync: migration(sync).upgrade())


async def test_columns_constraints_and_history_guards_are_additive(task_creation_db):
    engine = service.storage_access.engine(task_creation_db)
    async with engine.connect() as connection:

        def check(sync):
            schema = sync.scalar(text("SELECT current_schema()"))
            inspector = inspect(sync)
            assert {
                value["name"]
                for value in inspector.get_columns(
                    "managed_original_creations", schema=schema
                )
            } == set(Creation.__table__.columns.keys())
            assert not inspector.get_foreign_keys(
                "managed_original_creations", schema=schema
            )
            triggers = set(
                sync.scalars(
                    text("""
                SELECT tgname FROM pg_trigger
                WHERE tgrelid='managed_original_creations'::regclass AND NOT tgisinternal
            """)
                )
            )
            assert triggers == {
                "trg_managed_original_creation_history",
                "trg_managed_original_creation_scope",
            }
            assert migration(sync).down_revision == "0229"

        await connection.run_sync(check)


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_kind": "unknown"},
        {"owner_kind": None},
        {"owner_kind": "dataset"},
        {"dataset_id": uuid.uuid4()},
        {"project_id": None},
        {"task_id": None},
        {"origin": "connector", "staging_bucket": None, "staging_key": None},
        {"object_key": "another/key"},
        {"object_key": "__aap_task_originals__/wrong/task/payload"},
    ],
)
async def test_task_scope_rejects_invalid_insert_facts(task_creation_db, changes):
    db = task_creation_db
    values = await allocation(db)
    identifier = uuid.uuid4()
    values.update(
        id=identifier,
        object_key=f"{service.TASK_ORIGINAL_PREFIX}/{values['task_id']}/{identifier}/payload",
        staging_key=f"{service.STAGING_PREFIX}/{identifier}/payload",
    )
    values.update(changes)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(insert(Creation).values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_id": None},
        {"project_id": uuid.uuid4()},
        {"task_id": uuid.uuid4()},
        {"owner_kind": "task"},
    ],
)
async def test_dataset_scope_still_requires_only_dataset(task_creation_db, changes):
    db = task_creation_db
    values = await service.allocate(
        db, dataset_id=uuid.uuid4(), logical_path="a.bin", origin="zip", max_bytes=10
    )
    values.update(id=uuid.uuid4(), object_key="another/" + uuid.uuid4().hex)
    values.update(changes)
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(insert(Creation).values(**values))


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_kind", "dataset"),
        ("project_id", uuid.uuid4()),
        ("task_id", uuid.uuid4()),
        ("dataset_id", uuid.uuid4()),
    ],
)
async def test_scope_is_immutable_from_initial_allocation(
    task_creation_db, field, value
):
    db = task_creation_db
    row = await allocation(db)
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(Creation).where(Creation.id == row["id"]).values({field: value})
            )


async def test_task_history_prevents_delete_or_destructive_downgrade(task_creation_db):
    db = task_creation_db
    row = await allocation(db)
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db.begin_nested():
            await db.execute(delete(Creation).where(Creation.id == row["id"]))
    await db.rollback()
    async with service.storage_access.engine(db).begin() as connection:
        with pytest.raises(RuntimeError, match="Task creation facts"):
            await connection.run_sync(lambda sync: migration(sync).downgrade())
    assert (await service._read(db, row["id"]))["owner_kind"] == "task"
