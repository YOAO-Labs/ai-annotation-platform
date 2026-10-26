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
from tests.migration_helpers import bind, historical_schema, load, relation


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
    test_engine,
):
    async with historical_schema(test_engine, "0215") as (schema, engine):
        async with engine.begin() as connection:
            location_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO storage_locations (id,name,kind,config) "
                    "VALUES (:id,:name,'filesystem','{}'::jsonb)"
                ),
                {"id": location_id, "name": "maintenance-" + uuid.uuid4().hex},
            )

            def verify(sync):
                module = bind(load("0215_storage_maintenance.py"), sync)
                module.downgrade()
                assert relation(sync, schema, "storage_operations") is None
                module.upgrade()
                assert sync.execute(
                    text(
                        "SELECT state,revision,changed_at FROM storage_maintenance_gates WHERE location_id=:id"
                    ),
                    {"id": location_id},
                ).one() == ("active", 1, None)
                actual = inspect(sync)
                assert not any(
                    fk["referred_table"] == "storage_locations"
                    for fk in actual.get_foreign_keys("storage_maintenance_gates")
                )
                assert any(
                    fk["referred_table"] == "storage_maintenance_gates"
                    and fk["options"].get("ondelete") == "RESTRICT"
                    for fk in actual.get_foreign_keys("storage_operations")
                )

            await connection.run_sync(verify)


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
