import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.migration_helpers import without_later_migrations
from sqlalchemy import text
import pytest

from tests.test_dataset_content import content_world as content_world
from app.services.dataset_registration import start_preflight


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0199",
        Path(__file__).parents[1]
        / "alembic/versions/0199_dataset_registration_jobs.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_empty_registration_schema_roundtrip(db_session, monkeypatch):
    migration = migration_module()

    def roundtrip(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with without_later_migrations(sync, monkeypatch, "0199"):
            migration.downgrade()
            assert (
                sync.scalar(text("SELECT to_regclass('dataset_registration_runs')"))
                is None
            )
            migration.upgrade()
            assert (
                sync.scalar(text("SELECT to_regclass('dataset_registration_entries')"))
                is not None
            )

    await db_session.run_sync(roundtrip)


async def test_registration_checkpoint_refuses_destructive_downgrade(
    db_session, content_world, monkeypatch
):
    await start_preflight(db_session, content_world[6].id, content_world[2])
    migration = migration_module()

    def reject(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain registration checkpoints"):
            migration.downgrade()

    await db_session.run_sync(reject)
