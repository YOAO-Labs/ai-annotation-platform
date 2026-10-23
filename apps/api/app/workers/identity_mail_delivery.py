"""Existing-scheduler adapter for the independent credential-mail executor."""

from __future__ import annotations

import asyncio

from app.services.identity_mail_delivery import run_credential_mail
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.identity_mail_delivery.relay_credential_mail")
def relay_credential_mail() -> dict:
    return asyncio.run(_run())


async def _run() -> dict:
    processed = 0
    async with task_session_factory() as session_factory:
        for _ in range(16):
            result = await run_credential_mail(session_factory=session_factory)
            if result["state"] in {"idle", "remote", "unavailable", "stale"}:
                return {"processed": processed, "state": result["state"]}
            processed += 1
    return {"processed": processed, "state": "bounded"}
