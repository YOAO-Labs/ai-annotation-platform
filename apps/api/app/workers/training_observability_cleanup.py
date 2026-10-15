"""Prune raw observation windows while preserving compact cursors and summaries."""

import asyncio

from app.services.training_observability import cleanup
from app.workers._db import task_session
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.training_observability_cleanup.purge_observations")
def purge_observations():
    return asyncio.run(_purge())


async def _purge():
    async with task_session() as db:
        result = await cleanup(db)
        await db.commit()
        return result
