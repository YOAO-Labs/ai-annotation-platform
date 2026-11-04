"""Celery delivery for the Assets-owned video media executor."""

import asyncio
from uuid import UUID

from app.services.assets_media import maintain_video_media, run_media_job
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


async def _run_video_media(job_id: str | None) -> bool:
    async with task_session_factory() as factory:
        return await run_media_job(
            UUID(job_id) if job_id is not None else None,
            session_factory=factory,
        )


async def _reconcile_video_media() -> bool:
    async with task_session_factory() as factory:
        return await maintain_video_media(session_factory=factory) is not None


@celery_app.task(name="app.workers.assets_media.run_video_media", queue="media")
def run_video_media(job_id: str | None = None) -> None:
    """Execute one accepted local job and wake the next; remote owners poll themselves."""
    if asyncio.run(_run_video_media(job_id)):
        run_video_media.delay(None)


@celery_app.task(name="app.workers.assets_media.reconcile_video_media", queue="media")
def reconcile_video_media() -> None:
    """Reconcile local Assets cache ownership; remote providers maintain themselves."""
    if asyncio.run(_reconcile_video_media()):
        run_video_media.delay(None)
