import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from app.services.dataset_registration import start_preflight
from tests.migration_helpers import bind, historical_schema, load, relation
from tests.test_dataset_content import content_world as content_world


def module():
    spec = importlib.util.spec_from_file_location(
        "migration_0201",
        Path(__file__).parents[1] / "alembic/versions/0201_dataset_source_changes.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_empty_comparison_schema_roundtrip(test_engine):
    async with historical_schema(test_engine, "0201") as (schema, engine):
        async with engine.begin() as connection:

            def verify(sync):
                module = bind(load("0201_dataset_source_changes.py"), sync)
                module.downgrade()
                assert relation(sync, schema, "dataset_source_changes") is None
                module.upgrade()
                assert (
                    relation(sync, schema, "dataset_source_changes")
                    == "dataset_source_changes"
                )

            await connection.run_sync(verify)


async def test_comparison_evidence_blocks_destructive_rollback(
    db_session, content_world, monkeypatch
):
    await start_preflight(db_session, content_world[6].id, content_world[2])
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain source comparison evidence"):
            migration.downgrade()
        assert sync.scalar(text("SELECT count(*) FROM dataset_source_changes")) == 1

    await db_session.run_sync(verify)
