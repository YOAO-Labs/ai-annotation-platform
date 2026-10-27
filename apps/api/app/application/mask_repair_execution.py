"""Application workflow for mask-repair execution and rollback."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

from aap_inference import extract_capabilities
from aap_inference.gpu.contracts import (
    GPUArbiterErrorCode,
    gpu_arbiter_failure_record,
)
from aap_labeling.video_control_contract import MarkEnqueued, ReadWorkerJob
from aap_inference_contract.execution import (
    FrozenModelOperation,
    ModelExecutionError,
    thaw_json,
)
from aap_protocol_v2 import MAX_MASK_RESPONSE_BYTES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_media_summary import AssetsTaskMediaSummaryReader
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    prediction_row,
    prediction_meta_row,
    task_row,
)
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorStatus
from app.schemas.video_tracker_job import VideoMaskCorrectionRequest
from app.services import async_job as async_job_svc
from app.services.mask_repair import (
    canonical_digest,
    execute_repair_shard,
    fail_repair_rollback,
    finish_repair_execution,
    finish_repair_rollback,
    load_repair_batch,
    record_repair_candidate,
    record_repair_rollback_shard,
    record_repair_shard_failure,
    rollback_repair_shard,
    store_repair_result_rles,
)
from app.services.content_snapshot import ContentError
from app.services.mask_repair_execution import (
    RepairAttempt,
    RepairAttemptStopped,
    lock_attempt,
    lock_batch,
    progress_counts,
    start_attempt,
)
from app.services.prediction_originals import settled
from app.services.reference_keys import is_reference
from app.services.reference_mask_repair import ReferenceRepairUnit, unit_items
from app.services.inference_backend_control import (
    BackendControlOperationError,
    backend_control_service,
)
from app.services.ml_backend import MLBackendService
from app.services.inference_limits import capability_limits_from_settings
from app.services.inference_model_execution import model_execution_port
from app.services.ml_interaction_proxy import (
    normalize_native_mask_response,
    prepare_interactive_context,
)
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import build_rle_reference
from app.services.storage import resolve_task_url
from app.services.video_frame_service import build_context_from_task
from app.services.video_tracking.jobs import (
    create_video_mask_correction_job,
    prepare_tracker_dispatch,
)
from app.services.video_tracking.control import video_control_command, video_control_job
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher
from app.services.jobs_dispatch import prepare_job_dispatch, publish_prepared_dispatch
from app.services.jobs_lifecycle_data import job_lock_statement

_GPU_ERROR_CODES = frozenset(code.value for code in GPUArbiterErrorCode)
_TRACKER_TASK = "app.workers.video_tracker.run_video_tracker_job"
_VIDEO_CONTROL_SCHEMA = "aap-labeling-video-control/v1"


def _execution_gpu_failure_record(error: BaseException) -> dict[str, Any] | None:
    cause: BaseException | None = error
    while cause is not None:
        record = gpu_arbiter_failure_record(cause)
        if record is not None:
            return record
        cause = cause.__cause__
    if not isinstance(error, ModelExecutionError):
        return None
    failure = error.failure
    if failure.code not in _GPU_ERROR_CODES:
        return None
    detail = thaw_json(failure.detail)
    message = detail.get("message") if isinstance(detail, dict) else None
    retry_after_s = None
    if failure.retry_after is not None:
        try:
            retry_after_s = int(failure.retry_after)
        except (TypeError, ValueError):
            retry_after_s = None
    return {
        "error_code": failure.code,
        "status_code": failure.status_code,
        "retry_after_s": retry_after_s,
        "message": message or failure.code,
    }


def _candidate_failure_result(kind: str, error: BaseException) -> dict[str, Any]:
    result = {
        "status": "failed",
        "kind": kind,
        "reason": (
            error.failure.code
            if isinstance(error, ModelExecutionError)
            else str(error)[:500]
        ),
    }
    if isinstance(error, ModelExecutionError):
        result["execution_failure"] = error.failure.to_wire()
    gpu_failure = _execution_gpu_failure_record(error)
    if gpu_failure is not None:
        result["gpu_arbiter_error"] = gpu_failure
        result["reason"] = gpu_failure["message"]
    return result


@asynccontextmanager
async def _repair_inputs(db, factory, attempt, batch, section, unit, worker_node_id):
    items = unit_items(batch, section, unit)
    owner = None
    if items:
        task = await get_core_row(
            db, task_row, uuid.UUID(str(items[0]["task_id"])), populate_existing=True
        )
        if any(item.get("reference_input") for item in items) or (
            task is not None and is_reference(task.file_path)
        ):
            owner = ReferenceRepairUnit(
                db, factory, attempt, batch, items, worker_node_id
            )
        elif (
            task is not None
            and task.file_type == "image"
            and unit.get("kind") != "rerun_tracker"
        ):
            from app.services.managed_mask_repair import ManagedRepairUnit

            if any(not item.get("original_input") for item in items):
                raise ContentError("repair_original_requires_new_plan")
            owner = ManagedRepairUnit(
                db, factory, attempt, batch, items, worker_node_id
            )
        elif any(item.get("original_input") for item in items):
            raise ContentError("content_binding_changed")
    failure = None
    try:
        if owner is not None:
            await db.commit()
            await owner.open()
        yield owner
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if owner is not None:
            try:
                await settled(db.rollback())
            finally:
                await settled(
                    owner.close(failure)
                    if getattr(owner, "is_managed", False)
                    else owner.close()
                )


async def _run_sam_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    batch_id: uuid.UUID,
    actor: IdentityActorStatus,
    job_id: uuid.UUID,
    attempt: RepairAttempt,
    item_index: int,
    source_owner,
) -> dict[str, Any]:
    task = await get_core_row(db, task_row, uuid.UUID(str(item["task_id"])))
    annotation = await get_core_row(
        db, annotation_row, uuid.UUID(str(item["annotation_id"]))
    )
    if task is None or annotation is None or not annotation.is_active:
        raise RuntimeError("candidate_source_missing")
    from app.services.labeling_write_scope import capture_labeling_write_scope
    from app.services.reference_bindings import task_binding

    if int(annotation.version or 1) != int(item["source_version"]):
        raise RuntimeError("candidate_source_version_conflict")
    task_id, annotation_id = task.id, annotation.id
    binding = task_binding(task)
    action = dict(item["action"])
    backend_id = uuid.UUID(str(action["backend_id"]))
    backend_service = MLBackendService(db)
    backend = await backend_service.get_registry(backend_id)
    if backend is None or not await backend_service.is_enabled(
        task.project_id, backend_id
    ):
        raise RuntimeError("sam_backend_unavailable")
    if source_owner is None:
        setup_reply = await backend_control_service(db).setup(backend_id)
        if setup_reply.error is not None:
            raise BackendControlOperationError(setup_reply.error)
        bbox = item.get("region_bbox") or {}
        context, model_id = prepare_interactive_context(
            {
                "type": "interactive_box",
                "bbox": [
                    bbox.get("x0"),
                    bbox.get("y0"),
                    bbox.get("x1"),
                    bbox.get("y1"),
                ],
                "output_geometry": "mask",
                **({"model_id": action["model_id"]} if action.get("model_id") else {}),
            },
            extract_capabilities(
                setup_reply.setup, limits=capability_limits_from_settings()
            ),
            task_id=str(task_id),
            requested_backend_id=str(backend_id),
        )
    else:
        context = deepcopy(source_owner.model["context"])
        model_id = source_owner.model["model_id"]
    if getattr(source_owner, "is_managed", False):
        expected_size = (source_owner.width, source_owner.height)
    else:
        media_summary = (
            await AssetsTaskMediaSummaryReader(db).one(task.dataset_item_id)
            if task.dataset_item_id is not None
            else None
        )
        expected_size = (
            (int(media_summary[0]), int(media_summary[1]))
            if media_summary is not None and media_summary[0] and media_summary[1]
            else None
        )
    requested_pool_id = await backend_service.pool_id_for_registry(backend_id)
    task_data = {
        "id": str(task_id),
        "file_path": source_owner.url if source_owner else resolve_task_url(task),
    }
    await db.commit()

    async def before_dispatch(dispatch):
        source_owner.validate_capabilities(
            thaw_json(dispatch.capabilities)
            if dispatch.capabilities is not None
            else {}
        )
        await source_owner.validate()
        await db.commit()

    operation = await model_execution_port(db).execute(
        FrozenModelOperation(
            request_id=uuid.uuid4(),
            operation="interactive_predict",
            task_inputs=(task_data,),
            context=context,
            project_id=task.project_id,
            requested_registry_id=backend_id,
            requested_pool_id=requested_pool_id,
            endpoint_registry_id=None,
            required_instance_id=None,
            route_owner=f"worker:mask-repair:{batch_id}:{item['issue_id']}",
            route_operation="interactive_predict",
            max_response_bytes=MAX_MASK_RESPONSE_BYTES,
            correlation={
                "batch_id": str(batch_id),
                "issue_id": str(item["issue_id"]),
            },
        ),
        before_dispatch=before_dispatch if source_owner is not None else None,
    )
    if len(operation.results) != 1 or operation.results[0].task_id != task_data["id"]:
        raise ValueError("interactive mask prediction returned an unexpected task")
    response = operation.results[0]
    candidates, diagnostic = normalize_native_mask_response(
        thaw_json(response.result),
        None if response.diagnostic is None else thaw_json(response.diagnostic),
        context=context,
        expected_size=expected_size,
    )
    if source_owner is not None:
        source_owner.route = {
            "instance_id": str(operation.provenance.instance_id),
            "pool_id": (
                str(operation.provenance.pool_id)
                if operation.provenance.pool_id is not None
                else None
            ),
        }
    await db.rollback()
    batch, _job = await lock_attempt(db, attempt)
    existing = (batch.result_json or {}).get("candidates", {}).get(str(item_index))
    if isinstance(existing, dict) and existing.get("status") == "completed":
        return existing
    if source_owner is not None:
        await source_owner.final()
    task = await get_core_row(db, task_row, task_id, populate_existing=True)
    annotation = await get_core_row(
        db, annotation_row, annotation_id, populate_existing=True
    )
    if task is None or task_binding(task) != binding:
        raise RuntimeError("candidate_input_changed")
    if (
        annotation is None
        or not annotation.is_active
        or annotation.was_cancelled
        or annotation.is_locked
        or int(annotation.version or 1) != int(item["source_version"])
    ):
        raise RuntimeError("candidate_source_version_conflict")
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=actor.id,
        actor_known=True,
        operation="mask_repair",
        execution_scope=None,
        execution_scope_known=True,
        job_id=job_id,
        job_id_known=True,
        source_annotation=annotation,
    )
    prediction_ids: list[str] = []
    candidate_records = {}
    prepared = []
    for candidate in candidates:
        rle = dict(candidate["value"]["rle"])
        reference = build_rle_reference(rle)
        prepared.append(({"type": "raster_mask", "mask": reference}, reference, rle))
    await store_repair_result_rles(db, task_id=task.id, results=prepared)
    for candidate_index, (candidate, (geometry, _reference, _rle)) in enumerate(
        zip(candidates, prepared, strict=True)
    ):
        result = [
            {
                "type": "raster_mask",
                "class_name": str(item["class_name"]),
                "geometry": geometry,
                "confidence": float(candidate.get("score") or 0),
                "tool_unit_id": str(item["tool_unit_id"]),
                "attributes": {},
            }
        ]
        receipt_kwargs = {}
        if source_owner is not None:
            receipt_key = (
                "repair_original_receipt"
                if getattr(source_owner, "is_managed", False)
                else "repair_reference_receipt"
            )
            receipt_kwargs[receipt_key] = source_owner.receipt("prediction", result)
        prediction_writer: ApplyPredictionResultPort = PredictionService(db)
        prediction = await prediction_writer.apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=task.project_id,
                ml_backend_id=operation.provenance.instance_id,
                ml_backend_pool_id=operation.provenance.pool_id,
                result=result,
                score=candidate.get("score"),
                model_version=response.model_version,
                inference_time_ms=response.inference_time_ms,
                source="mask_repair",
                pipeline_extra={
                    "mask_repair": {
                        "batch_id": str(batch_id),
                        "issue_id": str(item["issue_id"]),
                        "source_annotation_id": str(annotation.id),
                        "source_version": int(annotation.version or 1),
                        "candidate_index": candidate_index,
                        "model_id": model_id,
                    }
                },
                write_scope=write_scope,
                **receipt_kwargs,
            )
        )
        prediction_ids.append(str(prediction.id))
        if source_owner is not None:
            stored = await db.scalar(
                select(prediction_row).where(
                    prediction_row.id == prediction.id,
                    prediction_row.created_at == prediction.created_at,
                )
            )
            metadata = await db.scalar(
                select(prediction_meta_row).where(
                    prediction_meta_row.prediction_id == prediction.id
                )
            )
            candidate_records[str(prediction.id)] = {
                "result_digest": canonical_digest(stored.result),
                "metadata_digest": canonical_digest(metadata.extra),
            }
    return {
        "status": "completed",
        "kind": "rerun_local_sam",
        "prediction_ids": prediction_ids,
        "candidate_count": len(prediction_ids),
        **({"candidate_records": candidate_records} if source_owner else {}),
        "diagnostic": diagnostic,
    }


async def _prepare_tracker_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    actor: IdentityActorStatus,
):
    """Create the durable child and freeze its exact execution identity.

    Publication is deliberately deferred: the caller journals this result in the
    same commit, so an unknown broker reply can never lose the child.
    """
    task = await get_core_row(db, task_row, uuid.UUID(str(item["task_id"])))
    annotation = await get_core_row(
        db, annotation_row, uuid.UUID(str(item["annotation_id"]))
    )
    if task is None or annotation is None or not annotation.is_active:
        raise RuntimeError("candidate_source_missing")
    if int(annotation.version or 1) != int(item["source_version"]):
        raise RuntimeError("candidate_source_version_conflict")
    frame_index = int(item["frame_index"])
    keyframe = next(
        (
            value
            for value in (annotation.geometry or {}).get("keyframes") or []
            if int(value.get("frame_index", -1)) == frame_index
        ),
        None,
    )
    reference = (keyframe or {}).get("mask") or {}
    action = dict(item["action"])
    request = VideoMaskCorrectionRequest(
        correction_frame=frame_index,
        from_frame=int(action["from_frame"]),
        to_frame=int(action["to_frame"]),
        model_key=str(action["model_key"]),
        model_id=str(action["model_id"]),
        backend_id=uuid.UUID(str(action["backend_id"])),
        direction=action["direction"],
        segment_id=(
            uuid.UUID(str(action["segment_id"])) if action.get("segment_id") else None
        ),
        source_annotation_version=int(annotation.version or 1),
        corrected_mask_digest=str(reference.get("sha256") or ""),
        allow_bbox_fallback=bool(action.get("allow_bbox_fallback")),
        text=action.get("text"),
    )
    context = await build_context_from_task(db, task)
    job = await create_video_mask_correction_job(
        db,
        task=task,
        ctx=context,
        annotation_id=annotation.id,
        payload=request,
        user=actor,
    )
    dispatch_lease = await prepare_tracker_dispatch(db, job.id)
    return (
        {
            "status": "dispatch_pending",
            "kind": "rerun_tracker",
            "tracker_job_id": str(job.id),
            "celery_task_id": dispatch_lease.execution_id,
            "candidate_count": 1,
            "dispatch_state": "pending",
        },
        dispatch_lease,
    )


def _candidate_record(batch, item_index: int) -> dict[str, Any]:
    return dict(
        ((batch.result_json or {}).get("candidates") or {}).get(str(item_index)) or {}
    )


def _rollback_checkpoints(batch) -> dict[str, Any]:
    return dict(((batch.result_json or {}).get("rollback") or {}).get("shards") or {})


def _rolled_back_count(checkpoints: dict[str, Any]) -> int:
    return sum(
        int(value.get("mutation_count") or 0)
        for value in checkpoints.values()
        if isinstance(value, dict) and value.get("status") == "completed"
    )


def _candidate_settled(record: dict[str, Any]) -> bool:
    return record.get("status") == "completed" or record.get("dispatch_state") == (
        "published"
    )


async def _read_tracker_child(db: AsyncSession, child_id: str):
    """Read the durable child through the VideoControl owner command."""
    return await video_control_job(
        db,
        ReadWorkerJob(
            schema=_VIDEO_CONTROL_SCHEMA,
            operation="read_worker_job",
            job_id=uuid.UUID(child_id),
        ),
    )


async def _dispatch_tracker_handoff(
    session_factory: Any,
    *,
    dispatcher: VideoTrackerJobDispatcher,
    attempt: RepairAttempt,
    item_index: int,
    child_id: str,
    celery_task_id: str,
    lease=None,
) -> None:
    """Publish one journalled child and record only proven acknowledgement.

    The child, its exact execution identity and (when this process journalled
    it) the publication lease are already durable, so an unknown broker reply
    leaves the same envelope redrivable and a later attempt republishes that
    identity instead of creating another tracker job. Publication is fenced on
    the current attempt, so a replaced or cancelled parent never publishes; a
    confirmed publication is never demoted by a later unknown reply.
    """
    async with session_factory() as db:
        try:
            batch, _job = await lock_attempt(db, attempt)
        except RepairAttemptStopped:
            await db.rollback()
            return
        current = _candidate_record(batch, item_index)
        if current.get("tracker_job_id") != child_id or _candidate_settled(current):
            await db.rollback()
            return
        await db.rollback()

    child = None
    execution_id = celery_task_id
    async with session_factory() as db:
        child = await _read_tracker_child(db, child_id)
        if child is None:
            raise RuntimeError("tracker_handoff_missing")
        execution_id = execution_id or child.celery_task_id
        if lease is None and child.status == "queued":
            if not execution_id:
                raise RuntimeError("tracker_handoff_missing")
            # Resume after process loss: re-acquire the already recorded identity
            # through the owner ledger. A pending intent is its scheduled retry.
            lease = await prepare_job_dispatch(
                db,
                job_id=uuid.UUID(child_id),
                task_name=_TRACKER_TASK,
                args=[child_id],
                execution_id=execution_id,
            )
        await db.commit()

    accepted = False
    if lease is not None and lease.state == "submitted":
        accepted = True
    elif lease is not None and lease.state == "leased":
        async with session_factory() as db:
            try:
                await publish_prepared_dispatch(
                    db,
                    lease,
                    publish=lambda: dispatcher.dispatch_tracker_job(
                        uuid.UUID(child_id), execution_id=lease.execution_id
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The owner records accepted=False, so the exact envelope stays
                # redrivable after its lease/backoff instead of being inferred.
                await db.rollback()
            else:
                accepted = True

    async with session_factory() as db:
        await db.scalar(
            job_lock_statement(attempt.job_id, mode="update", populate_existing=True)
        )
        batch = await lock_batch(db, attempt.batch_id)
        current = _candidate_record(batch, item_index) if batch is not None else {}
        if (
            batch is None
            or batch.async_job_id != attempt.job_id
            or current.get("tracker_job_id") != child_id
        ):
            await db.rollback()
            return
        if _candidate_settled(current):
            await db.rollback()
            return
        child = await _read_tracker_child(db, child_id)
        # A child that left `queued` only proves our publication when it carries
        # the exact execution identity this attempt journalled.
        proven = accepted or (
            child is not None
            and child.status != "queued"
            and child.celery_task_id == execution_id
        )
        updated = dict(current)
        if proven:
            if child is not None and (
                child.status == "queued" or child.celery_task_id != execution_id
            ):
                # Always confirm enqueue through the owner command while the child
                # still awaits it; a matching id is not the same fact as the
                # recorded enqueue receipt.
                await video_control_command(
                    db,
                    MarkEnqueued(
                        schema=_VIDEO_CONTROL_SCHEMA,
                        operation="mark_enqueued",
                        job_id=child.id,
                        celery_task_id=execution_id,
                    ),
                )
            updated.update(status="completed", dispatch_state="published")
            updated.pop("reason", None)
        else:
            updated.update(dispatch_state="unknown", reason="tracker_dispatch_unknown")
        if not await record_repair_candidate(
            db,
            batch_id=attempt.batch_id,
            async_job_id=attempt.job_id,
            item_index=item_index,
            result_value=updated,
        ):
            await db.rollback()
            return
        await db.commit()


async def run_mask_repair(
    batch_id: str,
    celery_task_id: str | None,
    *,
    session_factory: Any,
    tracker_dispatcher: VideoTrackerJobDispatcher,
    worker_node_id: str | None,
) -> None:
    repair_id = uuid.UUID(batch_id)
    async with session_factory() as db:
        attempt = await start_attempt(db, repair_id, None, celery_task_id)
        if attempt is None:
            await db.commit()
            return
        batch = await load_repair_batch(db, batch_id=repair_id)
        async_job_id = attempt.job_id
        shards = list((batch.plan_json or {}).get("shards") or [])
        items = list((batch.plan_json or {}).get("items") or [])
        await db.commit()

    candidate_items = [
        (index, item)
        for index, item in enumerate(items)
        if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
    ]
    work_count = len(shards) + len(candidate_items)
    for position, shard in enumerate(shards):
        shard_id = str(shard["id"])
        async with session_factory() as db:
            try:
                batch, _job = await lock_attempt(db, attempt)
            except RepairAttemptStopped:
                await db.rollback()
                return
            existing = (batch.result_json or {}).get("shards", {}).get(shard_id)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                continue
            actor = await IdentityAccountLockData(db).read_actor_status(
                attempt.actor_id
            )
            if actor is None:
                raise RuntimeError("mask repair actor no longer exists")
            try:
                async with _repair_inputs(
                    db, session_factory, attempt, batch, "shards", shard, worker_node_id
                ) as source_owner:
                    await db.rollback()
                    await lock_attempt(db, attempt)
                    if source_owner is not None:
                        await source_owner.final()
                    shard_result = await execute_repair_shard(
                        db,
                        batch_id=repair_id,
                        shard=shard,
                        actor=actor,
                        async_job_id=async_job_id,
                        source_owner=source_owner,
                    )
                    if shard_result is None:
                        await db.rollback()
                        return
                    if getattr(source_owner, "is_managed", False):
                        await source_owner.recheck_credentials()
                    await db.commit()
            except RepairAttemptStopped:
                await db.rollback()
                return
            except Exception as exc:
                await db.rollback()
                async with session_factory() as journal_db:
                    recorded = await record_repair_shard_failure(
                        journal_db,
                        batch_id=repair_id,
                        async_job_id=async_job_id,
                        shard=shard,
                        error=str(exc),
                    )
                    await journal_db.commit()
                if not recorded:
                    return
        async with session_factory() as db:
            await async_job_svc.update_progress(
                db,
                async_job_id,
                int(((position + 1) / max(1, work_count)) * 100),
            )
            await db.commit()

    for offset, (item_index, item) in enumerate(candidate_items):
        handoff: tuple[str, str, Any] | None = None
        candidate_lease = None
        candidate_result = None
        async with session_factory() as db:
            try:
                batch, _job = await lock_attempt(db, attempt)
            except RepairAttemptStopped:
                await db.rollback()
                return
            existing = _candidate_record(batch, item_index)
            if existing.get("status") == "completed":
                continue
            durable_child = existing.get("tracker_job_id")
            try:
                if durable_child is not None:
                    # The child and its execution identity are already durable;
                    # republish that identity instead of creating another child.
                    await db.rollback()
                    handoff = (
                        str(durable_child),
                        str(existing.get("celery_task_id") or ""),
                        None,
                    )
                else:
                    actor = await IdentityAccountLockData(db).read_actor_status(
                        attempt.actor_id
                    )
                    if actor is None or not actor.is_active:
                        raise RuntimeError("repair_actor_unavailable")
                    async with _repair_inputs(
                        db,
                        session_factory,
                        attempt,
                        batch,
                        "candidates",
                        item,
                        worker_node_id,
                    ) as source_owner:
                        if item["kind"] == "rerun_local_sam":
                            candidate_result = await _run_sam_candidate(
                                db,
                                item=item,
                                batch_id=repair_id,
                                actor=actor,
                                job_id=async_job_id,
                                attempt=attempt,
                                item_index=item_index,
                                source_owner=source_owner,
                            )
                        else:
                            await lock_attempt(db, attempt)
                            (
                                candidate_result,
                                candidate_lease,
                            ) = await _prepare_tracker_candidate(
                                db,
                                item=item,
                                actor=actor,
                            )
                        recorded = await record_repair_candidate(
                            db,
                            batch_id=repair_id,
                            async_job_id=async_job_id,
                            item_index=item_index,
                            result_value=candidate_result,
                        )
                        if not recorded:
                            await db.rollback()
                            return
                        if getattr(source_owner, "is_managed", False):
                            await source_owner.recheck_credentials()
                        await db.commit()
                        durable_child = candidate_result.get("tracker_job_id")
                        if durable_child is not None:
                            handoff = (
                                str(durable_child),
                                str(candidate_result.get("celery_task_id") or ""),
                                candidate_lease,
                            )
            except RepairAttemptStopped:
                await db.rollback()
                return
            except Exception as exc:
                await db.rollback()
                async with session_factory() as journal_db:
                    durable_batch = await load_repair_batch(
                        journal_db, batch_id=repair_id
                    )
                    durable = (
                        _candidate_record(durable_batch, item_index)
                        if durable_batch is not None
                        else {}
                    )
                    # COMMIT may have succeeded before its response was lost.
                    # Never replace durable work with an inferred failure.
                    if durable.get("status") == "completed" or durable.get(
                        "tracker_job_id"
                    ):
                        await journal_db.rollback()
                        if durable.get("tracker_job_id"):
                            handoff = (
                                str(durable["tracker_job_id"]),
                                str(durable.get("celery_task_id") or ""),
                                candidate_lease
                                if candidate_lease is not None
                                and candidate_result is not None
                                and candidate_result.get("tracker_job_id")
                                == durable["tracker_job_id"]
                                and candidate_lease.execution_id
                                == durable.get("celery_task_id")
                                else None,
                            )
                    else:
                        recorded = await record_repair_candidate(
                            journal_db,
                            batch_id=repair_id,
                            async_job_id=async_job_id,
                            item_index=item_index,
                            result_value=_candidate_failure_result(item["kind"], exc),
                        )
                        await journal_db.commit()
                        if not recorded:
                            return
        if handoff is not None:
            await _dispatch_tracker_handoff(
                session_factory,
                dispatcher=tracker_dispatcher,
                attempt=attempt,
                item_index=item_index,
                child_id=handoff[0],
                celery_task_id=handoff[1],
                lease=handoff[2],
            )
        async with session_factory() as db:
            done = len(shards) + offset + 1
            await async_job_svc.update_progress(
                db,
                async_job_id,
                int((done / max(1, work_count)) * 100),
            )
            await db.commit()

    async with session_factory() as db:
        try:
            batch, _job = await lock_attempt(db, attempt)
        except RepairAttemptStopped:
            await db.rollback()
            return
        completed, failed, _total = progress_counts(batch)
        await finish_repair_execution(
            db,
            batch_id=repair_id,
            async_job_id=async_job_id,
            completed=completed,
            failed=failed,
        )
        await db.commit()


async def run_mask_repair_rollback(
    batch_id: str,
    celery_task_id: str | None,
    *,
    session_factory: Any,
    worker_node_id: str | None,
) -> None:
    repair_id = uuid.UUID(batch_id)
    rollback_job_id: uuid.UUID | None = None
    try:
        async with session_factory() as db:
            batch = await load_repair_batch(db, batch_id=repair_id)
            rollback_job_id = batch.rollback_async_job_id if batch is not None else None
            attempt = await start_attempt(
                db, repair_id, rollback_job_id, celery_task_id, rollback=True
            )
            if attempt is None:
                await db.commit()
                return
            batch = await load_repair_batch(db, batch_id=repair_id)
            rollback_job_id = attempt.job_id
            plan_items = list((batch.plan_json or {}).get("items") or [])
            # JSONB object key order is not execution order. Follow the immutable
            # plan so reversing this list really reverses the forward shards.
            saved = (batch.result_json or {}).get("shards") or {}
            completed_shards = [
                (str(row["id"]), saved[str(row["id"])])
                for row in (batch.plan_json or {}).get("shards") or []
                if (saved.get(str(row["id"])) or {}).get("status") == "completed"
            ]
            await db.commit()
        for shard_id, shard_result in reversed(completed_shards):
            async with session_factory() as db:
                try:
                    batch, _job = await lock_attempt(db, attempt)
                    actor = await IdentityAccountLockData(db).read_actor_status(
                        attempt.actor_id
                    )
                    if batch is None or actor is None or not actor.is_active:
                        raise RuntimeError("mask repair rollback actor missing")
                    checkpoints = _rollback_checkpoints(batch)
                    if (checkpoints.get(shard_id) or {}).get("status") == "completed":
                        await db.rollback()
                        continue
                    shard = next(
                        row
                        for row in (batch.plan_json or {}).get("shards") or []
                        if str(row["id"]) == shard_id
                    )
                    async with _repair_inputs(
                        db,
                        session_factory,
                        attempt,
                        batch,
                        "shards",
                        shard,
                        worker_node_id,
                    ) as source_owner:
                        await db.rollback()
                        await lock_attempt(db, attempt)
                        if source_owner is not None:
                            await source_owner.final()
                        mutation_count = await rollback_repair_shard(
                            db,
                            plan_items=plan_items,
                            shard_result=shard_result,
                            actor=actor,
                            batch_id=repair_id,
                            rollback_job_id=rollback_job_id,
                            source_owner=source_owner,
                        )
                        if mutation_count is None:
                            await db.rollback()
                            return
                        if not await record_repair_rollback_shard(
                            db,
                            batch_id=repair_id,
                            rollback_job_id=rollback_job_id,
                            shard_id=shard_id,
                            shard_result=shard_result,
                            mutation_count=mutation_count,
                            managed=getattr(source_owner, "is_managed", False),
                        ):
                            raise RepairAttemptStopped()
                        if getattr(source_owner, "is_managed", False):
                            await source_owner.recheck_credentials()
                        await db.commit()
                except RepairAttemptStopped:
                    await db.rollback()
                    return
                except Exception as exc:
                    await db.rollback()
                    async with session_factory() as journal_db:
                        try:
                            batch, _job = await lock_attempt(journal_db, attempt)
                        except RepairAttemptStopped:
                            await journal_db.rollback()
                            return
                        checkpoints = _rollback_checkpoints(batch)
                        if (checkpoints.get(shard_id) or {}).get(
                            "status"
                        ) == "completed":
                            # COMMIT may have succeeded before its response was
                            # lost; the checkpoint is the durable fact.
                            await journal_db.rollback()
                            continue
                        await fail_repair_rollback(
                            journal_db,
                            batch_id=repair_id,
                            rollback_job_id=rollback_job_id,
                            error=str(exc),
                            failed_shard_id=shard_id,
                            rolled_back=_rolled_back_count(checkpoints),
                        )
                        await journal_db.commit()
                    return
        async with session_factory() as db:
            batch, _job = await lock_attempt(db, attempt)
            await finish_repair_rollback(
                db,
                batch_id=repair_id,
                rollback_job_id=rollback_job_id,
                rolled_back=_rolled_back_count(_rollback_checkpoints(batch)),
            )
            await db.commit()
    except RepairAttemptStopped:
        return
    except Exception as exc:
        if rollback_job_id is not None:
            async with session_factory() as db:
                batch = await load_repair_batch(db, batch_id=repair_id)
                await fail_repair_rollback(
                    db,
                    batch_id=repair_id,
                    rollback_job_id=rollback_job_id,
                    error=str(exc),
                    failed_shard_id=None,
                    rolled_back=(
                        _rolled_back_count(_rollback_checkpoints(batch))
                        if batch is not None
                        else 0
                    ),
                )
                await db.commit()
        raise
