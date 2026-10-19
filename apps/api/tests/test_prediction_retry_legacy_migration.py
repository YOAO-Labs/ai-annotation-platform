"""Cold-upgrade legacy claim reclamation requires an explicit queue-drain gate."""

from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction
from tests.factory import create_project


@pytest.mark.asyncio
async def test_legacy_reclamation_refuses_active_jobs_and_requires_drain_ack(
    db_session, super_admin
):
    owner, _token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    legacy = FailedPrediction(
        project_id=project.id,
        error_type="TIMEOUT",
        message="lost legacy delivery",
        retry_count=2,
        extra={"retry_pending": True, "request_context": {"type": "text"}},
    )
    bound = FailedPrediction(
        project_id=project.id,
        error_type="TIMEOUT",
        message="bound claim",
        retry_count=1,
        extra={"retry_pending": True, "retry_job_id": str(uuid.uuid4())},
    )
    db_session.add_all([legacy, bound])
    await db_session.flush()
    source = (
        Path(__file__).resolve().parents[1]
        / "alembic/sql/0233_prediction_retry_state.sql"
    ).read_text()
    statement = next(
        part.strip()
        for part in source.split("-- aap-statement-break")
        if "DO $block$" in part
    )
    await db_session.execute(
        text("SELECT set_config('aap.prediction_retry_legacy_drained','false',true)")
    )
    with pytest.raises(DBAPIError, match="Stop and drain"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))
    await db_session.refresh(legacy)
    assert legacy.extra["retry_pending"] is True and legacy.retry_count == 2
    legacy_job = AsyncJob(
        kind="prediction_retry",
        project_id=project.id,
        user_id=owner.id,
        status="running",
        payload={"failed_prediction_id": str(legacy.id)},
    )
    db_session.add(legacy_job)
    await db_session.flush()
    await db_session.execute(
        text("SELECT set_config('aap.prediction_retry_legacy_drained','true',true)")
    )
    with pytest.raises(DBAPIError, match="Drain and settle"):
        async with db_session.begin_nested():
            await db_session.execute(text(statement))
    await db_session.refresh(legacy_job)
    legacy_job.status = "failed"
    await db_session.flush()
    await db_session.execute(text(statement))
    await db_session.refresh(legacy)
    await db_session.refresh(bound)
    assert (
        legacy.extra == {"request_context": {"type": "text"}}
        and legacy.retry_count == 2
    )
    assert bound.extra["retry_pending"] is True and bound.retry_count == 1
    await db_session.delete(legacy_job)
    await db_session.flush()
