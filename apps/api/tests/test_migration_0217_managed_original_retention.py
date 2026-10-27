"""Retention facts and immutable declaration history survive schema rollback."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, text, update
from sqlalchemy.exc import IntegrityError

from app.db.models.managed_original_retention_hold import (
    ManagedOriginalRetentionHold as Hold,
)
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from tests.test_managed_original_reclamation import assertion


def migration(sync, monkeypatch):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0217_managed_original_retention.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return module


def hold(row, **fields):
    return Hold(
        **(
            {
                "id": uuid.uuid4(),
                "retirement_id": row.id,
                "kind": "backup",
                "reference_code": "BACKUP:synthetic",
                "reason": "Declared recovery requirement",
                "created_by": uuid.uuid4(),
            }
            | fields
        )
    )


async def test_empty_retention_schema_roundtrip_with_initial_retirement(
    db_session, monkeypatch
):
    row = await assertion(db_session)

    def verify(sync):
        module = migration(sync, monkeypatch)
        assert module.down_revision == "0216"
        module.downgrade()
        schema = inspect(sync.connection())
        assert "managed_original_retention_holds" not in schema.get_table_names()
        module.upgrade()
        schema = inspect(sync.connection())
        foreign_keys = schema.get_foreign_keys("managed_original_retention_holds")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "managed_original_retirements"
        assert foreign_keys[0]["options"]["ondelete"] == "RESTRICT"
        assert {
            column["name"]
            for column in schema.get_columns("managed_original_retention_holds")
        } == set(Hold.__table__.columns.keys())
        assert any(
            index["name"] == "ix_managed_retirement_locator_hash"
            for index in schema.get_indexes("managed_original_retirements")
        )
        assert sync.execute(
            text(
                "SELECT policy_revision, retain_until FROM managed_original_retirements WHERE id=:id"
            ),
            {"id": row.id},
        ).one() == (1, None)

    await db_session.run_sync(verify)


@pytest.mark.parametrize(
    "fact", ["active_hold", "released_hold", "revision", "deadline"]
)
async def test_retention_facts_refuse_destructive_downgrade(
    db_session, monkeypatch, fact
):
    row = await assertion(db_session)
    if fact in {"active_hold", "released_hold"}:
        fields = (
            {
                "revision": 2,
                "released_at": datetime.now(timezone.utc),
                "released_by": uuid.uuid4(),
                "release_reason": "Reviewed release",
            }
            if fact == "released_hold"
            else {}
        )
        db_session.add(hold(row, **fields))
    elif fact == "revision":
        row.policy_revision = 2
    else:
        row.retain_until = datetime.now(timezone.utc)
    await db_session.flush()

    def verify(sync):
        with pytest.raises(RuntimeError, match="retention facts"):
            migration(sync, monkeypatch).downgrade()
        assert (
            "managed_original_retention_holds"
            in inspect(sync.connection()).get_table_names()
        )

    await db_session.run_sync(verify)


@pytest.mark.parametrize(
    "invalid",
    [
        {"kind": "object-lock"},
        {"reason": "\t\n"},
        {"reference_code": "unsafe/path"},
        {"revision": 2},
        {"released_by": uuid.uuid4()},
        {"retirement_id": uuid.uuid4()},
    ],
)
async def test_hold_database_constraints(db_session, invalid):
    row = await assertion(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(hold(row, **invalid))
            await db_session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("reference_code", "NEW:reference"),
        ("reason", "Rewritten declaration"),
        ("created_by", uuid.uuid4()),
        ("kind", "migration"),
    ],
)
async def test_creation_history_is_database_immutable(db_session, field, value):
    row = await assertion(db_session)
    declared = hold(row)
    db_session.add(declared)
    await db_session.flush()
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Hold).where(Hold.id == declared.id).values({field: value})
            )
    await db_session.refresh(declared)
    assert getattr(declared, field) != value


async def test_exact_anchor_fk_active_uniqueness_and_release_history(db_session):
    db = db_session
    row = await assertion(db)
    declared = hold(row)
    db.add(declared)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(delete(Retirement).where(Retirement.id == row.id))
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(hold(row))
            await db.flush()
    await db.execute(
        update(Hold)
        .where(Hold.id == declared.id)
        .values(
            revision=2,
            released_at=datetime.now(timezone.utc),
            released_by=uuid.uuid4(),
            release_reason="Independently reviewed",
        )
    )
    db.add(hold(row))
    await db.flush()
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db.begin_nested():
            await db.execute(
                update(Hold)
                .where(Hold.id == declared.id)
                .values(release_reason="Rewritten release")
            )
