"""Historical recovery records cannot disappear during schema rollback."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine


async def test_recovery_migration_refuses_nonempty_downgrade_and_is_reversible(
    test_engine,
):
    # Exercise the historical migration at its actual schema shape. New v2
    # owners (including deleted tombstones) have their own downgrade coverage.
    schema = "prediction_input_migration_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    path = (
        Path(__file__).parents[1] / "alembic/versions/0212_prediction_input_leases.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"input_migration_{uuid.uuid4().hex}", path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, operation):
        with Operations.context(MigrationContext.configure(connection)):
            operation()

    insert = text(
        "INSERT INTO prediction_input_leases "
        "(id, owner_id, owner_kind, task_id, bucket, storage_identity, state, "
        "uncertain_write, expires_at, retain_until, next_cleanup_at) VALUES "
        "(:id, :owner, 'batch_predict', :task, 'historical-inputs', :identity, "
        "'active', false, now() + interval '2 hours', "
        "now() + interval '7 days 2 hours', now())"
    )

    def values():
        return {
            "id": uuid.uuid4(),
            "owner": uuid.uuid4(),
            "task": uuid.uuid4(),
            "identity": "a" * 64,
        }

    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda conn: migrate(conn, migration.upgrade))
            await connection.execute(insert, values())
        async with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="outstanding prediction input"):
                await connection.run_sync(
                    lambda conn: migrate(conn, migration.downgrade)
                )
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM prediction_input_leases")
                )
                == 1
            )

            # The downgrade retains its exclusive lock until transaction end,
            # so a concurrent intent cannot pass the empty check and be lost.
            with pytest.raises(DBAPIError) as blocked:
                async with engine.begin() as concurrent:
                    await concurrent.execute(text("SET LOCAL lock_timeout='100ms'"))
                    await concurrent.execute(insert, values())
            assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM prediction_input_leases"))
            await connection.run_sync(lambda conn: migrate(conn, migration.downgrade))
            assert (
                await connection.scalar(
                    text("SELECT to_regclass(:qualified)"),
                    {"qualified": schema + ".prediction_input_leases"},
                )
                is None
            )
            await connection.run_sync(lambda conn: migrate(conn, migration.upgrade))
            assert (
                await connection.scalar(
                    text("SELECT to_regclass(:qualified)"),
                    {"qualified": schema + ".prediction_input_leases"},
                )
                is not None
            )
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
