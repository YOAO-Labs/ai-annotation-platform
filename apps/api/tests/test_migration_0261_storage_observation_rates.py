"""Daily summaries survive application rollback and reject destructive downgrade."""

from importlib import util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import Date, delete, func, insert, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models.storage_observation_rate import StorageObservationBucket as Bucket


def migration():
    path = (
        Path(__file__).parents[1] / "alembic/versions/0261_storage_observation_rates.py"
    )
    spec = util.spec_from_file_location("rate_migration", path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_empty_roundtrip_has_exact_primary_key_and_no_business_foreign_keys(
    db_session,
):
    await db_session.execute(delete(Bucket))
    connection = await db_session.connection()

    def roundtrip(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration().downgrade()
            assert (
                conn.scalar(text("SELECT to_regclass('storage_observation_buckets')"))
                is None
            )
            migration().upgrade()
            inspector = inspect(conn)
            assert inspector.get_foreign_keys(Bucket.__tablename__) == []
            assert inspector.get_pk_constraint(Bucket.__tablename__)[
                "constrained_columns"
            ] == ["day", "location_id", "source_id", "metric"]
            assert len(inspector.get_check_constraints(Bucket.__tablename__)) == 3

    await connection.run_sync(roundtrip)


def values():
    return dict(
        day=func.timezone("UTC", func.statement_timestamp()).cast(Date),
        location_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        metric="original_read",
        success_count=1,
        failure_count=0,
        first_observed_at=func.statement_timestamp(),
        last_observed_at=func.statement_timestamp(),
    )


async def test_nonempty_downgrade_preserves_summary(db_session):
    await db_session.execute(insert(Bucket).values(**values()))
    connection = await db_session.connection()

    def refuse(conn):
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="empty table"):
                migration().downgrade()
            assert (
                conn.scalar(
                    text("SELECT sum(success_count) FROM storage_observation_buckets")
                )
                >= 1
            )

    await connection.run_sync(refuse)


@pytest.mark.parametrize(
    "changes",
    [
        {"metric": "scan"},
        {"success_count": -1},
        {"success_count": 0},
        {"last_observed_at": text("statement_timestamp()-interval '1 day'")},
    ],
)
async def test_invalid_aggregate_facts_are_rejected(db_session, changes):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(insert(Bucket).values(**{**values(), **changes}))
