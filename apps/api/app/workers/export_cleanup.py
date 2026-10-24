"""Retire expired, independently owned exports with bounded durable cleanup."""

from __future__ import annotations

import asyncio
import logging

from app.services import export_media_retirement, export_outputs
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.workers.export_cleanup.purge_expired_export_artifacts")
def purge_expired_export_artifacts() -> dict:
    return asyncio.run(_purge_async())


async def _purge_async() -> dict:
    async with task_session() as db:
        result = await export_outputs.cleanup(db)
        result["media_bundles"] = await export_media_retirement.cleanup(db)
    log.info("purge_expired_export_artifacts: %s", result)
    return result
