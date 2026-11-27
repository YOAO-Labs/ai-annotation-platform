"""Revision 0230 adds task scope without changing historical dataset receipts."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import managed_original_creation as service
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_migration_0219_managed_original_creations import (
    CREATION_COLUMNS_0219,
    allocation_values,
    creation_table,
    historical_creation_database,
)


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
        await connection.execute(
            text("ALTER TABLE managed_original_creations ADD COLUMN staging_ref jsonb")
        )
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


def task_values():
    values = allocation_values()
    identifier, task_id = values["id"], uuid.uuid4()
    return values | {
        "owner_kind": "task",
        "dataset_id": None,
        "project_id": uuid.uuid4(),
        "task_id": task_id,
        "origin": "direct",
        "object_key": f"__aap_task_originals__/{task_id}/{identifier}/payload",
        "staging_bucket": "staging",
        "authority": {"source": "task"},
        "staging_key": f"__aap_upload_staging__/{identifier}/payload",
    }


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def task_history(test_db_url):
    async with historical_creation_database(test_db_url, "0230") as engine:
        async with engine.begin() as db:
            creations = await db.run_sync(creation_table)
            await db.execute(creations.insert(), allocation_values())
            result = await db.execute(
                creations.insert().returning(creations), task_values()
            )
            row = result.mappings().one()
        yield engine, creations, row


@pytest.fixture
async def scope_db(task_history):
    engine, creations, row = task_history
    async with engine.connect() as db:
        yield db, creations, row


async def test_dataset_history_is_unchanged_and_downgrade_preserves_it(test_db_url):
    async with (
        historical_creation_database(test_db_url, "0229") as engine,
        engine.begin() as connection,
    ):
        await connection.run_sync(lambda sync: migration(sync).upgrade())
        await connection.run_sync(lambda sync: migration(sync).downgrade())
        creations = await connection.run_sync(creation_table)
        await connection.execute(
            creations.insert().values(
                **allocation_values(),
                status="failed",
                completed_at=func.now(),
                error_code="old_failure",
            )
        )
        receipt = text("SELECT to_jsonb(c) FROM public.managed_original_creations c")
        before = (await connection.execute(receipt)).scalar_one()
        await connection.run_sync(lambda sync: migration(sync).upgrade())
        assert (await connection.execute(receipt)).scalar_one() == before | {
            "owner_kind": "dataset",
            "project_id": None,
            "task_id": None,
        }
        await connection.run_sync(lambda sync: migration(sync).downgrade())
        assert (await connection.execute(receipt)).scalar_one() == before
        await connection.run_sync(lambda sync: migration(sync).upgrade())


async def test_columns_constraints_and_history_guards_are_additive(scope_db):
    db, creations, _ = scope_db
    assert set(creations.c.keys()) == CREATION_COLUMNS_0219 | {
        "owner_kind",
        "project_id",
        "task_id",
    }

    def check(sync):
        assert not inspect(sync).get_foreign_keys(
            "managed_original_creations", schema="public"
        )
        triggers = sync.scalars(
            text("""
                SELECT tgname FROM pg_trigger
                WHERE tgrelid='public.managed_original_creations'::regclass AND NOT tgisinternal
            """)
        )
        assert set(triggers) == {
            "trg_managed_original_creation_history",
            "trg_managed_original_creation_scope",
        }
        assert migration(sync).down_revision == "0229"

    await db.run_sync(check)


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
async def test_task_scope_rejects_invalid_insert_facts(scope_db, changes):
    db, creations, _ = scope_db
    violation = (
        "not-null constraint"
        if changes == {"owner_kind": None}
        else "ck_managed_creation_scope"
    )
    with pytest.raises(IntegrityError, match=violation):
        async with db.begin_nested():
            await db.execute(creations.insert(), task_values() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"dataset_id": None},
        {"project_id": uuid.uuid4()},
        {"task_id": uuid.uuid4()},
        {"owner_kind": "task"},
    ],
)
async def test_dataset_scope_still_requires_only_dataset(scope_db, changes):
    db, creations, _ = scope_db
    owner_kind = await db.scalar(
        creations.insert().returning(creations.c.owner_kind), allocation_values()
    )
    assert owner_kind == "dataset"
    with pytest.raises(IntegrityError, match="ck_managed_creation_scope"):
        async with db.begin_nested():
            await db.execute(creations.insert(), allocation_values() | changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_kind", "dataset"),
        ("project_id", uuid.uuid4()),
        ("task_id", uuid.uuid4()),
        ("dataset_id", uuid.uuid4()),
    ],
)
async def test_scope_is_immutable_from_initial_allocation(scope_db, field, value):
    db, creations, row = scope_db
    target = creations.c.id == row["id"]
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(creations.update().where(target).values({field: value}))
    assert (await db.execute(select(creations).where(target))).mappings().one() == row


async def test_task_history_prevents_delete_or_destructive_downgrade(task_history):
    engine, creations, row = task_history
    target = creations.c.id == row["id"]
    history = select(creations).order_by(creations.c.id)
    async with engine.connect() as db:
        before = (await db.execute(history)).mappings().all()
        with pytest.raises(IntegrityError, match="history is immutable"):
            await db.execute(creations.delete().where(target))
    async with engine.begin() as db:
        with pytest.raises(RuntimeError, match="Task creation facts"):
            await db.run_sync(lambda sync: migration(sync).downgrade())
    async with engine.connect() as db:
        assert (await db.execute(history)).mappings().all() == before
