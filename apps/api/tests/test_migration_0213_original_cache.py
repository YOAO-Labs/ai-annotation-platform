"""Reversible when unused; never discard outstanding copies or policy choices."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from tests.test_dataset_content import content_world as content_world
from tests.test_original_cache import cache_world as cache_world, fill, record


def migration_module():
    spec = importlib.util.spec_from_file_location(
        "cache_migration",
        Path(__file__).parents[1] / "alembic/versions/0213_original_cache_ownership.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


async def test_unused_schema_roundtrip(db_session, monkeypatch):
    migration = migration_module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        assert sync.scalar(text("SELECT to_regclass('original_cache_entries')")) is None
        migration.upgrade()
        assert (
            sync.scalar(text("SELECT to_regclass('original_cache_entries')"))
            is not None
        )

    await db_session.run_sync(verify)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE dataset_sources SET cache_policy='on_demand' WHERE id=:identity",
        "UPDATE storage_locations SET config=jsonb_build_object('cache_policy','on_demand') WHERE id=:location",
    ],
)
async def test_reserved_policy_prevents_destructive_downgrade(
    db_session, content_world, monkeypatch, statement
):
    migration = migration_module()
    source, location = content_world[5], content_world[4]
    await db_session.execute(
        text(statement), {"identity": source.id, "location": location.id}
    )

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Cannot discard original cache"):
            migration.downgrade()

    await db_session.run_sync(verify)


@pytest.mark.parametrize("owner", ["entry", "locator"])
async def test_outstanding_cache_ownership_prevents_downgrade(
    cache_world, monkeypatch, owner
):
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.dataset_source import DatasetSource
    from app.db.models.original_cache import OriginalCacheEntry

    world = cache_world
    assert await fill(world) == "stored"
    owned = await record(world)
    async with world.factory() as db:
        source = await db.get(DatasetSource, world.source_id)
        source.cache_policy = "none"
        location = await db.get(StorageLocation, world.location_id)
        location.config = {"cache_policy": "preview"}
        if owner == "locator":
            await db.delete(await db.get(OriginalCacheEntry, owned.id))
        await db.commit()
    migration = migration_module()

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Cannot discard original cache"):
            migration.downgrade()

    async with world.factory() as db:
        await db.run_sync(verify)
