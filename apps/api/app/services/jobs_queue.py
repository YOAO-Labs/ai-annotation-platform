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

from app.application.preannotation_contract import (
    BatchPredictionDispatcher,
    BatchPredictionRequest,
)
from app.config import settings
from app.services.job_execution_control import JobExecutionControl
from app.services.prediction_retry import PredictionRetryDispatcher
from app.services.quality_job_dispatch import QualityJobDispatcher, QualityRunKind
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher

_MAX_TOKEN_BYTES = 4096
_QUALITY_RUN_TASKS = {
    "mask_qc": "app.workers.mask_qc.run_mask_qc",
    "point_cloud_quality": "app.workers.point_cloud_quality.run",
    "video_track_quality": "app.workers.video_track_quality.run",
}
_BATCH_PREDICTION_TASK = "app.workers.tasks.batch_predict"
_PREDICTION_RETRY_TASK = "app.workers.predictions_retry.retry_failed_prediction"


def _batch_prediction_task_payload(
    request: BatchPredictionRequest,
) -> tuple[list, dict]:
    kwargs = {
        "prompt": request.prompt,
        "output_mode": request.output_mode,
        "batch_id": request.batch_id,
        "user_id": request.user_id,
        "params": request.params,
        "predict_mode": request.predict_mode,
        "model_id": request.model_id,
        "task": request.task,
        "prompt_type": request.prompt_type,
        "model_variants": request.model_variants,
        "class_filter": request.class_filter,
        "pipeline_stages": request.pipeline_stages,
        "execution_unit": request.execution_unit,
        "execution_scope": request.execution_scope,
    }
    if request.task_type is not None:
        kwargs["task_type"] = request.task_type
    if request.async_job_id is not None:
        kwargs["async_job_id"] = request.async_job_id
    if request.frozen_model_ref is not None:
        kwargs["frozen_model_ref"] = request.frozen_model_ref
    if request.accepted_inputs_protocol is not None:
        kwargs["accepted_inputs_protocol"] = request.accepted_inputs_protocol
    if request.accepted_input_ids is not None:
        kwargs["accepted_input_ids"] = request.accepted_input_ids
    if request.accepted_input_manifest_protocol is not None:
        kwargs["accepted_input_manifest_protocol"] = (
            request.accepted_input_manifest_protocol
        )
    if request.accepted_input_manifest_id is not None:
        kwargs["accepted_input_manifest_id"] = request.accepted_input_manifest_id
    if request.accepted_input_manifest_digest is not None:
        kwargs["accepted_input_manifest_digest"] = (
            request.accepted_input_manifest_digest
        )
    if request.accepted_video_inputs_protocol is not None:
        kwargs["accepted_video_inputs_protocol"] = (
            request.accepted_video_inputs_protocol
        )
    if request.accepted_video_manifest_id is not None:
        kwargs["accepted_video_manifest_id"] = request.accepted_video_manifest_id
    if request.accepted_video_selection_digest is not None:
        kwargs["accepted_video_selection_digest"] = (
            request.accepted_video_selection_digest
        )
    if request.accepted_video_selection is not None:
        kwargs["accepted_video_selection"] = request.accepted_video_selection
    return [request.project_id, request.ml_backend_id, request.task_ids], kwargs


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


async def publish_queue_envelope(envelope: dict) -> str:
    """Publish an already frozen task or flat canvas through the selected owner."""
    if settings.jobs_queue_url:
        try:
            return await JobsQueueClient(
                settings.jobs_queue_url, _jobs_queue_token()
            ).publish_envelope(envelope)
        except (JobsQueueContractError, JobsQueueUnavailable):
            raise
        except Exception:
            raise JobsQueueUnavailable() from None

    from aap_jobs.queue_contract import (
        QueueCanvas,
        QueueDispatch,
        decode_queue_request,
        encode_queue_request,
    )

    operation = decode_queue_request(encode_queue_request(envelope))
    if isinstance(operation, QueueDispatch):
        return await dispatch_job(
            task_name=operation.task_name,
            execution_id=operation.execution_id,
            args=operation.args,
            kwargs=operation.kwargs,
            options=operation.options,
        )
    if not isinstance(operation, QueueCanvas):
        raise JobsQueueContractError()
    from aap_jobs.queue_runtime import publish_canvas
    from app.workers.celery_app import celery_app

    await asyncio.to_thread(publish_canvas, celery_app, operation)
    return operation.execution_id


def dispatch_job_sync(
    *,
    task_name: str,
    execution_id: str,
    args: list,
    kwargs: dict | None = None,
    options: dict | None = None,
) -> str:
    """Publish synchronously through the selected Jobs queue owner only."""
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

    def dispatch_mask_repair(
        self, batch_id: uuid.UUID, *, rollback: bool, execution_id: str
    ) -> str:
        return dispatch_job_sync(
            task_name=(
                "app.workers.mask_repair.rollback_mask_repair"
                if rollback
                else "app.workers.mask_repair.run_mask_repair"
            ),
            execution_id=execution_id,
            args=[str(batch_id)],
        )

    def dispatch_mask_import(self, batch_id: uuid.UUID, *, execution_id: str) -> str:
        return dispatch_job_sync(
            task_name="app.workers.mask_format_import.run_mask_format_import",
            execution_id=execution_id,
            args=[str(batch_id)],
        )


class JobsQueueVideoTrackerDispatcher:
    __slots__ = ()

    def dispatch_tracker_job(self, job_id: uuid.UUID, *, execution_id: str) -> str:
        return dispatch_job_sync(
            task_name="app.workers.video_tracker.run_video_tracker_job",
            execution_id=execution_id,
            args=[str(job_id)],
        )


class JobsQueueBatchPredictionDispatcher:
    __slots__ = ()

    def dispatch(
        self,
        request: BatchPredictionRequest,
        *,
        queue: str,
        soft_time_limit: int | None = None,
    ) -> str:
        execution_id = request.async_job_id or str(uuid.uuid4())
        args, kwargs = _batch_prediction_task_payload(request)
        options = {"queue": queue}
        if soft_time_limit is not None:
            options["soft_time_limit"] = soft_time_limit
        return dispatch_job_sync(
            task_name=_BATCH_PREDICTION_TASK,
            execution_id=execution_id,
            args=args,
            kwargs=kwargs,
            options=options,
        )


class JobsQueuePredictionRetryDispatcher:
    __slots__ = ()

    def dispatch_prediction_retry(
        self,
        failed_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        execution_queue: str,
        job_id: uuid.UUID | None = None,
    ) -> None:
        execution_id = str(job_id or uuid.uuid4())
        kwargs = {"job_id": str(job_id)} if job_id is not None else None
        dispatch_job_sync(
            task_name=_PREDICTION_RETRY_TASK,
            execution_id=execution_id,
            args=[str(failed_id), str(actor_id)],
            kwargs=kwargs,
            options={"queue": execution_queue},
        )

    def dispatch_job_item_retry(
        self,
        failed_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None = None,
    ) -> None:
        execution_id = str(job_id or uuid.uuid4())
        kwargs = {"job_id": str(job_id)} if job_id is not None else None
        dispatch_job_sync(
            task_name=_PREDICTION_RETRY_TASK,
            execution_id=execution_id,
            args=[str(failed_id), str(actor_id)],
            kwargs=kwargs,
        )


def build_quality_job_dispatcher() -> QualityJobDispatcher:
    """Select the configured transport without loading worker tasks remotely."""
    if settings.jobs_queue_url:
        return JobsQueueQualityDispatcher()
    from app.workers.quality_dispatch import CeleryQualityJobDispatcher

    return CeleryQualityJobDispatcher()


def build_video_tracker_job_dispatcher() -> VideoTrackerJobDispatcher:
    """Select the tracker transport without loading worker tasks remotely."""
    if settings.jobs_queue_url:
        return JobsQueueVideoTrackerDispatcher()
    from app.workers.video_tracker_dispatch import CeleryVideoTrackerJobDispatcher

    return CeleryVideoTrackerJobDispatcher()


def build_batch_prediction_dispatcher() -> BatchPredictionDispatcher:
    """Select batch prediction transport without loading worker tasks remotely."""
    if settings.jobs_queue_url:
        return JobsQueueBatchPredictionDispatcher()
    from app.workers.batch_prediction_dispatch import CeleryBatchPredictionDispatcher

    return CeleryBatchPredictionDispatcher()


def build_prediction_retry_dispatcher() -> PredictionRetryDispatcher:
    """Select retry transport without loading worker tasks remotely."""
    if settings.jobs_queue_url:
        return JobsQueuePredictionRetryDispatcher()
    from app.workers.prediction_retry_dispatch import CeleryPredictionRetryDispatcher

    return CeleryPredictionRetryDispatcher()
