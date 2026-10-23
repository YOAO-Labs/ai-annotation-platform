"""Existing schedules remain manual; enabled automation blocks policy loss."""

import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from app.schemas.dataset_source import SourceSyncScheduleUpdate as Update
from app.services.dataset_source_sync import configure
from tests.test_dataset_content import content_world as content_world


async def test_policy_defaults_roundtrip_and_rollback_barrier(
    db_session, content_world, monkeypatch
):
    row = await configure(
        db_session,
        content_world[5].id,
        Update(expected_revision=0, enabled=True),
        content_world[2],
    )

    def verify(sync):
        path = (
            Path(__file__).parents[1]
            / "alembic/versions/0205_source_sync_ingestion_policy.py"
        )
        spec = importlib.util.spec_from_file_location(path.stem, path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        migration.upgrade()
        assert sync.execute(
            text("SELECT new_file_policy,timezone FROM dataset_source_sync_schedules")
        ).one() == ("manual", "UTC")
        sync.execute(
            text(
                "UPDATE dataset_source_sync_schedules SET new_file_policy='pure_additions'"
            )
        )
        with pytest.raises(RuntimeError, match="Pause automatic"):
            migration.downgrade()
        sync.execute(text("UPDATE dataset_source_sync_schedules SET enabled=false"))
        migration.downgrade()
        migration.upgrade()

    await db_session.run_sync(verify)
    await db_session.refresh(row)
    assert row.new_file_policy == "manual"
