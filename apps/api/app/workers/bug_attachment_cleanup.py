"""Retry committed bug attachment retirements at their saved physical location."""

import asyncio

from app.services.bug_attachment_uploads import cleanup
from app.workers._db import task_session
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.bug_attachment_cleanup.purge_bug_attachments")
def purge_bug_attachments():
    return asyncio.run(_purge())


async def _purge():
    async with task_session() as db:
        return await cleanup(db)
