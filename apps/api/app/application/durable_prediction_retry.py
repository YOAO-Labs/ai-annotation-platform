"""Resume one admitted retry through its original scope and exact result receipt."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import math
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.application.preannotation_results import (
    BatchResultExecution,
    PreannotationResultDeferred,
    _json_text,
    restore_write_scope,
)
from app.application.prediction_retry_inputs import (
    FrozenInputRetryDeferred,
    _INPUT_WAIT_CODES,
    _VIDEO_WAIT_CODES,
    _UNPREPARED_RETRY_INPUT,
    _prepare_retry_input,
    _verify_retry_source,
    _verify_video_retry_source,
)
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultRequest,
)
from app.services.prediction_retry_data import (
    IndependentPredictionRetryData,
    SqlAlchemyPredictionRetryData,
)
from app.services.raster_mask_contract import RasterMaskContractError
from app.services.gpu_arbitration.contracts import GPUArbiterDispatchError
from app.services.labeling_core_read_relations import get_core_row, task_row

_DEFER_CODES = frozenset(
    {
        "execution_busy",
        "lease_lost",
        "storage_unavailable",
        "invalid_response",
        "content_deleting",
        "content_epoch_changed",
    }
)


class _ModelRequestFailure(RuntimeError):
    code = "model_request_failed"


def retry_scope(attempt):
    return {
        "job_id": attempt.job_id,
        "actor_id": attempt.actor_id,
        "project_id": attempt.project_id,
        "request_digest": attempt.request_digest,
    }


async def call_retry(database, operation, **values):
    from aap_preannotation_results import retry_contract as c

    request = c.OPERATIONS[operation].request.model_validate_json(_json_text(values))
    try:
        return await database.call(operation, request)
    except c.PredictionRetryFailure as error:
        if error.code in _DEFER_CODES:
            raise PreannotationResultDeferred(error.code) from None
        raise


async def read_retry_attempt(database, *, job_id, failed_id, actor_id):
    result = await call_retry(
        database,
        "read_prediction_retry",
        job_id=job_id,
        failed_prediction_id=failed_id,
        actor_id=actor_id,
    )
    return result.attempt


async def _terminal_outcome(data, attempt):
    current = await read_retry_attempt(
        data,
        job_id=attempt.job_id,
        failed_id=attempt.failed_prediction_id,
        actor_id=attempt.actor_id,
    )
    return (
        current.outcome
        if current.state in {"succeeded", "failed", "abandoned"}
        else None
    )


async def _notify_terminal(db, job_id):
    from app.services import async_job as async_job_svc
    from app.services.async_job_notify import notify_job_terminal

    await async_job_svc.get_job(db, job_id, populate_existing=True)
    await notify_job_terminal(db, job_id=job_id)


async def _start_member(execution, attempt, session_factory):
    from app.services.labeling_write_guard import lock_labeling_write
    from app.services.notification import NotificationService

    page = await execution.call(
        "read_batch_result_execution",
        leased=False,
        after_ordinal=0,
        limit=1,
    )
    if not page.members:
        raise ValueError("retry task is unavailable")
    notify = page.members[0].state == "planned"
    async with session_factory() as db:
        await lock_labeling_write(
            db,
            restore_write_scope(attempt.write_scope),
            operations=("prediction_retry",),
        )
        await execution.call("start_batch_result_member", db=db, ordinal=1)
        pending = None
        service = NotificationService(db)
        if notify:
            pending = await service.notify(
                user_id=attempt.actor_id,
                type="failed_prediction.retry.started",
                target_type="failed_prediction",
                target_id=attempt.failed_prediction_id,
                payload={
                    "project_id": str(attempt.project_id),
                    "retry_job_id": str(attempt.job_id),
                },
            )
        await db.commit()
        if pending is not None:
            await service.publish_committed([pending])


def _one_result(results, task_id):
    if len(results) != 1:
        raise ValueError("retry backend must return exactly one target result")
    first = results[0]
    try:
        returned = uuid.UUID(str(first.task_id))
    except (AttributeError, ValueError, TypeError):
        raise ValueError("retry backend returned an invalid task identity") from None
    if returned != task_id:
        raise ValueError("retry backend returned another task's result")
    if not isinstance(first.result, list) or any(
        not isinstance(item, dict) for item in first.result
    ):
        raise ValueError("retry backend returned invalid result objects")
    if first.score is not None and (
        type(first.score) not in {int, float} or not math.isfinite(first.score)
    ):
        raise ValueError("retry backend returned an invalid score")
    if first.model_version is not None and not isinstance(first.model_version, str):
        raise ValueError("retry backend returned an invalid model version")
    if first.inference_time_ms is not None and (
        type(first.inference_time_ms) is not int or first.inference_time_ms < 0
    ):
        raise ValueError("retry backend returned an invalid inference duration")
    if first.meta is not None and not isinstance(first.meta, dict):
        raise ValueError("retry backend returned invalid metadata")
    return first


async def _infer(execution, attempt, prepared, session_factory):
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
    from app.services.ml_routing.client import RoutedMLBackendClient
    from app.services.prediction import remap_prediction_track_ids
    from app.services.storage import resolve_task_url

    if prepared.terminal_error is not None:
        raise prepared.terminal_error
    if attempt.task_id is None or attempt.ml_backend_id is None:
        raise ValueError("missing_task_or_backend")
    context = attempt.source_extra.get("request_context")
    context = context if isinstance(context, dict) else None
    async with session_factory() as db:
        task = await get_core_row(db, task_row, attempt.task_id)
        backend = await db.get(MLBackendRegistry, attempt.ml_backend_id)
        if task is None or backend is None:
            raise ValueError("missing_task_or_backend")
        client = RoutedMLBackendClient(
            db,
            backend,
            project_id=attempt.project_id,
            owner=f"retry:{attempt.job_id}",
            operation="prediction_retry",
            shadow_session_factory=session_factory,
            dispatch_context_factory=build_gpu_dispatch_context_factory(
                session_factory
            ),
        )
        input_url = (
            prepared.reference.url
            if prepared.reference is not None
            else resolve_task_url(
                SimpleNamespace(
                    file_path=attempt.write_scope.source_file_path,
                    dataset_item_id=attempt.write_scope.source_dataset_item_id,
                )
            )
        )
        task_input = {"id": str(attempt.task_id), "file_path": input_url}
        try:
            results = await client.predict(
                [task_input], **({"context": context} if context is not None else {})
            )
        except GPUArbiterDispatchError:
            raise
        except HTTPException as error:
            # A model response is an attempted inference, even when its HTTP
            # status also describes a temporary source/provider outage.
            raise _ModelRequestFailure(str(error)) from error
        first = _one_result(results, attempt.task_id)
        request = ApplyPredictionResultRequest(
            task_id=attempt.task_id,
            project_id=attempt.project_id,
            ml_backend_id=client.last_instance_id or attempt.ml_backend_id,
            ml_backend_pool_id=client.pool_id,
            result=remap_prediction_track_ids(first.result),
            score=first.score,
            model_version=first.model_version,
            inference_time_ms=first.inference_time_ms,
            token_meta=first.meta,
            write_scope=restore_write_scope(attempt.write_scope),
            pipeline_extra={
                "accepted_input": {
                    key: str(value) if isinstance(value, uuid.UUID) else value
                    for key, value in prepared.accepted_input.items()
                    if key != "project_id"
                }
            }
            if prepared.accepted_input is not None
            else None,
            accepted_video_input=prepared.accepted_video_input,
            model_provenance=(context or {}).get("frozen_model_ref"),
        )
    raw_context = {
        "task_id": str(attempt.task_id),
        "total_cost": format(
            Decimal(str((first.meta or {}).get("total_cost") or 0)), "f"
        ),
        "pipeline_stages": {},
        "gpu_arbiter_failures": [],
    }
    await execution.capture(1, (request,), stage="raw", context=raw_context)
    # Temporary source verification failure cannot discard the completed model output.
    if prepared.accepted_video_failure is not None:
        await _verify_video_retry_source(
            prepared.gateway, prepared.request, expected_plan=prepared.reference.plan
        )
    elif prepared.accepted_input is not None:
        await _verify_retry_source(prepared.gateway, prepared.request)


async def _prepare_snapshot(execution, session_factory):
    from dataclasses import replace
    from app.services.batch_result_masks import normalize_batch_prediction_masks
    from app.services.prediction import to_video_bbox_result

    saved = (await execution.read_result(1)).result
    if saved.state == "capturing":
        await execution.seal(1, stage="raw")
    if not saved.prepared_complete:
        _raw, requests = await execution.load(1, stage="raw")
        requests = tuple(
            replace(
                request,
                result=to_video_bbox_result(
                    request.result, request.accepted_video_input.entry.frame_index
                ),
            )
            if request.accepted_video_input is not None
            else request
            for request in requests
        )
        async with session_factory() as db:
            requests = await normalize_batch_prediction_masks(db, requests)
            await db.commit()
        await execution.capture(1, requests, stage="prepared", context=saved.context)
    elif saved.state != "ready":
        await execution.seal(1, stage="prepared")
    return await execution.load(1, stage="prepared")


async def _apply_snapshot(execution, attempt, data, session_factory, result, requests):
    from app.services.prediction import PredictionService

    for index in range(3):
        async with session_factory() as db:
            try:
                await PredictionService(db).apply_prediction_result_bundle(
                    deepcopy(requests)
                )
                finished = await call_retry(
                    SqlAlchemyPredictionRetryData(db, writer=True),
                    "finish_prediction_retry_write",
                    **execution._values(),
                    result_id=result.result_id,
                    prepared_digest=result.prepared_digest,
                )
                await _notify_terminal(db, attempt.job_id)
                await db.commit()
                return finished.outcome
            except ApplyPredictionResultError as error:
                await db.rollback()
                if not error.retryable:
                    raise
                if index == 2:
                    raise PreannotationResultDeferred(error.code) from None
                await asyncio.sleep(0.05 * 2**index)
            except (SQLAlchemyError, PreannotationResultDeferred) as error:
                await db.rollback()
                outcome = await _terminal_outcome(data, attempt)
                if outcome is not None:
                    return outcome
                if not _transient(error):
                    raise
                raise PreannotationResultDeferred("storage_unavailable") from None


async def _finish_failure(
    execution, attempt, data, session_factory, error, *, abandon=False
):
    from aap_preannotation_results.retry_contract import PredictionRetryFailure
    from app.services.gpu_arbitration.contracts import gpu_arbiter_failure_record
    from app.services.labeling_write_guard import lock_labeling_write

    gpu_error = gpu_arbiter_failure_record(error)
    saved = (
        (await execution.read_result(1)).result if attempt.task_id is not None else None
    )
    context = (
        saved.context
        if saved is not None
        else {
            "task_id": str(attempt.task_id) if attempt.task_id is not None else None,
            "total_cost": "0",
            "pipeline_stages": {},
            "gpu_arbiter_failures": [],
        }
    )
    if gpu_error is not None:
        context = {**context, "gpu_arbiter_failures": [gpu_error]}
    code = (
        gpu_error["error_code"]
        if gpu_error is not None
        else getattr(error, "code", None) or type(error).__name__
    )
    async with session_factory() as db:
        try:
            if attempt.write_scope is not None and not abandon:
                await lock_labeling_write(
                    db,
                    restore_write_scope(attempt.write_scope),
                    operations=("prediction_retry",),
                )
            finished = await call_retry(
                SqlAlchemyPredictionRetryData(db, writer=True),
                "finish_prediction_retry_failure",
                **execution._values(),
                abandon=abandon,
                error_code=code,
                reason=gpu_error["message"]
                if gpu_error is not None
                else "prediction_result_invalid"
                if isinstance(error, SQLAlchemyError)
                else str(error)[:200],
                gpu_arbiter_error=gpu_error,
                context=context,
            )
            await _notify_terminal(db, attempt.job_id)
            await db.commit()
            return finished.outcome
        except HTTPException as invalidation:
            await db.rollback()
            if _transient(invalidation):
                raise PreannotationResultDeferred("execution_busy") from None
            return await _finish_failure(
                execution, attempt, data, session_factory, invalidation, abandon=True
            )
        except PredictionRetryFailure as rejected:
            await db.rollback()
            outcome = await _terminal_outcome(data, attempt)
            if outcome is not None:
                return outcome
            if not abandon and _invalidated(rejected):
                return await _finish_failure(
                    execution, attempt, data, session_factory, rejected, abandon=True
                )
            raise PreannotationResultDeferred("storage_unavailable") from None
        except (SQLAlchemyError, PreannotationResultDeferred):
            await db.rollback()
            outcome = await _terminal_outcome(data, attempt)
            if outcome is not None:
                return outcome
            raise PreannotationResultDeferred("storage_unavailable") from None


def _transient(error):
    from sqlalchemy.exc import DataError, IntegrityError

    if isinstance(error, (DataError, IntegrityError, GPUArbiterDispatchError)):
        return False
    if isinstance(
        error, (SQLAlchemyError, FrozenInputRetryDeferred, PreannotationResultDeferred)
    ):
        return True
    if (
        getattr(error, "code", None)
        in _INPUT_WAIT_CODES | _VIDEO_WAIT_CODES | _DEFER_CODES
    ):
        return True
    if isinstance(error, (HTTPException, ApplyPredictionResultError)):
        return (
            error.status_code >= 500
            or getattr(error, "retryable", False)
            or (
                isinstance(error.detail, dict)
                and str(error.detail.get("reason", "")).endswith("_busy")
            )
        )
    return isinstance(error, RasterMaskContractError) and bool(
        error.detail.get("retryable")
    )


def _invalidated(error):
    code = getattr(error, "code", None)
    if isinstance(error, (RasterMaskContractError, GPUArbiterDispatchError)):
        return False
    if isinstance(error, HTTPException):
        if error.status_code in {401, 403, 404, 423}:
            return True
        if isinstance(error.detail, dict):
            code = error.detail.get("reason") or error.detail.get("code")
    return code in {
        "dismissed",
        "scope_mismatch",
        "job_not_writable",
        "result_conflict",
        "source_changed",
        "task_scope_changed",
        "input_scope_mismatch",
        "not_found",
        "permission_denied",
        "account_not_active",
        "labeling_authentication_required",
        "labeling_permission_changed",
        "labeling_target_not_found",
        "labeling_write_scope_mismatch",
        "labeling_write_scope_unknown",
        "prediction_source_changed",
        "prediction_task_scope_changed",
        "prediction_execution_scope_mismatch",
        "prediction_execution_not_writable",
        "prediction_input_manifest_origin_mismatch",
        "prediction_video_manifest_origin_mismatch",
        "accepted_input_unavailable",
        "accepted_video_input_unavailable",
    }


async def run_durable_prediction_retry(
    session_factory,
    attempt,
    *,
    execution_id,
    executor_session_factory=None,
    prepared_input=_UNPREPARED_RETRY_INPUT,
    prepare_input=_prepare_retry_input,
):
    if attempt.state in {"succeeded", "failed", "abandoned"}:
        return attempt.outcome
    data = IndependentPredictionRetryData(executor_session_factory or session_factory)
    execution = BatchResultExecution(
        None,
        session_factory,
        execution_id=execution_id,
        executor_session_factory=executor_session_factory,
        scope=retry_scope(attempt),
    )
    try:
        try:
            await execution.claim()
        except Exception:
            outcome = await _terminal_outcome(data, attempt)
            if outcome is not None:
                return outcome
            raise
        try:
            saved = (
                (await execution.read_result(1)).result
                if attempt.task_id is not None
                else None
            )
            if saved is None or not saved.raw_complete:
                if (
                    attempt.task_id is None
                    or attempt.ml_backend_id is None
                    or attempt.write_scope is None
                ):
                    raise ValueError("missing_task_or_backend")
                if prepared_input is _UNPREPARED_RETRY_INPUT:
                    async with prepare_input(
                        session_factory,
                        str(attempt.failed_prediction_id),
                        str(attempt.actor_id),
                        frozen_attempt=attempt,
                    ) as prepared:
                        if prepared.terminal_error is not None:
                            raise prepared.terminal_error
                        await execution.call("start_batch_result_execution")
                        await _start_member(execution, attempt, session_factory)
                        await _infer(execution, attempt, prepared, session_factory)
                else:
                    if prepared_input.terminal_error is not None:
                        raise prepared_input.terminal_error
                    await execution.call("start_batch_result_execution")
                    await _start_member(execution, attempt, session_factory)
                    await _infer(execution, attempt, prepared_input, session_factory)
            result, requests = await _prepare_snapshot(execution, session_factory)
            return await _apply_snapshot(
                execution, attempt, data, session_factory, result, requests
            )
        except Exception as error:
            if _transient(error):
                code = getattr(error, "code", None)
                if isinstance(error, PreannotationResultDeferred):
                    code = str(error)
                if (
                    not isinstance(code, str)
                    or not code.isascii()
                    or len(code) > 80
                    or not all(char.islower() or char == "_" for char in code)
                ):
                    code = "storage_unavailable"
                raise PreannotationResultDeferred(code) from None
            from aap_preannotation_results.contract import BatchResultFailure
            from aap_preannotation_results.retry_contract import PredictionRetryFailure

            if isinstance(error, (BatchResultFailure, PredictionRetryFailure)):
                outcome = await _terminal_outcome(data, attempt)
                if outcome is not None:
                    return outcome
                if not _invalidated(error):
                    raise PreannotationResultDeferred("storage_unavailable") from None
            return await _finish_failure(
                execution,
                attempt,
                data,
                session_factory,
                error,
                abandon=_invalidated(error),
            )
    finally:
        await execution.close()
