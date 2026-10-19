"""Publish durable retry deliveries while retaining uncertain broker outcomes."""

from __future__ import annotations

import logging
import uuid

from app.application.durable_prediction_retry import call_retry
from app.services.prediction_retry_data import (
    IndependentPredictionRetryData,
    retry_executor_sessions,
)

log = logging.getLogger(__name__)


async def publish_prediction_retry_deliveries(
    session_factory,
    dispatcher,
    *,
    job_id=None,
    limit=100,
    worker_id=None,
):
    data = IndependentPredictionRetryData(session_factory)
    claimed = await call_retry(
        data,
        "claim_prediction_retry_deliveries",
        worker_id=worker_id or uuid.uuid4(),
        job_id=job_id,
        limit=limit,
        lease_seconds=30,
    )
    submitted = pending = 0
    for delivery in claimed.items:
        try:
            if delivery.execution_queue is None:
                dispatcher.dispatch_job_item_retry(
                    delivery.failed_prediction_id,
                    delivery.actor_id,
                    job_id=delivery.job_id,
                )
            else:
                dispatcher.dispatch_prediction_retry(
                    delivery.failed_prediction_id,
                    delivery.actor_id,
                    execution_queue=delivery.execution_queue,
                    job_id=delivery.job_id,
                )
            accepted = True
        except Exception:
            # Publication may already have reached the broker. Keep the stable
            # task identity and durable outbox instead of clearing the claim.
            accepted = False
        try:
            await call_retry(
                data,
                "finish_prediction_retry_delivery",
                job_id=delivery.job_id,
                generation=delivery.generation,
                lease_token=delivery.lease_token,
                submitted=accepted,
                error_code=None if accepted else "publication_unavailable",
            )
        except Exception:
            # Reclaim after lease expiry can republish the same task UUID.
            log.warning("Retry publication acknowledgement remains pending")
        submitted += int(accepted)
        pending += int(not accepted)
    return {"submitted": submitted, "pending": pending}


async def publish_admitted_retry(session_factory, dispatcher, job_id):
    try:
        async with retry_executor_sessions(session_factory) as executor:
            await publish_prediction_retry_deliveries(
                executor,
                dispatcher,
                job_id=job_id,
                limit=1,
            )
    except Exception:
        log.warning("Admitted retry remains in the durable dispatch queue")
