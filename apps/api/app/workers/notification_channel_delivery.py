"""Celery task adapter for the external notification channel delivery worker."""

from __future__ import annotations

import asyncio

from app.services.notification_channel_delivery import (
    run_notification_channel_deliveries,
)
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


@celery_app.task(
    name="app.workers.notification_channel_delivery.relay_notification_channel_deliveries"
)
def relay_notification_channel_deliveries() -> dict[str, int]:
    return asyncio.run(_run())


async def _run() -> dict[str, int]:
    async with task_session_factory() as session_factory:
        return await run_notification_channel_deliveries(
            session_factory=session_factory
        )
