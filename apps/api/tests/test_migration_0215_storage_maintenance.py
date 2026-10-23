"""Maintenance facts and unresolved access cannot disappear on downgrade."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import inspect, text

from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from tests.test_storage_maintenance_impact import location


def migration(sync, monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/0215_storage_maintenance.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return module


async def test_unused_schema_roundtrip_seeds_external_locations_without_business_fk(
    db_session, monkeypatch
):
    where = await location(db_session)

    def verify(sync):
        module = migration(sync, monkeypatch)
        module.downgrade()
        assert sync.scalar(text("SELECT to_regclass('storage_operations')")) is None
        module.upgrade()
        assert sync.execute(
            text(
                "SELECT state,revision,changed_at FROM storage_maintenance_gates WHERE location_id=:id"
            ),
            {"id": where.id},
        ).one() == ("active", 1, None)
        schema = inspect(sync.connection())
        assert not any(
            fk["referred_table"] == "storage_locations"
            for fk in schema.get_foreign_keys("storage_maintenance_gates")
        )
        assert any(
            fk["referred_table"] == "storage_maintenance_gates"
            and fk["options"].get("ondelete") == "RESTRICT"
            for fk in schema.get_foreign_keys("storage_operations")
        )

    await db_session.run_sync(verify)


@pytest.mark.parametrize("history", ["draining", "resumed", "receipt"])
async def test_history_and_unresolved_receipts_block_destructive_downgrade(
    db_session, monkeypatch, history
):
    identity = uuid.uuid4()
    db_session.add(
        Gate(
            location_id=identity,
            state="draining" if history == "draining" else "active",
            revision=3 if history == "resumed" else 1,
        )
    )
    await db_session.flush()
    if history == "receipt":
        db_session.add(
            StorageOperation(
                location_id=identity,
                revision=1,
                purpose="crashed_worker",
                node_id="test:node",
            )
        )
        await db_session.flush()

    def verify(sync):
        with pytest.raises(RuntimeError, match="maintenance history or unresolved"):
            migration(sync, monkeypatch).downgrade()
        assert (
            sync.scalar(text("SELECT to_regclass('storage_maintenance_gates')"))
            is not None
        )

    await db_session.run_sync(verify)
