"""0255 is additive, reversible when unused and cascades only with its job."""

import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0255_storage_migration_inventories.py"
    )
    spec = importlib.util.spec_from_file_location(
        "inventory_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_0255_roundtrip_preserves_existing_tables_and_bounded_private_fk(
    db_session,
):
    connection = await db_session.connection()
    before = await db_session.scalar(text("SELECT count(*) FROM users"))
    await connection.run_sync(lambda sync: migration(sync).downgrade())
    await connection.run_sync(lambda sync: migration(sync).upgrade())
    constraints = await connection.run_sync(
        lambda sync: inspect(sync).get_check_constraints(
            "storage_migration_inventories"
        )
    )
    report_check = next(
        row["sqltext"]
        for row in constraints
        if row["name"] == "ck_migration_inventory_report"
    )
    assert "33554432" in report_check and "octet_length(report)" in report_check
    foreign_keys = await connection.run_sync(
        lambda sync: inspect(sync).get_foreign_keys("storage_migration_inventories")
    )
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "async_jobs"
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"
    assert await db_session.scalar(text("SELECT count(*) FROM users")) == before
