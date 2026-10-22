import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
import pytest


async def test_empty_scan_ledger_roundtrip_and_retention_guard(db_session, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_0196",
        Path(__file__).parents[1] / "alembic/versions/0196_dataset_scan_ledger.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        assert (
            sync.scalar(text("SELECT to_regclass('public.dataset_scan_runs')")) is None
        )
        migration.upgrade()
        assert (
            sync.scalar(text("SELECT to_regclass('public.dataset_scan_entries')"))
            == "dataset_scan_entries"
        )
        sync.execute(
            text(
                "INSERT INTO storage_locations (id, name, kind, config) VALUES ('00000000-0000-0000-0000-000000000196','migration-scan-root','filesystem','{}')"
            )
        )
        sync.execute(
            text(
                "INSERT INTO dataset_sources (id, location_id, name, config) VALUES ('00000000-0000-0000-0000-000000000196','00000000-0000-0000-0000-000000000196','history','{}')"
            )
        )
        with pytest.raises(
            RuntimeError, match="Retain dataset source and scan history"
        ):
            migration.downgrade()
        assert sync.scalar(text("SELECT count(*) FROM dataset_sources")) == 1

    await db_session.run_sync(roundtrip)
