"""New private query table round-trips and enforces database ownership keys."""

import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect


async def test_saved_views_schema_roundtrip(db_session, monkeypatch):
    def verify(sync):
        path = (
            Path(__file__).parents[1] / "alembic/versions/0206_dataset_saved_views.py"
        )
        spec = importlib.util.spec_from_file_location(path.stem, path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        assert "dataset_saved_views" not in inspect(sync.connection()).get_table_names()
        migration.upgrade()
        schema = inspect(sync.connection())
        assert any(
            row["column_names"] == ["dataset_id", "owner_id", "name"]
            for row in schema.get_unique_constraints("dataset_saved_views")
        )
        assert {
            fk["referred_table"]
            for fk in schema.get_foreign_keys("dataset_saved_views")
        } == {"datasets", "users"}
        assert all(
            fk["options"]["ondelete"] == "CASCADE"
            for fk in schema.get_foreign_keys("dataset_saved_views")
        )

    await db_session.run_sync(verify)
