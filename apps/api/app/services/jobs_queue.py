"""Bind existing job producers to the selected Jobs queue transport."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from aap_jobs.queue_client import JobsQueueClient, JobsQueueExecutionControl
from aap_jobs.queue_contract import (
    JobsQueueContractError,
    JobsQueueUnavailable,
)

from app.config import settings
from app.services.job_execution_control import JobExecutionControl
from app.services.quality_job_dispatch import QualityJobDispatcher, QualityRunKind

_MAX_TOKEN_BYTES = 4096
_QUALITY_RUN_TASKS = {
    "mask_qc": "app.workers.mask_qc.run_mask_qc",
    "point_cloud_quality": "app.workers.point_cloud_quality.run",
    "video_track_quality": "app.workers.video_track_quality.run",
}


def _jobs_queue_token() -> str:
    token = settings.jobs_queue_token
    path = settings.jobs_queue_token_file
    if bool(token) == bool(path):
        raise JobsQueueUnavailable()
    if path:
        try:
            with Path(path).open("rb") as source:
                raw = source.read(_MAX_TOKEN_BYTES + 1)
            if len(raw) > _MAX_TOKEN_BYTES:
                raise ValueError
            token = raw.decode("utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            raise JobsQueueUnavailable() from None
    try:
        encoded = token.encode("ascii")
    except UnicodeError:
        raise JobsQueueUnavailable() from None
    if not encoded or any(value < 33 or value > 126 for value in encoded):
        raise JobsQueueUnavailable()
    return token


def build_job_execution_control() -> JobExecutionControl:
    """Use the same private broker owner for revoke when remote dispatch is enabled."""
    if not settings.jobs_queue_url:
        from app.workers.job_execution_control import CeleryJobExecutionControl

        return CeleryJobExecutionControl()
    try:
        return JobsQueueExecutionControl(settings.jobs_queue_url, _jobs_queue_token())
    except JobsQueueUnavailable:
        raise
    except Exception:
        raise JobsQueueUnavailable() from None


async def dispatch_job(
    *,
    task_name: str,
    execution_id: str,
    args: list,
    kwargs: dict | None = None,
    options: dict | None = None,
) -> str:
    """Publish one existing task with its stable execution ID and current Celery route."""
    if options is not None and (type(options) is not dict or options):
        raise JobsQueueContractError()
    if settings.jobs_queue_url:
        try:
            return await JobsQueueClient(
                settings.jobs_queue_url,
                _jobs_queue_token(),
            ).dispatch_job(
                task_name,
                execution_id,
                args,
                kwargs=kwargs,
                options=options,
            )
        except JobsQueueContractError:
            raise
        except JobsQueueUnavailable:
            raise
        except Exception:
            raise JobsQueueUnavailable() from None

    from app.workers.celery_app import celery_app

    await asyncio.to_thread(
        celery_app.send_task,
        task_name,
        args=args,
        kwargs=kwargs,
        task_id=execution_id,
        **({} if options is None else options),
    )
    return execution_id


def dispatch_job_sync(
    *,
    task_name: str,
    execution_id: str,
    args: list,
    kwargs: dict | None = None,
    options: dict | None = None,
) -> str:
    """Publish synchronously through the selected Jobs queue owner only."""
    if options is not None and (type(options) is not dict or options):
        raise JobsQueueContractError()
    if not settings.jobs_queue_url:
        raise JobsQueueUnavailable()
    try:
        return JobsQueueClient(
            settings.jobs_queue_url,
            _jobs_queue_token(),
        ).dispatch_job_sync(
            task_name,
            execution_id,
            args,
            kwargs=kwargs,
            options=options,
        )
    except JobsQueueContractError:
        raise
    except JobsQueueUnavailable:
        raise
    except Exception:
        raise JobsQueueUnavailable() from None


class JobsQueueQualityDispatcher:
    """Transport binding for quality runs, repair and staged-import jobs."""

    __slots__ = ()

    def dispatch_run(
        self, kind: QualityRunKind, run_id: uuid.UUID, *, execution_id: str
    ) -> None:
        try:
            task_name = _QUALITY_RUN_TASKS[kind]
        except (KeyError, TypeError):
            raise ValueError(f"Unsupported quality run kind: {kind}") from None
        dispatch_job_sync(
            task_name=task_name,
            execution_id=execution_id,
            args=[str(run_id)],
        )

    def dispatch_mask_repair(self, batch_id: uuid.UUID, *, rollback: bool) -> str:
        execution_id = str(uuid.uuid4())
        return dispatch_job_sync(
            task_name=(
                "app.workers.mask_repair.rollback_mask_repair"
                if rollback
                else "app.workers.mask_repair.run_mask_repair"
            ),
            execution_id=execution_id,
            args=[str(batch_id)],
        )

    def dispatch_mask_import(self, batch_id: uuid.UUID) -> str:
        execution_id = str(uuid.uuid4())
        return dispatch_job_sync(
            task_name="app.workers.mask_format_import.run_mask_format_import",
            execution_id=execution_id,
            args=[str(batch_id)],
        )


def build_quality_job_dispatcher() -> QualityJobDispatcher:
    """Select the configured transport without loading worker tasks remotely."""
    if settings.jobs_queue_url:
        return JobsQueueQualityDispatcher()
    from app.workers.quality_dispatch import CeleryQualityJobDispatcher

    return CeleryQualityJobDispatcher()
