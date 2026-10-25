"""Archive location migration preserves permanent facts and refuses destructive rollback."""

import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from app.db.models.dataset_asset import StorageBinding
from app.services.dataset_assets import ensure_legacy_bindings
from tests.test_audit_archive_location_records import owner
from tests.test_audit_archive_storage import reference


def migration():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0247_audit_archive_location_bindings.py"
    )
    spec = importlib.util.spec_from_file_location("archive_location_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_roundtrip_preserves_legacy_location_and_state(db_session):
    row = owner()
    row.storage_ref = {
        **reference(),
        "object_key": f"{row.year}/{row.month:02d}/{row.id}.jsonl.gz",
    }
    db_session.add(row)
    await db_session.flush()
    saved = dict(row.storage_ref)
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration().downgrade()
            result = conn.execute(
                text(
                    "SELECT storage_ref,state FROM audit_archive_outputs WHERE id=:identifier"
                ),
                {"identifier": row.id},
            ).one()
            assert result.storage_ref == saved and result.state == "pending"
            migration().upgrade()

    await connection.run_sync(roundtrip)


@pytest.mark.parametrize("fact", ["schema2", "binding"])
async def test_new_facts_refuse_destructive_downgrade(db_session, fact):
    if fact == "binding":
        await ensure_legacy_bindings(db_session)
        (await db_session.get(StorageBinding, "audit-archive")).generation = 2
    else:
        db_session.add(owner())
    await db_session.flush()
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
                migration().downgrade()

    await connection.run_sync(refuse)
