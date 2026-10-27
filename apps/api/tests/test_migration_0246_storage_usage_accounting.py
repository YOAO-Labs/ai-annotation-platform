"""Application rollback retains facts; explicit empty-schema downgrade is safe."""

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text, inspect

from tests.migration_helpers import historical_schema, load


async def test_empty_roundtrip_preserves_old_snapshot_and_populated_ledger_blocks(
    test_engine,
):
    migration = load("0246_storage_usage_accounting.py")
    async with historical_schema(test_engine, "0246") as (_schema, engine):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO storage_usage_snapshots (scope_key, object_count, total_size_bytes, attempt_status) VALUES ('usage-migration-test', 7, 123, 'completed')"
                )
            )

            def check(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    relations = (
                        conn.execute(
                            text(
                                "SELECT oid FROM pg_class WHERE relname IN ('storage_usage_mutations','storage_usage_inventory_objects','storage_usage_reconciliations','storage_usage_schedules','storage_usage_snapshots') AND relnamespace=current_schema()::regnamespace"
                            )
                        )
                        .scalars()
                        .all()
                    )
                    migration.downgrade()
                    assert (
                        conn.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND mode='AccessExclusiveLock' AND relation=ANY(:relations)"
                            ),
                            {"relations": relations},
                        )
                        == 5
                    )
                    assert "storage_usage_mutations" not in inspect(
                        conn
                    ).get_table_names(schema=_schema)
                    assert (
                        conn.scalar(
                            text(
                                "SELECT total_size_bytes FROM storage_usage_snapshots WHERE scope_key='usage-migration-test'"
                            )
                        )
                        == 123
                    )
                    migration.upgrade()
                    conn.execute(
                        text(
                            "INSERT INTO storage_usage_schedules (id, enabled, interval_hours, status, authority, scope) VALUES ('platform', false, 24, 'off', '{}', '[]')"
                        )
                    )
                    with pytest.raises(RuntimeError, match="must be retained"):
                        migration.downgrade()
                    assert "storage_usage_mutations" in inspect(conn).get_table_names(
                        schema=_schema
                    )

            await connection.run_sync(check)
