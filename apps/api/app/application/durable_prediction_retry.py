"""Resume one admitted retry through its original scope and exact result receipt."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
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
from aap_inference.gpu.contracts import GPUArbiterDispatchError
from aap_inference.execution_pipeline import execute_task_pipeline

from aap_inference_contract.execution import (
    ModelExecutionError,
    freeze_json,
    thaw_json,
)
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


def retry_scope(attempt):
    return {
        "job_id": attempt.job_id,
        "actor_id": attempt.actor_id,
        "project_id": attempt.project_id,
        "request_digest": attempt.request_digest,
    }


def _model_execution_port(db):
    """Use the parent-composed, route/GPU-authorized execution owner."""
    from app.services.inference_model_execution import model_execution_port

    return model_execution_port(db)


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
    if not isinstance(first.result, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in first.result
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
    if first.meta is not None and not isinstance(first.meta, Mapping):
        raise ValueError("retry backend returned invalid metadata")
    return first


async def _retry_job_payload(db, attempt):
    from app.services import async_job as jobs

    job = await jobs.get_job(db, uuid.UUID(str(attempt.job_id)), populate_existing=True)
    payload = job.payload if job is not None and isinstance(job.payload, dict) else {}
    return job, payload


def _reference_retry_owner(db, attempt, proof, task):
    from app.services.prediction_references import ReferencePredictionJob
    from app.services.storage_location import api_node_id

    return ReferencePredictionJob(
        db,
        attempt.job_id,
        proof,
        project_id=attempt.project_id,
        actor_id=attempt.actor_id,
        task_ids=[task.id],
        batch_id=task.batch_id,
        scope=proof["execution_scope"],
        predict_mode="append",
        snapshot_id=uuid.UUID(str(proof["snapshot_id"])),
        snapshot_digest=proof["snapshot_digest"],
        node_id=api_node_id(),
        execution_unit=proof.get("execution_unit"),
        job_kind="prediction_retry",
    )


def _video_retry_owner(db, attempt, proof, task):
    from app.services.storage_location import api_node_id
    from app.services.video_prediction_references import VideoPredictionJob

    return VideoPredictionJob(
        db,
        attempt.job_id,
        proof,
        project_id=attempt.project_id,
        actor_id=attempt.actor_id,
        task_ids=[task.id],
        batch_id=task.batch_id,
        scope=proof["execution_scope"],
        predict_mode="append",
        snapshot_id=uuid.UUID(str(proof["snapshot_id"])),
        snapshot_digest=proof["snapshot_digest"],
        node_id=api_node_id(),
        celery_task_id=str(attempt.job_id),
        execution_unit=proof.get("execution_unit"),
        job_kind="prediction_retry",
    )


async def _video_pipeline_hooks(owner, task, db):
    async def before_dispatch(stage_number, dispatch):
        await db.commit()
        await owner.before_dispatch(task, dispatch, stage_id=stage_number)
        await db.commit()

    async def validate_results(stage_number, operation, context):
        from app.services.video_prediction_results import adapt_results

        await owner.check_execution(task.id, stage_number, operation)
        context = context or {}
        window = (
            (context.get("from_frame"), context.get("to_frame"))
            if "from_frame" in context or "to_frame" in context
            else None
        )
        direction = context.get("direction") or "forward"
        qualified = await adapt_results(
            operation.results,
            task.id,
            owner.video_probe(task.id),
            window=window,
            direction=direction,
        )
        await owner.register_results(
            task.id, stage_number, qualified, window=window, direction=direction
        )
        return qualified.results

    return before_dispatch, validate_results


async def _infer(execution, attempt, prepared, session_factory):
    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.prediction import remap_prediction_track_ids
    from app.services.storage import resolve_task_url

    if prepared.terminal_error is not None:
        raise prepared.terminal_error
    if attempt.task_id is None or attempt.ml_backend_id is None:
        raise ValueError("missing_task_or_backend")
    task_id = uuid.UUID(str(attempt.task_id))
    project_id = uuid.UUID(str(attempt.project_id))
    requested_backend_id = uuid.UUID(str(attempt.ml_backend_id))
    context = attempt.source_extra.get("request_context")
    context = context if isinstance(context, dict) else None
    owner = None
    try:
        async with session_factory() as db:
            task = await get_core_row(db, task_row, task_id)
            registry_reference = await InferenceTopologyData(db).registry_reference(
                requested_backend_id
            )
            if task is None or registry_reference is None:
                raise ValueError("missing_task_or_backend")
            _job, payload = await _retry_job_payload(db, attempt)
            image_proof = payload.get("prediction_authority")
            video_proof = payload.get("video_prediction_authority")
            if image_proof is not None and video_proof is not None:
                raise ValueError("retry authority is ambiguous")
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
            retained_inputs = None
            pipeline_kwargs = {}
            if image_proof is not None:
                owner = _reference_retry_owner(db, attempt, image_proof, task)
                await owner.authorize(db)
                # Source admission and snapshot transfer are independent I/O;
                # no business lock is held while the backend fetches the input.
                retained_inputs = await owner.open_inputs(task, session_factory)
                input_url = owner.resolve_url(task)
            elif video_proof is not None:
                owner = _video_retry_owner(db, attempt, video_proof, task)
                await owner.authorize(db)
                retained_inputs = await owner.open_inputs(task, session_factory)
                before_dispatch, validate_results = await _video_pipeline_hooks(
                    owner, task, db
                )
                pipeline_kwargs = {
                    "before_dispatch": before_dispatch,
                    "validate_results": validate_results,
                    "max_response_bytes": 64 * 1024**2,
                }
            snapshot_id = getattr(_job, "execution_snapshot_id", None)
            if snapshot_id is None:
                proof = image_proof or video_proof
                snapshot_id = proof.get("snapshot_id") if proof is not None else None
            if snapshot_id is None:
                stored = attempt.source_extra.get("execution_snapshot")
                snapshot_id = stored.get("id") if isinstance(stored, dict) else None
            snapshot_payload = None
            snapshot_digest = None
            if snapshot_id is not None:
                from app.services.prediction_execution import load_snapshot

                snapshot_payload, snapshot_digest = await load_snapshot(
                    db, snapshot_id, project_id
                )
                proof = image_proof or video_proof
                if (
                    proof is not None
                    and proof.get("snapshot_digest") is not None
                    and proof["snapshot_digest"] != snapshot_digest
                ):
                    raise ValueError("retry execution snapshot changed")
            if snapshot_payload is not None:
                from aap_inference.execution_pipeline import resolve_input_mode
                from app.services.prediction_execution import (
                    build_stage_context,
                    execution_summary,
                )

                ordered = sorted(
                    snapshot_payload["stages"], key=lambda item: int(item["stage"])
                )
                stage_modes = tuple(
                    "crop"
                    if stage.get("parent_stage") is None
                    else resolve_input_mode(stage)
                    for stage in ordered
                )
                stages = tuple(freeze_json(stage) for stage in ordered)
                stage_contexts = tuple(
                    None if built is None else freeze_json(built)
                    for built in (
                        build_stage_context(stage, snapshot_payload)
                        for stage in ordered
                    )
                )
                pipeline_kwargs["execution_snapshot_id"] = uuid.UUID(str(snapshot_id))
                pipeline_kwargs["stage_modes"] = stage_modes
            else:
                stages = (
                    freeze_json(
                        {
                            "stage": 0,
                            "parent_stage": None,
                            "ml_backend_id": str(requested_backend_id),
                            "model_id": (context or {}).get("model_id"),
                            "write": None,
                        }
                    ),
                )
                stage_contexts = (
                    freeze_json(context) if context is not None else None,
                )
                stage_modes = ("crop",)
            source_image = None
            if len(stages) > 1 and any(mode == "crop" for mode in stage_modes[1:]):
                if retained_inputs is not None and image_proof is not None:
                    load_image = getattr(retained_inputs, "load_image", None)
                    if load_image is not None:
                        source_image = load_image(task)
                if source_image is not None:
                    pipeline_kwargs["source_image"] = source_image
                if retained_inputs is not None and image_proof is not None:
                    upload_crop = getattr(retained_inputs, "upload_crop", None)
                    if upload_crop is not None:

                        class _RetainedCropPublisher:
                            def publish(self, _task_key, box_index, jpeg_bytes):
                                return upload_crop(task, box_index, jpeg_bytes)

                        pipeline_kwargs["crop_publisher"] = _RetainedCropPublisher()
            accepted_input_facts = (
                {
                    key: str(value) if isinstance(value, uuid.UUID) else value
                    for key, value in prepared.accepted_input.items()
                }
                if prepared.accepted_input is not None
                else None
            )
            model_port = _model_execution_port(db)
            try:
                output = await execute_task_pipeline(
                    str(task_id),
                    stages,
                    (model_port,) * len(stages),
                    stage_contexts,
                    source_url=input_url,
                    project_id=project_id,
                    owner_prefix=f"retry:{attempt.job_id}",
                    route_operation="prediction_retry",
                    correlation={
                        "job_id": str(attempt.job_id),
                        "failed_prediction_id": str(attempt.failed_prediction_id),
                    },
                    input_provenance=(
                        {"accepted_input": accepted_input_facts}
                        if accepted_input_facts is not None
                        else None
                    ),
                    **pipeline_kwargs,
                )
            finally:
                if source_image is not None:
                    source_image.close()
            if owner is not None and video_proof is not None:
                await owner.finish_source(task.id)
            if owner is not None and video_proof is None:
                # Recheck the retained source/credentials after inference and
                # before sealing the raw snapshot. The writer session re-opens
                # the same owner and issues the receipt it can actually apply.
                await owner.validate(task)
            first = _one_result(output.results, task_id)
            extra = dict(thaw_json(output.extra) if output.extra is not None else {})
            if snapshot_payload is not None:
                extra["execution_snapshot"] = {
                    "id": str(snapshot_id),
                    "digest": snapshot_digest,
                    "stages": thaw_json(execution_summary(stages)),
                }
            if accepted_input_facts is not None:
                extra["accepted_input"] = {
                    key: value
                    for key, value in accepted_input_facts.items()
                    if key != "project_id"
                }
            pipeline_errors = extra.get("pipeline", {}).get("stage_errors")
            if extra.get("pipeline", {}).get("partial"):
                error = ValueError("管线阶段仍有失败，已保留原失败记录和原候选结果")
                error.pipeline_errors = pipeline_errors or []
                raise error
            model_provenance = stages[0].get("frozen_model_ref") or (context or {}).get(
                "frozen_model_ref"
            )
            request = ApplyPredictionResultRequest(
                task_id=task_id,
                project_id=project_id,
                ml_backend_id=output.provenance.instance_id,
                ml_backend_pool_id=output.provenance.pool_id,
                result=remap_prediction_track_ids(thaw_json(first.result)),
                score=first.score,
                model_version=first.model_version,
                inference_time_ms=first.inference_time_ms,
                token_meta=thaw_json(first.meta) if first.meta is not None else None,
                write_scope=restore_write_scope(attempt.write_scope),
                pipeline_extra=extra or None,
                accepted_video_input=prepared.accepted_video_input,
                model_provenance=thaw_json(model_provenance),
            )
    finally:
        if owner is not None:
            await owner.close_inputs(task_id)
    raw_context = {
        "task_id": str(task_id),
        "total_cost": format(
            Decimal(str((first.meta or {}).get("total_cost") or 0)), "f"
        ),
        "pipeline_stages": {},
        "gpu_arbiter_failures": [],
    }
    await execution.capture(1, (request,), stage="raw", context=raw_context)
    # Temporary source verification failure cannot discard the completed model output.
    if owner is not None:
        return
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


async def _revalidate_retry_job(db, attempt):
    """Re-read the admitted Job and its accepted authority in the writer session."""
    from app.services import async_job as jobs

    job = await jobs.get_job(db, uuid.UUID(str(attempt.job_id)), populate_existing=True)
    payload = job.payload if job is not None and isinstance(job.payload, dict) else {}
    if (
        job is None
        or job.kind != "prediction_retry"
        or job.user_id != attempt.actor_id
        or job.status not in {"pending", "running"}
        or payload.get("cancel_requested")
        or payload.get("prediction_retry_request_digest") != attempt.request_digest
    ):
        raise ValueError("retry_job_not_writable")
    return job, payload


def _video_write_facts(request):
    return dict(
        task_id=request.task_id,
        project_id=request.project_id,
        ml_backend_id=request.ml_backend_id,
        result=request.result,
        score=request.score,
        model_version=request.model_version,
        inference_time_ms=request.inference_time_ms,
        token_meta=request.token_meta,
        source=request.source,
        pipeline_extra=request.pipeline_extra,
        ml_backend_pool_id=request.ml_backend_pool_id,
        model_provenance=request.model_provenance,
    )


async def _attach_input_receipts(db, attempt, payload, session_factory, requests):
    """Open the owner input and issue its receipt in this writer transaction.

    The receipt is issuer/session bound, so it is created here and never
    serialized into the batch-result snapshot or carried across a retry.
    """
    from dataclasses import replace

    image_proof = payload.get("prediction_authority")
    video_proof = payload.get("video_prediction_authority")
    if image_proof is None and video_proof is None:
        return requests, None, None
    if image_proof is not None and video_proof is not None:
        raise ValueError("retry authority is ambiguous")
    task_id = uuid.UUID(str(attempt.task_id))
    task = await get_core_row(db, task_row, task_id)
    if task is None:
        raise ValueError("retry_authority_task_unavailable")
    if image_proof is not None:
        owner = _reference_retry_owner(db, attempt, image_proof, task)
        await owner.authorize(db)
        await owner.open_inputs(task, session_factory)
        try:
            receipt = await owner.final_receipt(task)
        except BaseException:
            await owner.close_inputs(task_id)
            raise
        key = "original_receipt" if owner.is_managed(task_id) else "reference_receipt"
        return (
            tuple(replace(request, **{key: receipt}) for request in requests),
            owner,
            task_id,
        )
    owner = _video_retry_owner(db, attempt, video_proof, task)
    await owner.authorize(db)
    await owner.open_inputs(task, session_factory)
    try:
        input_receipt = await owner.final_receipt(task)
        result_receipts = await owner.qualify_native_results(
            task,
            input_receipt,
            [_video_write_facts(request) for request in requests],
        )
        prepared = tuple(
            replace(
                request,
                video_input_receipt=input_receipt,
                video_result_receipt=result_receipt,
            )
            for request, result_receipt in zip(requests, result_receipts, strict=True)
        )
    except BaseException:
        await owner.close_inputs(task_id)
        raise
    return prepared, owner, task_id


async def _apply_snapshot(execution, attempt, data, session_factory, result, requests):
    from app.services.prediction import PredictionService

    for index in range(3):
        async with session_factory() as db:
            owner = None
            task_id = None
            try:
                execution.ensure_owned()
                _job, payload = await _revalidate_retry_job(db, attempt)
                writes, owner, task_id = await _attach_input_receipts(
                    db, attempt, payload, session_factory, requests
                )
                await PredictionService(db).apply_prediction_result_bundle(
                    deepcopy(writes)
                )
                if owner is not None and payload.get("video_prediction_authority"):
                    # The video owner's credentials outlive inference; recheck
                    # them in the transaction that publishes the result.
                    await owner.recheck_credentials()
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
            finally:
                if owner is not None:
                    await owner.close_inputs(task_id)


async def _finish_failure(
    execution, attempt, data, session_factory, error, *, abandon=False
):
    from aap_preannotation_results.retry_contract import PredictionRetryFailure
    from aap_inference.gpu.contracts import gpu_arbiter_failure_record
    from app.services.labeling_write_guard import lock_labeling_write

    gpu_error = gpu_arbiter_failure_record(error)
    model_failure = error.failure if isinstance(error, ModelExecutionError) else None
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
    if model_failure is not None and model_failure.provenance is not None:
        context = {
            **context,
            "ml_backend_id": str(model_failure.provenance.instance_id),
            "ml_backend_pool_id": (
                str(model_failure.provenance.pool_id)
                if model_failure.provenance.pool_id is not None
                else None
            ),
        }
    code = (
        gpu_error["error_code"]
        if gpu_error is not None
        else model_failure.code
        if model_failure is not None
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
            pipeline_errors = getattr(error, "pipeline_errors", None)
            if pipeline_errors:
                from app.db.models.prediction import FailedPrediction

                failed = await db.get(
                    FailedPrediction,
                    attempt.failed_prediction_id,
                    populate_existing=True,
                )
                if failed is not None:
                    failed.extra = {
                        **(failed.extra or {}),
                        "last_pipeline_errors": pipeline_errors,
                    }
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

    if isinstance(
        error, (DataError, IntegrityError, GPUArbiterDispatchError, ModelExecutionError)
    ):
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
    code = (
        error.failure.code
        if isinstance(error, ModelExecutionError)
        else getattr(error, "code", None)
    )
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
        "content_binding_changed",
        "reference_retry_requires_original_binding",
        "prediction_original_requires_new_request",
        "prediction_video_requires_new_request",
        "prediction_reference_receipt_invalid",
        "prediction_original_receipt_invalid",
        "prediction_credentials_invalid",
        "prediction_authority_revoked",
        "api_key_authority_revoked",
        "retry_authority_task_unavailable",
        "retry_job_not_writable",
    }


async def _mark_retry_running(attempt, session_factory):
    """Move an admitted Job to running before any owner call reads its status."""
    from app.services import async_job as jobs

    async with session_factory() as db:
        job = await jobs.get_job(
            db, uuid.UUID(str(attempt.job_id)), populate_existing=True
        )
        if job is None:
            raise ValueError("retry_job_missing")
        if job.status == "pending":
            await jobs.mark_running(db, job.id, celery_task_id=job.celery_task_id)
            await db.commit()


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
            await _mark_retry_running(attempt, session_factory)
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
