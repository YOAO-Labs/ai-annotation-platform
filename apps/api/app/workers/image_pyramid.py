"""Celery compatibility wakes for accepted Assets image jobs."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from app.workers.celery_app import celery_app
from app.workers._db import task_session_factory

logger = logging.getLogger(__name__)


async def _execute_image_media(job_id: str | None) -> bool:
    from app.services.assets_images import run_image_job

    async with task_session_factory() as factory:
        return await run_image_job(
            UUID(job_id) if job_id else None, session_factory=factory
        )


async def _reconcile_image_media() -> None:
    from app.services.assets_images import maintain_images

    async with task_session_factory() as factory:
        result = await maintain_images(session_factory=factory)
    if result is not None:
        execute_image_media.delay(None)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    queue="image-pyramid",
)
def execute_image_media(self, job_id: str | None = None) -> None:
    """Wake one already accepted job, or drain one item from the accepted pool."""
    try:
        if asyncio.run(_execute_image_media(job_id)):
            execute_image_media.delay(None)
    except Exception as exc:  # noqa: BLE001 - celery boundary hides source details
        logger.warning(
            "accepted image job wake deferred (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from None


@celery_app.task(bind=True, queue="cleanup")
def reconcile_image_media(self) -> None:
    """Run local Assets reconciliation; a selected remote owner polls itself."""
    try:
        asyncio.run(_reconcile_image_media())
    except Exception as exc:  # noqa: BLE001 - celery boundary hides source details
        logger.warning(
            "accepted image reconciliation deferred (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from None


@celery_app.task(bind=True, max_retries=0, queue="image-pyramid")
def generate_image_pyramid(
    self, owner_kind: str, owner_id: str, force: bool = False
) -> None:
    """Acknowledge only pre-cutover deliveries; owner IDs never admit new work."""
    logger.warning(
        "discarded legacy image-pyramid delivery after native cutover; "
        "drain it before migration 0220"
    )


@celery_app.task(bind=True, queue="cleanup")
def reconcile_image_pyramids(self) -> None:
    """Keep the old beat task name while delegating to native accepted state."""
    asyncio.run(_reconcile_image_media())
