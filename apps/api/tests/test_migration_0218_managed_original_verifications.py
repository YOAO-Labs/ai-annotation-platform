"""Observation facts and unsettled operation references survive schema rollback."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import delete, inspect, update
from sqlalchemy.exc import IntegrityError

from app.db.models.async_job import AsyncJob
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.db.models.managed_original_verification import (
    ManagedOriginalVerification as Verification,
)
from tests.test_managed_original_reclamation import assertion


def migration(sync, monkeypatch):
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0218_managed_original_verifications.py"
    )
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "op", Operations(MigrationContext.configure(sync.connection()))
    )
    return module


def verification(anchor, **fields):
    return Verification(
        **(
            {
                "id": uuid.uuid4(),
                "retirement_id": anchor.id,
                "requested_by": uuid.uuid4(),
                "task_id": str(uuid.uuid4()),
                "authority": {},
                "anchor": {},
                "configuration": {},
                "input_fingerprint": "a" * 64,
                "expected_sha256": "a" * 64,
                "expected_size_bytes": 42,
                "maintenance_revision": 1,
                "status": "pending",
                "observation": {},
            }
            | fields
        )
    )


def terminal_fields(status):
    now = datetime.now(timezone.utc)
    if status == "pending":
        return {}
    if status == "running":
        return {"started_at": now}
    if status == "completed":
        return {
            "started_at": now,
            "operation_id": uuid.uuid4(),
            "completed_at": now,
            "outcome": "matched",
            "observed_sha256": "a" * 64,
            "observed_size_bytes": 42,
        }
    return {
        "completed_at": now,
        "outcome": status,
        "error_code": "verification_interrupted" if status == "failed" else None,
        "cancel_requested_at": now if status == "cancelled" else None,
    }


async def test_empty_verification_schema_roundtrip_preserves_prior_anchor(
    db_session, monkeypatch
):
    anchor = await assertion(db_session)

    def verify(sync):
        module = migration(sync, monkeypatch)
        assert module.down_revision == "0217"
        module.downgrade()
        assert (
            "managed_original_verifications"
            not in inspect(sync.connection()).get_table_names()
        )
        module.upgrade()
        schema = inspect(sync.connection())
        assert {
            column["name"]
            for column in schema.get_columns("managed_original_verifications")
        } == set(Verification.__table__.columns.keys())
        foreign_keys = schema.get_foreign_keys("managed_original_verifications")
        assert {
            (fk["referred_table"], fk["options"]["ondelete"]) for fk in foreign_keys
        } == {("managed_original_retirements", "RESTRICT"), ("async_jobs", "SET NULL")}
        assert any(
            index["name"] == "uq_managed_verification_active" and index["unique"]
            for index in schema.get_indexes("managed_original_verifications")
        )
        assert sync.get(Retirement, anchor.id)

    await db_session.run_sync(verify)


@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "cancelled"]
)
async def test_any_verification_fact_refuses_destructive_downgrade(
    db_session, monkeypatch, status
):
    anchor = await assertion(db_session)
    db_session.add(verification(anchor, status=status, **terminal_fields(status)))
    await db_session.flush()

    def verify(sync):
        with pytest.raises(RuntimeError, match="verification facts"):
            migration(sync, monkeypatch).downgrade()
        assert (
            "managed_original_verifications"
            in inspect(sync.connection()).get_table_names()
        )

    await db_session.run_sync(verify)


@pytest.mark.parametrize(
    "fields",
    [
        {"expected_sha256": "not-sha"},
        {"expected_size_bytes": -1},
        {"maintenance_revision": 0},
        {"input_fingerprint": "bad"},
        {"status": "unknown"},
        {"outcome": "matched"},
        {"observed_sha256": "a" * 64},
        {"observation": {"premature": True}},
        {"retirement_id": uuid.uuid4()},
        {
            "status": "completed",
            "started_at": datetime.now(timezone.utc),
            "completed_at": datetime.now(timezone.utc),
            "operation_id": uuid.uuid4(),
            "outcome": None,
        },
        {
            "status": "completed",
            "started_at": datetime.now(timezone.utc),
            "completed_at": datetime.now(timezone.utc),
            "operation_id": uuid.uuid4(),
            "outcome": "matched",
            "observed_sha256": None,
            "observed_size_bytes": None,
        },
        {
            "status": "completed",
            "started_at": datetime.now(timezone.utc),
            "completed_at": datetime.now(timezone.utc),
            "outcome": "missing",
        },
    ],
)
async def test_database_rejects_invalid_or_invented_facts(db_session, fields):
    anchor = await assertion(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(verification(anchor, **fields))
            await db_session.flush()


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_by", uuid.uuid4()),
        ("task_id", str(uuid.uuid4())),
        ("authority", {"changed": True}),
        ("anchor", {"changed": True}),
        ("configuration", {"changed": True}),
        ("input_fingerprint", "b" * 64),
        ("expected_sha256", "b" * 64),
        ("expected_size_bytes", 43),
        ("maintenance_revision", 2),
    ],
)
async def test_initial_input_history_is_database_immutable(db_session, field, value):
    anchor = await assertion(db_session)
    row = verification(anchor)
    db_session.add(row)
    await db_session.flush()
    with pytest.raises(IntegrityError, match="history is immutable"):
        async with db_session.begin_nested():
            await db_session.execute(
                update(Verification)
                .where(Verification.id == row.id)
                .values({field: value})
            )


async def test_stable_anchor_single_active_and_delete_guard(db_session):
    db = db_session
    anchor = await assertion(db)
    row = verification(anchor)
    db.add(row)
    await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(verification(anchor))
            await db.flush()
    for model, identifier in ((Retirement, anchor.id), (Verification, row.id)):
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(delete(model).where(model.id == identifier))
    await db.execute(
        update(Verification)
        .where(Verification.id == row.id)
        .values(
            status="cancelled",
            outcome="cancelled",
            completed_at=datetime.now(timezone.utc),
            cancel_requested_at=datetime.now(timezone.utc),
        )
    )
    db.add(verification(anchor))
    await db.flush()


async def test_terminal_history_and_operation_receipt_are_immutable_but_job_cleanup_is_allowed(
    db_session,
):
    db = db_session
    anchor = await assertion(db)
    job = AsyncJob(
        kind="managed_original_verification", status="completed", payload={}, result={}
    )
    db.add(job)
    await db.flush()
    row = verification(
        anchor, async_job_id=job.id, status="completed", **terminal_fields("completed")
    )
    db.add(row)
    await db.flush()
    for fields in (
        {"outcome": "changed"},
        {"operation_id": None},
        {"observation": {"rewritten": True}},
        {"status": "pending"},
    ):
        with pytest.raises(IntegrityError, match="history is immutable"):
            async with db.begin_nested():
                await db.execute(
                    update(Verification)
                    .where(Verification.id == row.id)
                    .values(**fields)
                )
    receipt = row.operation_id
    await db.execute(delete(AsyncJob).where(AsyncJob.id == job.id))
    await db.refresh(row)
    assert (
        row.async_job_id is None
        and row.operation_id == receipt
        and row.outcome == "matched"
    )
