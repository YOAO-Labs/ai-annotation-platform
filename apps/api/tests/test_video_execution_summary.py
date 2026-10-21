"""Owned PostgreSQL acceptance for idempotent tracker summary projection."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob
from app.services.async_job import ensure_video_tracker_summary
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_concurrent_redelivery_keeps_one_summary_and_never_resets_terminal_progress(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    job_id = uuid4()
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def ensure(kind="video_tracker"):
        async with sessions() as db:
            summary = await ensure_video_tracker_summary(
                db,
                tracker_job_id=job_id,
                kind=kind,
                user_id=None,
                project_id=None,
                payload={"video_tracker_job_id": str(job_id)},
                celery_task_id="owned-video-summary",
            )
            await db.commit()
            return summary.id, summary.status

    try:
        deliveries = await asyncio.gather(ensure(), ensure())
        assert deliveries == [(job_id, "pending")] * 2
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AsyncJob)
                    .where(AsyncJob.id == job_id)
                )
                == 1
            )
            await db.execute(
                update(AsyncJob)
                .where(AsyncJob.id == job_id)
                .values(status="completed", progress_pct=100)
            )
            await db.commit()
        assert await ensure() == (job_id, "completed")
        with pytest.raises(ValueError, match="identity conflict"):
            await ensure("video_correction")
        async with sessions() as db:
            summary = await db.get(AsyncJob, job_id)
            assert summary.kind == "video_tracker"
            assert summary.status == "completed"
            assert summary.progress_pct == 100
    finally:
        async with sessions() as db:
            await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
            await db.commit()
