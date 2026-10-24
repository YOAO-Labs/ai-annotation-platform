"""A late API broker response cannot replace a claimed export attempt."""

import asyncio
from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.exporting import media_jobs
from tests.test_export_media_bundle_models import owned_db as owned_db


async def seed(db, *, status="pending", task_id=None):
    await db.execute(
        text("""CREATE TABLE async_jobs (
            id uuid PRIMARY KEY, kind text NOT NULL, status text NOT NULL,
            celery_task_id text, updated_at timestamptz,
            completed_at timestamptz, error_message text
        )""")
    )
    identifier = uuid.uuid4()
    await db.execute(
        text("""INSERT INTO async_jobs(id,kind,status,celery_task_id,updated_at)
            VALUES (:id,'export',:status,:task_id,:updated)"""),
        {
            "id": identifier,
            "status": status,
            "task_id": task_id,
            "updated": datetime.now(timezone.utc),
        },
    )
    await db.commit()
    return identifier


@pytest.mark.parametrize("action", ["success", "failure"])
@pytest.mark.parametrize("task_id", [None, "first-broker-delivery"])
@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "cancelled"]
)
async def test_late_dispatch_response_only_updates_an_unassigned_pending_job(
    owned_db, action, task_id, status
):
    db = owned_db
    identifier = await seed(db, status=status, task_id=task_id)
    changed = (
        await media_jobs.record_dispatch(db, identifier, "late-broker-delivery")
        if action == "success"
        else await media_jobs.fail_dispatch(db, identifier)
    )
    await db.commit()
    row = (
        (
            await db.execute(
                text("SELECT * FROM async_jobs WHERE id=:id"), {"id": identifier}
            )
        )
        .mappings()
        .one()
    )
    eligible = status == "pending" and task_id is None
    assert changed is eligible
    assert row["status"] == ("failed" if eligible and action == "failure" else status)
    assert row["celery_task_id"] == (
        "late-broker-delivery" if eligible and action == "success" else task_id
    )
    if eligible and action == "failure":
        assert row["completed_at"] is not None
        assert row["error_message"] == "export_dispatch_failed"


async def test_worker_claim_lock_fences_both_late_success_and_late_failure(owned_db):
    db = owned_db
    identifier = await seed(db)
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as worker:
        await worker.execute(
            text("""UPDATE async_jobs SET status='running',celery_task_id='winner'
                WHERE id=:id"""),
            {"id": identifier},
        )
        # The actual claim is still uncommitted. Backfill skips its row lock,
        # rather than waiting and then changing the worker's durable identity.
        assert not await asyncio.wait_for(
            media_jobs.record_dispatch(db, identifier, "late"), timeout=1
        )
        assert not await asyncio.wait_for(
            media_jobs.fail_dispatch(db, identifier), timeout=1
        )
        await db.commit()
        await worker.commit()
    assert not await media_jobs.record_dispatch(db, identifier, "later")
    assert not await media_jobs.fail_dispatch(db, identifier)
    row = (
        await db.execute(
            text("SELECT status,celery_task_id FROM async_jobs WHERE id=:id"),
            {"id": identifier},
        )
    ).one()
    assert tuple(row) == ("running", "winner")
