"""Celery adapter for resumable owner-provided task creation capabilities."""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.config import settings
from app.services.task_creation_contract import TaskCreationFailure
from app.services.task_creation_jobs import TaskCreationJobs
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)


class RetryableTaskCreation(RuntimeError):
    pass


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        return bool(
            exc.connection_invalidated
            or sqlstate in {"55P03", "40001", "40P01", "57014"}
            or (sqlstate and sqlstate.startswith("08"))
        )
    return False


@celery_app.task(
    bind=True,
    name="app.workers.create_tasks.run_create_tasks",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=None,
)
def run_create_tasks(self, job_id: str) -> None:
    try:
        asyncio.run(
            _run_create_tasks(
                job_id=job_id,
                celery_task_id=getattr(self.request, "id", None),
            )
        )
    except RetryableTaskCreation as exc:
        countdown = min(30, 2 ** min(self.request.retries, 5))
        raise self.retry(exc=exc, countdown=countdown, max_retries=None) from exc


async def _run_create_tasks(*, job_id: str, celery_task_id: str | None) -> None:
    job_uuid = uuid.UUID(job_id)
    engine = create_async_engine(
        settings.database_url, echo=False, hide_parameters=True
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = None
    try:
        async with maker() as db:
            jobs = TaskCreationJobs(db)
            try:
                claim = await jobs.claim(job_uuid, celery_task_id or str(uuid.uuid4()))
                operation_id = claim.operation_id
                await db.commit()
                if claim.status in {"completed", "failed", "cancelled"}:
                    return
                if claim.cancel_requested:
                    await jobs.fail(job_uuid, operation_id, "creation_cancelled")
                    await db.commit()
                    return
                await DatasetTaskCreationWorkflow(db).resume_creation(operation_id)
                await jobs.finish(job_uuid, operation_id)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                if _retryable(exc):
                    raise RetryableTaskCreation(
                        "Task creation persistence is temporarily unavailable"
                    ) from exc
                if isinstance(exc, TaskCreationFailure):
                    code = exc.code
                else:
                    code = "creation_execution_failed"
                try:
                    await jobs.fail(job_uuid, operation_id, code)
                    await db.commit()
                except Exception as recording_error:
                    await db.rollback()
                    if _retryable(recording_error):
                        raise RetryableTaskCreation(
                            "Task creation failure could not yet be persisted"
                        ) from recording_error
                    log.exception(
                        "Could not record task creation failure for job %s", job_uuid
                    )
                if code in {"job_not_active", "job_cancelled", "creation_cancelled"}:
                    return
                raise
    finally:
        await engine.dispose()
