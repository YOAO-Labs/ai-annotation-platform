import importlib.util
from pathlib import Path
import uuid

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services.storage_location import connector_location_id
from tests.migration_helpers import bind, historical_schema, load


async def test_connector_registry_backfill_roundtrip_and_parent_cleanup(
    db_session, monkeypatch
):
    connection = StorageConnection(
        name="historic",
        kind="s3",
        config={"endpoint": "internal", "bucket": "original"},
        secret_enc=b"encrypted-existing",
        scope="owner",
    )
    db_session.add(connection)
    await db_session.flush()
    before = connection.updated_at
    spec = importlib.util.spec_from_file_location(
        "migration_0195",
        Path(__file__).parents[1]
        / "alembic/versions/0195_controlled_storage_locations.py",
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def roundtrip(sync):
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        migration.downgrade()
        migration.upgrade()
        assert (
            sync.scalar(
                text(
                    "SELECT secret_enc FROM storage_connections WHERE name = 'historic'"
                )
            )
            == b"encrypted-existing"
        )

    await db_session.run_sync(roundtrip)
    location = await db_session.scalar(
        select(StorageLocation).where(StorageLocation.connection_id == connection.id)
    )
    assert location.id == connector_location_id(connection.id)
    assert location.config == {} and location.config_revision == 1
    await db_session.refresh(connection)
    assert connection.updated_at == before
    await db_session.delete(connection)
    await db_session.flush()
    assert (
        await db_session.scalar(
            select(StorageLocation.id).where(StorageLocation.id == location.id)
        )
        is None
    )


async def test_referenced_filesystem_location_refuses_downgrade(test_engine):
    async with historical_schema(test_engine, "0195") as (_schema, engine):
        async with engine.begin() as connection:
            location_id = uuid.uuid4()
            await connection.execute(
                text(
                    "INSERT INTO storage_locations (id,name,kind,config) "
                    "VALUES (:id,'referenced-directory','filesystem','{}'::jsonb)"
                ),
                {"id": location_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO storage_bindings (purpose,location_id,bucket,generation) "
                    "VALUES ('datasets',:id,'unused',1)"
                ),
                {"id": location_id},
            )

            def refuse(sync):
                module = bind(load("0195_controlled_storage_locations.py"), sync)
                with pytest.raises(
                    RuntimeError, match="Retain referenced storage locations"
                ):
                    module.downgrade()
                assert (
                    sync.scalar(text("SELECT count(*) FROM storage_location_probes"))
                    == 0
                )
                assert sync.scalar(text("SELECT count(*) FROM storage_bindings")) == 1

            await connection.run_sync(refuse)
