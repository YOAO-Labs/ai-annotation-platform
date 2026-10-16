"""Expire only owned preview prefixes after process exit and PUT-window closure."""

import asyncio

from app.services.training_previews import cleanup
from app.workers._db import task_session
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.training_preview_cleanup.purge_expired_previews")
def purge_expired_previews():
    return asyncio.run(_purge())


async def _purge():
    async with task_session() as db:
        result = await cleanup(db)
        await db.commit()
        return result
