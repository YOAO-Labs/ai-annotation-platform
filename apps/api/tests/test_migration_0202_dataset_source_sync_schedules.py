import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from app.schemas.dataset_source import SourceSyncScheduleUpdate
from app.services.dataset_source_sync import configure
from tests.migration_helpers import bind, historical_schema, load, relation
from tests.test_dataset_content import content_world as content_world


def module():
    spec = importlib.util.spec_from_file_location(
        "migration_0202",
        Path(__file__).parents[1]
        / "alembic/versions/0202_dataset_source_sync_schedules.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_empty_schedule_schema_roundtrip(test_engine):
    async with historical_schema(test_engine, "0202") as (schema, engine):
        async with engine.begin() as connection:

            def verify(sync):
                module = bind(load("0202_dataset_source_sync_schedules.py"), sync)
                module.downgrade()
                assert relation(sync, schema, "dataset_source_sync_schedules") is None
                module.upgrade()
                assert (
                    relation(sync, schema, "dataset_source_sync_schedules")
                    == "dataset_source_sync_schedules"
                )

            await connection.run_sync(verify)


async def test_schedule_history_blocks_destructive_rollback(
    db_session, content_world, monkeypatch
):
    await configure(
        db_session,
        content_world[5].id,
        SourceSyncScheduleUpdate(expected_revision=0, enabled=True),
        content_world[2],
    )
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain periodic source schedules"):
            migration.downgrade()
        assert (
            sync.scalar(text("SELECT count(*) FROM dataset_source_sync_schedules")) == 1
        )

    await db_session.run_sync(verify)
