"""Empty-only destructive downgrade; normal application rollback retains facts."""

from importlib import util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, func, insert, text

from app.db.models.storage_fault import StorageFaultState as State


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/0249_storage_fault_states.py"
    spec = util.spec_from_file_location("fault_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_empty_roundtrip_recreates_exact_table_indexes_and_sequence(db_session):
    await db_session.execute(delete(State))
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration().downgrade()
            assert (
                conn.scalar(text("SELECT to_regclass('storage_fault_states')")) is None
            )
            migration().upgrade()
            assert conn.scalar(text("SELECT count(*) FROM storage_fault_states")) == 0
            assert (
                conn.scalar(text("SELECT nextval('storage_fault_observation_seq')")) > 0
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE tablename='storage_fault_states'"
                    )
                )
                == 5
            )

    await connection.run_sync(roundtrip)


async def test_nonempty_state_refuses_downgrade_and_keeps_watermark(db_session):
    identifier = uuid.uuid4()
    await db_session.execute(
        insert(State).values(
            id=identifier,
            scope_key="a" * 64,
            location_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            category="root_unavailable",
            operation="scan",
            node_id="worker:test",
            state="recovered",
            first_failed_at=func.now(),
            last_failed_at=func.now(),
            recovered_at=func.now(),
            failure_count=2,
            last_sequence=12,
            recovered_sequence=12,
            scope_facts={},
            evidence={},
        )
    )
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="empty table"):
                migration().downgrade()
            assert (
                conn.scalar(
                    text("SELECT last_sequence FROM storage_fault_states WHERE id=:id"),
                    {"id": identifier},
                )
                == 12
            )

    await connection.run_sync(refuse)
