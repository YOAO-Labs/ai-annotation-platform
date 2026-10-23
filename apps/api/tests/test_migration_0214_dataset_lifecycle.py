"""Lifecycle facts must survive schema upgrades and block destructive downgrade."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text

from app.db.models.dataset import Dataset


def migration(sync, monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/0214_dataset_lifecycle.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return module


async def test_untouched_dataset_lifecycle_schema_roundtrip(
    db_session, super_admin, monkeypatch
):
    row = Dataset(
        display_id=f"D-{uuid.uuid4().hex[:10]}",
        name="Historical active dataset",
        created_by=super_admin[0].id,
    )
    db_session.add(row)
    await db_session.flush()

    def verify(sync):
        module = migration(sync, monkeypatch)
        module.downgrade()
        assert "lifecycle_revision" not in {
            c["name"] for c in inspect(sync.connection()).get_columns("datasets")
        }
        module.upgrade()
        assert sync.execute(
            text(
                "SELECT archived_at, archived_by, lifecycle_revision FROM datasets WHERE id=:id"
            ),
            {"id": row.id},
        ).one() == (None, None, 1)
        schema = inspect(sync.connection())
        assert any(
            fk["constrained_columns"] == ["archived_by"]
            and fk["referred_table"] == "users"
            and fk["options"].get("ondelete") == "SET NULL"
            for fk in schema.get_foreign_keys("datasets")
        )
        assert any(
            index["name"] == "ix_datasets_archived_at"
            for index in schema.get_indexes("datasets")
        )

    await db_session.run_sync(verify)


@pytest.mark.parametrize("archived", [True, False])
async def test_lifecycle_history_blocks_downgrade(
    db_session, super_admin, monkeypatch, archived
):
    row = Dataset(
        display_id=f"D-{uuid.uuid4().hex[:10]}",
        name="Retained lifecycle history",
        created_by=super_admin[0].id,
        lifecycle_revision=2 if archived else 3,
        archived_at=datetime.now(timezone.utc) if archived else None,
        archived_by=super_admin[0].id if archived else None,
    )
    db_session.add(row)
    await db_session.flush()

    def verify(sync):
        module = migration(sync, monkeypatch)
        with pytest.raises(RuntimeError, match="lifecycle history"):
            module.downgrade()
        assert {"archived_at", "archived_by", "lifecycle_revision"} <= {
            c["name"] for c in inspect(sync.connection()).get_columns("datasets")
        }
        assert sync.execute(
            text("SELECT lifecycle_revision FROM datasets WHERE id=:id"),
            {"id": row.id},
        ).scalar_one() == (2 if archived else 3)

    await db_session.run_sync(verify)
