"""Existing-scheduler adapter for the Notifications-owned durable publisher."""

from __future__ import annotations

import asyncio

from app.services.notification_delivery import run_identity_notifications
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.notification_delivery.relay_identity_notifications")
def relay_identity_notifications() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    async with task_session_factory() as session_factory:
        return await run_identity_notifications(session_factory=session_factory)
