"""Bind cross-frame dispatch to the selected Jobs queue transport."""

from __future__ import annotations

import asyncio
from pathlib import Path

from aap_jobs.queue_client import JobsQueueClient, JobsQueueExecutionControl
from aap_jobs.queue_contract import (
    JobsQueueContractError,
    JobsQueueUnavailable,
)

from app.config import settings
from app.services.job_execution_control import JobExecutionControl

_MAX_TOKEN_BYTES = 4096


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
