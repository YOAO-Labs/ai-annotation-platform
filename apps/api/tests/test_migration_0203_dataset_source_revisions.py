"""Upgrade real existing path heads without fabricating acceptance provenance."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

from app.services.dataset_references import register_entry
from tests.test_dataset_content import content_world as content_world
from tests.migration_helpers import bind, historical_schema, load, relation


def module():
    path = (
        Path(__file__).parents[1] / "alembic/versions/0203_dataset_source_revisions.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0203", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_empty_revision_schema_roundtrip(test_engine):
    async with historical_schema(test_engine, "0203") as (schema, engine):
        async with engine.begin() as connection:

            def verify(sync):
                module = bind(load("0203_dataset_source_revisions.py"), sync)
                module.downgrade()
                assert relation(sync, schema, "dataset_source_revisions") is None
                module.upgrade()
                assert (
                    relation(sync, schema, "dataset_source_revisions")
                    == "dataset_source_revisions"
                )

            await connection.run_sync(verify)


async def test_existing_heads_backfill_and_populated_rollback_refused(
    db_session, content_world, monkeypatch
):
    db, world = db_session, content_world
    await register_entry(db, world[6].id, world[7].path_digest, world[2])
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        # Later migrations are additive for these objects. Rolling the shared
        # schema back would be blocked by facts other tests legitimately retain,
        # so round-trip only this revision's own objects.
        with pytest.raises(RuntimeError, match="Retain accepted source revisions"):
            migration.downgrade()
        before = sync.execute(
            text(
                "SELECT source_id,path_digest,run_id,dataset_item_id,asset_id,content_sha256 FROM dataset_source_items"
            )
        ).one()
        # Simulate 0202 only inside the verified disposable SAVEPOINT test database.
        sync.execute(
            text("ALTER TABLE dataset_source_items DISABLE TRIGGER source_head_guard")
        )
        sync.execute(
            text(
                "ALTER TABLE dataset_source_items ALTER COLUMN current_revision_id DROP NOT NULL"
            )
        )
        sync.execute(text("UPDATE dataset_source_items SET current_revision_id=NULL"))
        sync.execute(text("DELETE FROM dataset_source_revisions"))
        sync.execute(
            text("ALTER TABLE dataset_source_items ENABLE TRIGGER source_head_guard")
        )
        migration.downgrade()
        migration.upgrade()
        after = sync.execute(
            text(
                "SELECT source_id,path_digest,run_id,dataset_item_id,asset_id,content_sha256 FROM dataset_source_items"
            )
        ).one()
        assert before == after
        row = sync.execute(
            text("""SELECT r.revision_number,r.item_id_at_accept,r.asset_id_at_accept,
              r.accepted_at,r.accepted_by_id,r.previous_revision_id
              FROM dataset_source_revisions r JOIN dataset_source_items h ON h.current_revision_id=r.id""")
        ).one()
        assert row == (1, before.dataset_item_id, before.asset_id, None, None, None)
        with pytest.raises(RuntimeError, match="Retain accepted source revisions"):
            migration.downgrade()

    await db.run_sync(verify)
