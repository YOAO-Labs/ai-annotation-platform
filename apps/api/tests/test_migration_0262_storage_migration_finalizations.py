"""Finalization schema retains identity, evidence and native responsibility."""

from importlib import util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.storage_migration_finalization import (
    StorageMigrationFinalization as Finalization,
)
from tests.test_storage_migration_finalizations import (
    bound as bound,
    control as control,
)
from tests.test_storage_migration_windows import prepared as prepared


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0262_storage_migration_finalizations.py"
    )
    spec = util.spec_from_file_location("finalization_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_empty_roundtrip_keeps_historical_inventory_id_without_job_fk(db_session):
    assert await db_session.scalar(select(Finalization.id).limit(1)) is None
    connection = await db_session.connection()

    def roundtrip(conn):
        from tests.test_storage_migration_cutovers import migration as cutover_migration
        from tests.test_migration_0263_storage_migration_read_publications import (
            migration as read_migration,
        )

        cutover_migration(conn).downgrade()
        read_migration(conn).downgrade()
        migration(conn).downgrade()
        migration(conn).upgrade()
        read_migration(conn).upgrade()
        cutover_migration(conn).upgrade()
        schema = inspect(conn)
        foreign = schema.get_foreign_keys(Finalization.__tablename__)
        assert len(foreign) == 4
        assert all(row["options"]["ondelete"] == "RESTRICT" for row in foreign)
        assert not any(
            "inventory_job_id" in row["constrained_columns"] for row in foreign
        )
        assert len(schema.get_check_constraints(Finalization.__tablename__)) == 6

    await connection.run_sync(roundtrip)


async def test_nonempty_downgrade_preserves_history(db_session, bound):
    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="retain native responsibility"):
        await connection.run_sync(lambda conn: migration(conn).downgrade())
    assert await db_session.get(Finalization, uuid.UUID(bound.claim["finalization_id"]))


@pytest.mark.parametrize(
    "change", ["claim", "revision", "identity", "delete", "transition"]
)
async def test_database_guards_reject_loss_or_reinterpretation(
    db_session, bound, change
):
    key = uuid.UUID(bound.claim["finalization_id"])
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            if change == "delete":
                await db_session.execute(
                    delete(Finalization).where(Finalization.id == key)
                )
            else:
                values = {"revision": 2}
                if change == "claim":
                    values["claim"] = {
                        **bound.claim,
                        "reservation_nonce": str(uuid.uuid4()),
                    }
                elif change == "revision":
                    values = {"revision": 4, "state": "invalidating"}
                elif change == "identity":
                    values["window_revision"] = 40
                else:
                    values["state"] = "invalidated"
                await db_session.execute(
                    update(Finalization).where(Finalization.id == key).values(**values)
                )


async def test_unique_active_window_and_historical_row_are_not_replaceable(
    db_session, bound
):
    row = await db_session.get(Finalization, uuid.UUID(bound.claim["finalization_id"]))
    from app.services.storage_migration_finalizations import _new
    from app.db.models.storage_migration_window import StorageMigrationWindow

    window = await db_session.get(StorageMigrationWindow, bound.window_id)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                _new(
                    window,
                    bound.actor.id,
                    {**bound.claim, "finalization_id": str(uuid.uuid4())},
                    bound.native,
                )
            )
            await db_session.flush()
    assert row.state == "bound"
