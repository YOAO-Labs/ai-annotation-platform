"""Send Jobs messages on the platform's Celery broker."""

from __future__ import annotations

import asyncio

from kombu.exceptions import OperationalError
from yoao_jobs.queue_contract import (
    JobsQueueContractError,
    JobsQueueUnavailable,
    QueueCanvas,
    QueueDispatch,
    decode_queue_request,
    encode_queue_request,
)
from yoao_jobs.queue_runtime import publish_canvas

from app.application.preannotation_contract import BatchPredictionRequest


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
    if request.async_job_id is None:
        raise ValueError("batch prediction requires an admitted Job")
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


async def _send_while_fenced(call, /, *args, **kwargs) -> None:
    """Drain a cancelled send before releasing its fence; its outcome stays unknown."""
    sender = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        await asyncio.shield(sender)
    except asyncio.CancelledError:
        while not sender.done():
            try:
                await asyncio.shield(sender)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not sender.cancelled():
            sender.exception()
        raise


async def dispatch_job(
    *,
    task_name: str,
    execution_id: str,
    args: list,
    kwargs: dict | None = None,
    options: dict | None = None,
) -> str:
    """Publish one existing task with its stable execution ID and current Celery route."""
    from app.workers.celery_app import celery_app

    await _send_while_fenced(
        celery_app.send_task,
        task_name,
        args=args,
        kwargs=kwargs,
        task_id=execution_id,
        **({} if options is None else options),
    )
    return execution_id


async def publish_queue_envelope(envelope: dict) -> str:
    """Publish the owner-saved task or flat canvas without changing its IDs."""
    operation = decode_queue_request(encode_queue_request(envelope))
    try:
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
        from app.workers.celery_app import celery_app

        await _send_while_fenced(publish_canvas, celery_app, operation)
    except OperationalError:
        raise JobsQueueUnavailable() from None
    return operation.execution_id
