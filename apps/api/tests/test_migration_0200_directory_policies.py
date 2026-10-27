"""Preserve historical reference mappings and refuse destructive rollback."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.migration_helpers import bind, historical_schema, load, relation
from tests.test_dataset_content import content_world as content_world
from app.services.dataset_references import register_entry


def module():
    spec = importlib.util.spec_from_file_location(
        "migration_0200",
        Path(__file__).parents[1] / "alembic/versions/0200_directory_policies.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_empty_policy_schema_roundtrip(test_engine):
    async with historical_schema(test_engine, "0200") as (schema, engine):
        async with engine.begin() as connection:

            def verify(sync):
                module = bind(load("0200_directory_policies.py"), sync)
                module.downgrade()
                assert relation(sync, schema, "dataset_source_items") is None
                module.upgrade()
                assert (
                    relation(sync, schema, "dataset_source_previews")
                    == "dataset_source_previews"
                )

            await connection.run_sync(verify)


@pytest.mark.parametrize(
    "change",
    [
        "ownership_mode='managed_copy'",
        "cache_policy='preview'",
        "policy_revision=2",
        "unregistered_at=now()",
    ],
)
async def test_source_policy_without_items_prevents_downgrade(
    db_session, content_world, monkeypatch, change
):
    source = content_world[5]
    await db_session.execute(
        text(f"UPDATE dataset_sources SET {change} WHERE id=CAST(:id AS uuid)"),
        {"id": str(source.id)},
    )
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        assert sync.scalar(text("SELECT count(*) FROM dataset_source_items")) == 0
        with pytest.raises(RuntimeError, match="Retain directory"):
            migration.downgrade()
        assert (
            sync.scalar(text("SELECT to_regclass('dataset_source_previews')"))
            is not None
        )

    await db_session.run_sync(verify)


async def test_existing_references_backfill_and_prevent_downgrade(
    db_session, content_world, monkeypatch
):
    _, _, actor, _, _, source, scan, entry = content_world
    result = await register_entry(
        db_session, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    source_id = str(source.id)
    migration = module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain directory"):
            migration.downgrade()
        # Explicitly discard only this disposable test's newer ledger to model 0199.
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
        # Later migrations are additive for these objects. Rolling the shared
        # schema back would be blocked by facts other tests legitimately retain,
        # so round-trip only this revision's own objects.
        sync.execute(text("DELETE FROM dataset_source_items"))
        migration.downgrade()
        migration.upgrade()
        row = sync.execute(
            text(
                "SELECT content_sha256,ownership_mode,owns_item FROM dataset_source_items WHERE source_id=CAST(:id AS uuid)"
            ),
            {"id": source_id},
        ).one()
        assert row == (result["sha256"], "reference", True)
        row = sync.execute(
            text(
                "SELECT ownership_mode,cache_policy,unregistered_at FROM dataset_sources WHERE id=CAST(:id AS uuid)"
            ),
            {"id": source_id},
        ).one()
        assert row == ("reference", "none", None)
        with pytest.raises(RuntimeError, match="Retain directory"):
            migration.downgrade()

    await db_session.run_sync(verify)
