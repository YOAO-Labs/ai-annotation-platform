"""Deliver admitted retries through the durable result and publication ledgers."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.durable_prediction_retry import (
    call_retry,
    read_retry_attempt,
    run_durable_prediction_retry,
)
from app.services.project_access import get_workspace_project
from app.application.preannotation_results import PreannotationResultDeferred
from app.application.prediction_retry_inputs import (
    FrozenInputRetryDeferred as FrozenInputRetryDeferred,
    _UNPREPARED_RETRY_INPUT,
    _prepare_retry_input as _prepare_retry_input,
)
from app.services.prediction_retry_data import (
    IndependentPredictionRetryData,
    SqlAlchemyPredictionRetryData,
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
def retry_failed_prediction(
    self, failed_id: str, user_id: str, *, job_id: str | None = None
) -> dict:
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
    job_id: str | None = None,
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


def _legacy_job_id(celery_task_id):
    if celery_task_id is None:
        return uuid.uuid4()
    try:
        return uuid.UUID(celery_task_id)
    except ValueError:
        # Old non-UUID broker IDs must retain one identity across redelivery.
        return uuid.uuid5(
            uuid.NAMESPACE_URL, "aap:prediction-retry:legacy:" + celery_task_id
        )


async def _do_retry_with_factory(
    session_factory,
    failed_id: str,
    user_id: str,
    celery_task_id: str | None = None,
    *,
    job_id: str | None = None,
    executor_session_factory=None,
    prepared_input=_UNPREPARED_RETRY_INPUT,
) -> dict:
    fid, actor = uuid.UUID(failed_id), uuid.UUID(user_id)
    identity = (
        uuid.UUID(str(job_id)) if job_id is not None else _legacy_job_id(celery_task_id)
    )
    if job_id is None:
        from app.services import async_job as jobs

        async with session_factory() as db:
            admitted = await call_retry(
                SqlAlchemyPredictionRetryData(db, writer=True),
                "admit_prediction_retry",
                job_id=identity,
                failed_prediction_id=fid,
                actor_id=actor,
                execution_queue=None,
                legacy_delivery=True,
            )
            if admitted.created:
                _project_id, metadata = await _retry_job_metadata(db, fid)
                await jobs.update_payload(db, identity, metadata)
            await db.commit()
        attempt = admitted.attempt
    else:
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


async def _retry_job_metadata(
    db: AsyncSession, fid: uuid.UUID
) -> tuple[uuid.UUID | None, dict]:
    from app.services.labeling_core_read_relations import (
        failed_prediction_row,
        get_core_row,
        task_row,
    )
    from app.services.ml_backend import MLBackendService

    payload: dict = {"failed_prediction_id": str(fid)}
    fp = await get_core_row(db, failed_prediction_row, fid)
    if not fp:
        return None, payload

    payload.update(
        {
            "task_id": str(fp.task_id) if fp.task_id else None,
            "project_id": str(fp.project_id),
            "ml_backend_id": str(fp.ml_backend_id) if fp.ml_backend_id else None,
            "error_type": fp.error_type,
            "message": (fp.message or "")[:200],
        }
    )
    task = await get_core_row(db, task_row, fp.task_id) if fp.task_id else None
    if task is not None:
        payload["task_display_id"] = task.display_id
    project = await get_workspace_project(db, project_id=fp.project_id)
    if project is not None:
        payload["project_display_id"] = project.display_id
        payload["project_name"] = project.name
    registry = (
        await MLBackendService(db).get_registry(fp.ml_backend_id)
        if fp.ml_backend_id
        else None
    )
    if registry is not None:
        payload["ml_backend_name"] = registry.name
    return fp.project_id, payload


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
    from app.services.jobs_queue import build_prediction_retry_dispatcher

    dispatcher = build_prediction_retry_dispatcher()
    async with task_session_factory() as factory:
        async with retry_executor_sessions(factory) as executor:
            return await publish_prediction_retry_deliveries(executor, dispatcher)
