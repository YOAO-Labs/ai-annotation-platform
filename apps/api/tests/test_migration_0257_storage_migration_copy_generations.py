"""Incremental ancestry is additive, unique and retained during app rollback."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect
from sqlalchemy.exc import IntegrityError

from app.db.models.storage_migration_copy import StorageMigrationCopy as Copy
from app.services import storage_migration_copies as service
from tests.test_storage_migration_copies import seed


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0257_storage_migration_copy_generations.py"
    )
    spec = importlib.util.spec_from_file_location(
        "copy_generation_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_0257_root_roundtrip_retains_existing_plan(db_session, super_admin):
    _, _, _, body = await seed(db_session, super_admin[0])
    row, _, _ = await service.start(db_session, super_admin[0], body)
    connection = await db_session.connection()
    await connection.run_sync(lambda sync: migration(sync).downgrade())
    await connection.run_sync(lambda sync: migration(sync).upgrade())
    await db_session.refresh(row)
    assert (
        row.generation == 1 and row.parent_copy_id is row.parent_evidence_sha256 is None
    )
    foreign_keys = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("storage_migration_copies")
    )
    parent_fk = next(
        fk for fk in foreign_keys if fk["name"] == "fk_migration_copy_parent"
    )
    assert parent_fk["referred_table"] == "storage_migration_copies"
    assert parent_fk["options"]["ondelete"] == "RESTRICT"


async def test_0257_lineage_restricts_forks_deletion_and_destructive_downgrade(
    db_session, super_admin
):
    _, _, _, body = await seed(db_session, super_admin[0])
    parent, _, _ = await service.start(db_session, super_admin[0], body)

    def child(**changes):
        return Copy(
            **{
                "id": uuid.uuid4(),
                "parent_copy_id": parent.id,
                "generation": 2,
                "parent_evidence_sha256": "a" * 64,
                "input_fingerprint": uuid.uuid4().hex * 2,
                "source_report": b"x",
                "source_report_sha256": "b" * 64,
                "source_complete": False,
                "source": parent.source,
                "target_location_id": parent.target_location_id,
                "target": parent.target,
                "handling": parent.handling,
                "limits": parent.limits,
                **changes,
            }
        )

    for changes in (
        {"parent_evidence_sha256": None},
        {"generation": 1},
        {"parent_evidence_sha256": "BAD"},
    ):
        with pytest.raises(IntegrityError):
            async with db_session.begin_nested():
                db_session.add(child(**changes))
                await db_session.flush()
    db_session.add(child())
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(child())
            await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Copy).where(Copy.id == parent.id))
    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="retain their ancestry"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
    columns = await connection.run_sync(
        lambda sync: inspect(sync).get_columns("storage_migration_copies")
    )
    assert "parent_copy_id" in {c["name"] for c in columns}
