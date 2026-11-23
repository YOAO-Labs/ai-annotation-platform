"""Deliver admitted retries through the durable result and publication ledgers."""

from __future__ import annotations

import asyncio
import uuid

from app.application.durable_prediction_retry import (
    read_retry_attempt,
    run_durable_prediction_retry,
)
from app.application.preannotation_results import PreannotationResultDeferred
from app.application.prediction_retry_inputs import (
    FrozenInputRetryDeferred as FrozenInputRetryDeferred,
    _UNPREPARED_RETRY_INPUT,
    _prepare_retry_input as _prepare_retry_input,
)
from app.services.prediction_retry_data import (
    IndependentPredictionRetryData,
    retry_executor_sessions,
)
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="app.workers.predictions_retry.retry_failed_prediction",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=None,
    default_retry_delay=30,
)
def retry_failed_prediction(self, failed_id: str, user_id: str, *, job_id: str) -> dict:
    try:
        return asyncio.run(
            _run_retry(
                failed_id,
                user_id,
                celery_task_id=getattr(self.request, "id", None),
                job_id=job_id,
            )
        )
    except (FrozenInputRetryDeferred, PreannotationResultDeferred) as error:
        raise self.retry(exc=error, countdown=30, max_retries=None) from error


async def _run_retry(
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
    *,
    job_id: str,
) -> dict:
    async with task_session_factory() as factory:
        async with retry_executor_sessions(factory) as executor:
            return await _do_retry_with_factory(
                factory,
                failed_id,
                user_id,
                celery_task_id=celery_task_id,
                job_id=job_id,
                executor_session_factory=executor,
            )


async def _do_retry_with_factory(
    session_factory,
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
    *,
    job_id: str,
    executor_session_factory=None,
    prepared_input=_UNPREPARED_RETRY_INPUT,
) -> dict:
    fid, actor = uuid.UUID(failed_id), uuid.UUID(user_id)
    identity = uuid.UUID(str(job_id))
    attempt = await read_retry_attempt(
        IndependentPredictionRetryData(executor_session_factory or session_factory),
        job_id=identity,
        failed_id=fid,
        actor_id=actor,
    )
    return await run_durable_prediction_retry(
        session_factory,
        attempt,
        execution_id=celery_task_id or str(identity),
        executor_session_factory=executor_session_factory,
        prepared_input=prepared_input,
        prepare_input=_prepare_retry_input,
    )


@celery_app.task(
    name="app.workers.predictions_retry.relay_prediction_retry_deliveries",
    queue="maintenance",
    max_retries=0,
)
def relay_prediction_retry_deliveries() -> dict[str, int]:
    return asyncio.run(_relay_pending_retries())


async def _relay_pending_retries():
    from app.application.prediction_retry_delivery import (
        publish_prediction_retry_deliveries,
    )
    from app.workers.prediction_retry_dispatch import CeleryPredictionRetryDispatcher

    dispatcher = CeleryPredictionRetryDispatcher()
    async with task_session_factory() as factory:
        async with retry_executor_sessions(factory) as executor:
            return await publish_prediction_retry_deliveries(executor, dispatcher)
