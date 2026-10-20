"""Existing scheduler adapter for the versioned durable mail executor."""

from __future__ import annotations

import asyncio

from app.services.identity_mail_v2_delivery import run_invitation_mail
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.identity_mail_v2_delivery.relay_invitation_mail")
def relay_invitation_mail() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    processed = 0
    async with task_session_factory() as session_factory:
        for _ in range(16):
            result = await run_invitation_mail(session_factory=session_factory)
            if result["state"] in {"idle", "remote", "unavailable", "stale"}:
                return {"processed": processed, "state": result["state"]}
            processed += 1
    return {"processed": processed, "state": "bounded"}
