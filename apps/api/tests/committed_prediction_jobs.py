"""Own a single prediction Job committed for independent test consumers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models.async_job import AsyncJob


@asynccontextmanager
async def committed_prediction_job_id(
    engine: AsyncEngine, kind: Literal["batch_predict", "prediction_retry"]
) -> AsyncIterator[UUID]:
    job_id = uuid4()
    try:
        yield job_id
    finally:
        # Consumers have exited. Run normal terminal/delete guards; never delete
        # execution history, retry receipts or any other Job in the project.
        owned = (AsyncJob.id == job_id) & (AsyncJob.kind == kind)
        async with engine.begin() as db:
            await db.execute(
                update(AsyncJob)
                .where(owned, AsyncJob.status.in_(("pending", "running")))
                .values(status="cancelled", completed_at=func.now())
            )
            await db.execute(delete(AsyncJob).where(owned))
