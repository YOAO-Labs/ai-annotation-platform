"""Drain durable Identity deactivation pages using stable Celery execution IDs."""

from __future__ import annotations

import asyncio
import logging
import uuid

from aap_identity.deactivation_contract import decode_deactivation_due_request
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_deactivation_commands import (
    DeactivationOperatorBusy,
    IdentityDeactivationDueCommands,
)
from app.services.identity_gateway import get_identity_gateway
from app.workers._db import task_session
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.workers.deactivation.process_deactivation_requests",
    autoretry_for=(IdentityGatewayUnavailable, DeactivationOperatorBusy),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": None},
)
def process_deactivation_requests(self) -> dict:
    task_id = self.request.id
    try:
        run_id = uuid.UUID(task_id) if task_id else uuid.uuid4()
    except ValueError:
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"aap-deactivation-task:{task_id}")
    return asyncio.run(_process_async(run_id))


async def _process_async(run_id: uuid.UUID | None = None) -> dict:
    run_id = run_id or uuid.uuid4()
    processed = 0
    blocked = 0
    page = 0
    async with task_session() as db:
        operator = IdentityDeactivationDueCommands(db, get_identity_gateway())
        while True:
            request = decode_deactivation_due_request(
                {
                    "schema": "aap-identity-deactivation-due/v1",
                    "operation_id": str(uuid.uuid5(run_id, f"batch:{page}")),
                    "limit": 256,
                }
            )
            result = await operator.process_due_deactivations(request)
            processed += result.processed
            blocked += result.blocked_last_super_admin
            if not result.has_more or result.processed == 0:
                break
            page += 1
    log.info(
        "process_deactivation_requests done: processed=%d blocked_last_super_admin=%d",
        processed,
        blocked,
    )
    return {"processed": processed}
