"""The additive edit revision preserves existing configuration and ciphertext."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.db.models.storage_connection import StorageConnection


async def test_revision_backfill_roundtrip_preserves_historical_connection(
    db_session, monkeypatch
):
    connection = StorageConnection(
        name="historic",
        kind="s3",
        config={"bucket": "original"},
        secret_enc=b"existing-ciphertext",
        scope="owner",
    )
    db_session.add(connection)
    await db_session.flush()
    before = connection.updated_at
    spec = importlib.util.spec_from_file_location(
        "migration_0210",
        Path(__file__).parents[1]
        / "alembic/versions/0210_storage_connection_revision.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        migration.upgrade()
        row = sync.execute(
            text(
                "SELECT revision, secret_enc, config, updated_at FROM storage_connections WHERE id=:id"
            ),
            {"id": connection.id},
        ).one()
        assert row.revision == 1 and row.secret_enc == b"existing-ciphertext"
        assert row.config == {"bucket": "original"} and row.updated_at == before

    await db_session.run_sync(roundtrip)
