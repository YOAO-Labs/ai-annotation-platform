"""A category upgrade never invents old observations or drops bucket totals."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text


async def test_categories_migration_roundtrip_preserves_existing_totals(db_session):
    path = (
        Path(__file__).parents[1] / "alembic/versions/0242_storage_usage_categories.py"
    )
    spec = importlib.util.spec_from_file_location("usage_categories_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    await db_session.execute(
        text(
            "INSERT INTO storage_usage_snapshots (scope_key, object_count, total_size_bytes, attempt_status) VALUES ('category-migration-test', 7, 123, 'completed')"
        )
    )
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            assert "category_totals" not in {
                column["name"]
                for column in inspect(conn).get_columns("storage_usage_snapshots")
            }
            assert (
                conn.execute(
                    text(
                        "SELECT total_size_bytes FROM storage_usage_snapshots WHERE scope_key='category-migration-test'"
                    )
                ).scalar_one()
                == 123
            )
            migration.upgrade()
            assert (
                conn.execute(
                    text(
                        "SELECT category_totals FROM storage_usage_snapshots WHERE scope_key='category-migration-test'"
                    )
                ).scalar_one()
                is None
            )
            for table, name in [
                ("dataset_asset_locations", "ix_asset_location_usage_locator"),
                ("dataset_source_previews", "ix_source_preview_usage_locator"),
            ]:
                assert name in {
                    index["name"] for index in inspect(conn).get_indexes(table)
                }

    await connection.run_sync(roundtrip)
