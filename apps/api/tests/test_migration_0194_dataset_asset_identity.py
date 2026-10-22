import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

spec = importlib.util.spec_from_file_location(
    "migration_0194",
    Path(__file__).parents[1] / "alembic/versions/0194_dataset_asset_identity.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.mark.asyncio
async def test_unused_identity_schema_roundtrip(test_engine, monkeypatch):
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:

            def roundtrip(sync):
                child_spec = importlib.util.spec_from_file_location(
                    "migration_0195",
                    Path(__file__).parents[1]
                    / "alembic/versions/0195_controlled_storage_locations.py",
                )
                child = importlib.util.module_from_spec(child_spec)
                child_spec.loader.exec_module(child)
                monkeypatch.setattr(
                    child, "op", Operations(MigrationContext.configure(sync))
                )
                scan_spec = importlib.util.spec_from_file_location(
                    "migration_0196",
                    Path(__file__).parents[1]
                    / "alembic/versions/0196_dataset_scan_ledger.py",
                )
                scan = importlib.util.module_from_spec(scan_spec)
                scan_spec.loader.exec_module(scan)
                monkeypatch.setattr(
                    scan, "op", Operations(MigrationContext.configure(sync))
                )
                scan.downgrade()
                child.downgrade()
                monkeypatch.setattr(
                    migration, "op", Operations(MigrationContext.configure(sync))
                )
                migration.downgrade()
                assert (
                    sync.scalar(text("SELECT to_regclass('public.dataset_assets')"))
                    is None
                )
                migration.upgrade()
                assert (
                    sync.scalar(text("SELECT to_regclass('public.dataset_assets')"))
                    == "dataset_assets"
                )
                child.upgrade()
                scan.upgrade()

            await connection.run_sync(roundtrip)
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_used_mapping_refuses_destructive_downgrade(db_session, monkeypatch):
    from app.services.dataset_assets import ensure_legacy_bindings

    await ensure_legacy_bindings(db_session)

    def refuse(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        with pytest.raises(RuntimeError, match="Retain used identity mappings"):
            migration.downgrade()
        assert sync.scalar(text("SELECT count(*) FROM storage_bindings")) == 9

    await db_session.run_sync(refuse)
