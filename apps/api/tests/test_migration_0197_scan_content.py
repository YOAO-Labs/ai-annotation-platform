import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
import pytest


async def test_content_receipt_roundtrip_and_retention(db_session, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_0197",
        Path(__file__).parents[1]
        / "alembic/versions/0197_scan_content_verification.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def verify(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        assert not sync.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='dataset_scan_entries' AND column_name='verified_content')"
            )
        )
        migration.upgrade()
        sync.execute(
            text(
                "INSERT INTO storage_locations (id,name,kind,config) VALUES ('00000000-0000-0000-0000-000000000197','content-migration','filesystem','{}')"
            )
        )
        sync.execute(
            text(
                "INSERT INTO dataset_sources (id,location_id,name,config) VALUES ('00000000-0000-0000-0000-000000000197','00000000-0000-0000-0000-000000000197','content','{}')"
            )
        )
        sync.execute(
            text(
                "INSERT INTO dataset_scan_runs (id,source_id,snapshot) VALUES ('00000000-0000-0000-0000-000000000197','00000000-0000-0000-0000-000000000197','{}')"
            )
        )
        sync.execute(
            text(
                "INSERT INTO dataset_scan_entries (run_id,path_digest,relative_path,kind,state,observed,verified_content) VALUES ('00000000-0000-0000-0000-000000000197','digest','file','file','observed','{}','{}')"
            )
        )
        with pytest.raises(RuntimeError, match="Retain verified content"):
            migration.downgrade()
        assert (
            sync.scalar(
                text(
                    "SELECT count(*) FROM dataset_scan_entries WHERE verified_content IS NOT NULL"
                )
            )
            == 1
        )

    await db_session.run_sync(verify)
