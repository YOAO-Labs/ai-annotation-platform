"""Retry only committed avatar retirements in their saved physical location."""

import asyncio

from app.services.avatar_uploads import cleanup
from app.workers._db import task_session
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.avatar_cleanup.purge_retired_avatars")
def purge_retired_avatars():
    return asyncio.run(_purge())


async def _purge():
    async with task_session() as db:
        return await cleanup(db)
