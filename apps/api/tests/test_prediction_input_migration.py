"""Recovery records cannot silently disappear during schema rollback."""

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services.prediction_inputs import PredictionInputs
from tests.test_prediction_inputs import input_world as input_world, open_inputs


async def test_recovery_migration_refuses_nonempty_downgrade_and_is_reversible(
    input_world, test_engine
):
    inputs = await open_inputs(input_world)
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

    try:
        async with test_engine.begin() as connection:
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

            @asynccontextmanager
            async def waiting_factory():
                async with input_world.factory() as db:
                    await db.execute(text("SET LOCAL lock_timeout='100ms'"))
                    yield db

            # The downgrade lock also prevents a new intent from slipping past
            # its empty-table check and being dropped before its first PUT.
            with pytest.raises(DBAPIError) as blocked:
                await PredictionInputs.create(
                    waiting_factory,
                    owner_id=input_world.owner_id,
                    owner_kind="batch_predict",
                    task_id=input_world.task_id,
                )
            assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
            assert not input_world.puts
    finally:
        await inputs.close()
    async with test_engine.begin() as connection:
        await connection.run_sync(lambda conn: migrate(conn, migration.downgrade))
        assert (
            await connection.scalar(
                text("SELECT to_regclass('prediction_input_leases')")
            )
            is None
        )
        await connection.run_sync(lambda conn: migrate(conn, migration.upgrade))
        assert (
            await connection.scalar(
                text("SELECT to_regclass('prediction_input_leases')")
            )
            is not None
        )
