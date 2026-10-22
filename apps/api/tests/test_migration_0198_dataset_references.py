import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from tests.test_dataset_references import reference_world as reference_world
from tests.test_dataset_content import content_world as content_world


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0198",
        Path(__file__).parents[1] / "alembic/versions/0198_dataset_references.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_unused_reference_schema_roundtrip(db_session, monkeypatch):
    migration = migration_module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        assert sync.scalar(text("SELECT to_regclass('dataset_references')")) is None
        migration.upgrade()
        assert sync.scalar(text("SELECT to_regclass('dataset_references')")) is not None

    await db_session.run_sync(verify)


async def test_registered_reference_schema_refuses_destructive_downgrade(
    db_session, monkeypatch, reference_world
):
    migration = migration_module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain registered reference"):
            migration.downgrade()
        assert sync.scalar(text("SELECT count(*) FROM dataset_references")) == 1

    await db_session.run_sync(verify)
