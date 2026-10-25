"""New training facts are additive and cannot be rewritten by legacy restore."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import training_dataset_storage as storage
from tests.test_migration_0220_training_dataset_storage import create as dataset
from tests.test_migration_0221_training_run_storage import create as run
from tests.test_migration_0222_auxiliary_training_storage import (
    KINDS,
    create as auxiliary,
    owned_db as owned_db,
)
from tests.test_training_storage_targets import target


def migration(connection):
    path = (
        Path(__file__).parents[1] / "alembic/versions/0254_training_storage_targets.py"
    )
    spec = importlib.util.spec_from_file_location(
        "training_targets_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
async def address_db(owned_db):
    connection = await owned_db.connection()
    await connection.run_sync(lambda sync: migration(sync).upgrade())
    return owned_db


async def create(db, family):
    if family == "training-dataset":
        return await dataset(db)
    if family == "training-run":
        return await run(db)
    return await auxiliary(db, next(kind for kind in KINDS if kind[2] == family))


@pytest.mark.parametrize("family", list(storage._NAMESPACES) + ["deletion"])
@pytest.mark.parametrize("legacy,generation", [(False, 2), (True, 3)])
async def test_schema_two_retains_responsibility_and_blocks_restore_and_downgrade(
    address_db, family, legacy, generation
):
    db = address_db
    purpose = "training-dataset" if family == "deletion" else family
    row = await create(db, purpose)
    logical = storage._prefix_for(row.project_id, row.id, purpose=purpose)
    ref = storage.make_ref(target(legacy=legacy), generation, logical, purpose=purpose)
    if family == "deletion":
        row = TrainingStorageDeletion(
            bucket=ref["bucket"], prefix=ref["prefix"], storage_ref=ref
        )
        db.add(row)
    else:
        row.storage_ref = ref
    await db.flush()
    connection = await db.connection()
    async with connection.begin_nested() as nested:
        with pytest.raises(RuntimeError, match="prevent destructive"):
            await connection.run_sync(lambda sync: migration(sync).downgrade())
        await nested.rollback()
    await db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    with pytest.raises(IntegrityError, match="immutable"):
        async with db.begin_nested():
            await db.execute(
                update(type(row))
                .where(type(row).id == row.id)
                .values(
                    storage_ref={
                        **ref,
                        "bucket": "changed",
                        "storage_identity": "f" * 64,
                    }
                )
            )
    await db.refresh(row)
    assert row.storage_ref == ref


async def test_upgrade_and_downgrade_keep_old_null_and_evidence(address_db):
    db = address_db
    old = await dataset(db, pinned=True)
    unpinned = await run(db)
    before = deepcopy(old.storage_ref)
    connection = await db.connection()
    await connection.run_sync(lambda sync: migration(sync).downgrade())
    await connection.run_sync(lambda sync: migration(sync).upgrade())
    await db.refresh(old)
    await db.refresh(unpinned)
    assert old.storage_ref == before and unpinned.storage_ref is None


@pytest.mark.parametrize(
    "change",
    [
        {"secret": "forbidden"},
        {"key_prefix": "other/"},
        {"binding_generation": 0},
        {"prefix": "elsewhere/"},
    ],
)
async def test_database_rejects_forged_schema_two(address_db, change):
    row = await dataset(address_db)
    ref = storage.make_ref(target(), 2, storage.prefix_for(row.project_id, row.id))
    with pytest.raises(IntegrityError):
        async with address_db.begin_nested():
            await address_db.execute(
                update(type(row))
                .where(type(row).id == row.id)
                .values(storage_ref=ref | change)
            )
