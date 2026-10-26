"""Read publication schema preserves exact routes and retained owner history."""

from importlib import util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.storage_migration_read_publication import (
    StorageMigrationReadPublication as Publication,
    StorageMigrationReadRoute as Route,
)
from tests.storage_migration_isolation import (
    db_session as db_session,
    migration_engine as migration_engine,
)
from tests.test_storage_migration_finalizations import (
    bound as bound,
    control as control,
)
from tests.test_storage_migration_read_publications import (
    publish,
    sealed as sealed,
)
from tests.test_storage_migration_windows import prepared as prepared


def migration(connection):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0263_storage_migration_read_publications.py"
    )
    spec = util.spec_from_file_location("read_publication_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


async def test_empty_roundtrip_and_restrict_owner_fks(test_engine):
    # Schema roundtrip drops named indexes/constraints, so it must run against
    # the real public schema, not the isolation clone (LIKE renames them).
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            assert await connection.scalar(select(Publication.id).limit(1)) is None

            def roundtrip(conn):
                from tests.test_storage_migration_cutovers import (
                    migration as cutover_migration,
                )

                cutover_migration(conn).downgrade()
                migration(conn).downgrade()
                migration(conn).upgrade()
                cutover_migration(conn).upgrade()
                schema = inspect(conn)
                assert len(schema.get_check_constraints(Publication.__tablename__)) == 5
                assert len(schema.get_check_constraints(Route.__tablename__)) == 3
                foreign = schema.get_foreign_keys(Publication.__tablename__)
                assert len(foreign) == 4
                assert all(row["options"]["ondelete"] == "RESTRICT" for row in foreign)
                route_foreign = schema.get_foreign_keys(Route.__tablename__)
                assert route_foreign[0]["options"]["ondelete"] == "RESTRICT"
                index = next(
                    row
                    for row in schema.get_indexes(Publication.__tablename__)
                    if row["name"] == "uq_migration_read_publication_active_source"
                )
                assert index["unique"]

            await connection.run_sync(roundtrip)
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("withdrawn", [False, True])
async def test_nonempty_downgrade_retains_even_withdrawn_history(
    db_session, sealed, withdrawn
):
    from app.services import storage_migration_read_publications as service

    value = await publish(db_session, sealed)
    if withdrawn:
        await service.begin_withdrawal(db_session, sealed.actor.id, sealed.claim, 1)
        await service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 2)
    connection = await db_session.connection()
    with pytest.raises(RuntimeError, match="retain migration responsibility"):
        await connection.run_sync(lambda conn: migration(conn).downgrade())
    assert await db_session.get(Publication, value.publication_id)
    assert len(list(await db_session.scalars(select(Route)))) == 2


@pytest.mark.parametrize(
    "change",
    [
        "delete",
        "source",
        "target",
        "token",
        "evidence",
        "digest",
        "count",
        "revision",
        "transition",
        "route_update",
        "route_delete",
    ],
)
async def test_database_rejects_identity_evidence_and_route_loss(
    db_session, sealed, change
):
    value = await publish(db_session, sealed)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            if change == "delete":
                await db_session.execute(
                    delete(Publication).where(Publication.id == value.publication_id)
                )
            elif change.startswith("route_"):
                statement = (
                    delete(Route)
                    if change == "route_delete"
                    else update(Route).values(target_version_id="foreign-version")
                )
                await db_session.execute(
                    statement.where(Route.publication_id == value.publication_id)
                )
            else:
                values = {"revision": 2}
                if change in {"source", "target", "token", "evidence"}:
                    values[change] = {}
                elif change == "digest":
                    values["mapping_sha256"] = "0" * 64
                elif change == "count":
                    values["route_count"] = 1
                elif change == "revision":
                    values["revision"] = 4
                else:
                    values["state"] = "withdrawn"
                await db_session.execute(
                    update(Publication)
                    .where(Publication.id == value.publication_id)
                    .values(**values)
                )


async def test_withdrawn_publication_cannot_be_reactivated(db_session, sealed):
    from app.services import storage_migration_read_publications as service

    value = await publish(db_session, sealed)
    await service.begin_withdrawal(db_session, sealed.actor.id, sealed.claim, 1)
    await service.finish_withdrawal(db_session, sealed.actor.id, sealed.claim, 2)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Publication)
                .where(Publication.id == value.publication_id)
                .values(
                    state="active", revision=4, withdrawn_at=None, withdrawing_at=None
                )
            )
