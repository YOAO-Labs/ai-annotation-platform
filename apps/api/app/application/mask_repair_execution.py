"""Application workflow for mask-repair execution and rollback."""

from __future__ import annotations

import uuid
from typing import Any

from aap_inference import extract_capabilities
from aap_protocol_v2 import MAX_MASK_RESPONSE_BYTES
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.assets_media_summary import AssetsTaskMediaSummaryReader
from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorStatus
from app.schemas.video_tracker_job import VideoMaskCorrectionRequest
from app.services import async_job as async_job_svc
from app.services.mask_repair import (
    begin_repair_execution,
    begin_repair_rollback,
    execute_repair_shard,
    fail_repair_rollback,
    finish_repair_execution,
    finish_repair_rollback,
    load_repair_batch,
    record_repair_candidate,
    record_repair_shard_failure,
    rollback_repair_shard,
    stop_repair_execution_if_cancelled,
    store_repair_result_rle,
)
from app.services.ml_backend import MLBackendService
from app.services.ml_client import MLBackendClient
from app.services.inference_limits import capability_limits_from_settings
from app.services.ml_interaction_proxy import (
    normalize_native_mask_response,
    prepare_interactive_context,
)
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultPort,
    ApplyPredictionResultRequest,
)
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import build_rle_reference
from app.services.storage import resolve_task_url
from app.services.video_frame_service import AssetsVideoContextReader
from app.services.video_tracking.jobs import (
    create_video_mask_correction_job,
    enqueue_tracker_job,
    prepare_tracker_dispatch,
)
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher


async def _run_sam_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    batch_id: uuid.UUID,
    actor: IdentityActorStatus,
    job_id: uuid.UUID,
    dispatch_context_factory,
) -> dict[str, Any]:
    task = await get_core_row(db, task_row, uuid.UUID(str(item["task_id"])))
    annotation = await get_core_row(
        db, annotation_row, uuid.UUID(str(item["annotation_id"]))
    )
    if task is None or annotation is None or not annotation.is_active:
        raise RuntimeError("candidate_source_missing")
    from app.services.labeling_write_scope import capture_labeling_write_scope

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
    if int(annotation.version or 1) != int(item["source_version"]):
        raise RuntimeError("candidate_source_version_conflict")
    action = dict(item["action"])
    backend_id = uuid.UUID(str(action["backend_id"]))
    backend_service = MLBackendService(db)
    backend = await backend_service.get(backend_id)
    if backend is None or not await backend_service.is_enabled(
        task.project_id, backend_id
    ):
        raise RuntimeError("sam_backend_unavailable")
    setup = await MLBackendClient(backend).setup()
    bbox = item.get("region_bbox") or {}
    context, model_id = prepare_interactive_context(
        {
            "type": "interactive_box",
            "bbox": [bbox.get("x0"), bbox.get("y0"), bbox.get("x1"), bbox.get("y1")],
            "output_geometry": "mask",
            **({"model_id": action["model_id"]} if action.get("model_id") else {}),
        },
        extract_capabilities(setup, limits=capability_limits_from_settings()),
        task_id=str(task.id),
        requested_backend_id=str(backend_id),
    )
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
    client = RoutedMLBackendClient(
        db,
        backend,
        project_id=task.project_id,
        owner=f"worker:mask-repair:{batch_id}:{item['issue_id']}",
        operation="interactive_predict",
        dispatch_context_factory=dispatch_context_factory,
    )
    response = await client.predict_interactive(
        task_data={"id": str(task.id), "file_path": resolve_task_url(task)},
        context=context,
        max_response_bytes=MAX_MASK_RESPONSE_BYTES,
    )
    candidates, diagnostic = normalize_native_mask_response(
        response.result,
        getattr(response, "diagnostic", None),
        context=context,
        expected_size=expected_size,
    )
    prediction_ids: list[str] = []
    for candidate_index, candidate in enumerate(candidates):
        rle = dict(candidate["value"]["rle"])
        reference = build_rle_reference(rle)
        geometry = {"type": "raster_mask", "mask": reference}
        await store_repair_result_rle(
            db,
            task_id=task.id,
            geometry=geometry,
            reference=reference,
            rle=rle,
        )
        prediction_writer: ApplyPredictionResultPort = PredictionService(db)
        prediction = await prediction_writer.apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=task.project_id,
                ml_backend_id=client.last_instance_id or backend_id,
                ml_backend_pool_id=client.pool_id,
                result=[
                    {
                        "type": "raster_mask",
                        "class_name": str(item["class_name"]),
                        "geometry": geometry,
                        "confidence": float(candidate.get("score") or 0),
                        "tool_unit_id": str(item["tool_unit_id"]),
                        "attributes": {},
                    }
                ],
                score=candidate.get("score"),
                model_version=getattr(response, "model_version", None),
                inference_time_ms=getattr(response, "inference_time_ms", None),
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
            )
        )
        prediction_ids.append(str(prediction.id))
    return {
        "status": "completed",
        "kind": "rerun_local_sam",
        "prediction_ids": prediction_ids,
        "candidate_count": len(prediction_ids),
        "diagnostic": diagnostic,
    }


async def _run_tracker_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    actor: IdentityActorStatus,
    dispatcher: VideoTrackerJobDispatcher,
) -> dict[str, Any]:
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
    context = await AssetsVideoContextReader(db).from_task_summary(
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    job = await create_video_mask_correction_job(
        db,
        task=task,
        ctx=context,
        annotation_id=annotation.id,
        payload=request,
        user=actor,
    )
    dispatch_lease = await prepare_tracker_dispatch(db, job.id)
    await db.commit()
    job = await enqueue_tracker_job(
        db,
        job.id,
        dispatcher=dispatcher,
        dispatch_lease=dispatch_lease,
        fail_closed=True,
    )
    return {
        "status": "completed",
        "kind": "rerun_tracker",
        "tracker_job_id": str(job.id),
        "candidate_count": 1,
    }


async def run_mask_repair(
    batch_id: str,
    celery_task_id: str | None,
    *,
    session_factory: Any,
    tracker_dispatcher: VideoTrackerJobDispatcher,
    dispatch_context_factory,
) -> None:
    repair_id = uuid.UUID(batch_id)
    async with session_factory() as db:
        async_job_id, shards, items, cancelled = await begin_repair_execution(
            db,
            batch_id=repair_id,
            celery_task_id=celery_task_id,
        )
        await db.commit()
    if cancelled:
        return

    candidate_items = [
        (index, item)
        for index, item in enumerate(items)
        if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
    ]
    work_count = len(shards) + len(candidate_items)
    completed = failed = 0
    for position, shard in enumerate(shards):
        shard_id = str(shard["id"])
        async with session_factory() as db:
            if await stop_repair_execution_if_cancelled(
                db,
                batch_id=repair_id,
                async_job_id=async_job_id,
            ):
                await db.commit()
                return
            batch = await load_repair_batch(db, batch_id=repair_id)
            if batch is None:
                raise RuntimeError("mask repair batch disappeared")
            if batch.async_job_id != async_job_id:
                return
            existing = (batch.result_json or {}).get("shards", {}).get(shard_id)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                completed += 1
                continue
            actor = (
                await IdentityAccountLockData(db).read_actor_status(
                    batch.requested_by_id
                )
                if batch.requested_by_id is not None
                else None
            )
            if actor is None:
                raise RuntimeError("mask repair actor no longer exists")
            try:
                shard_result = await execute_repair_shard(
                    db,
                    batch_id=repair_id,
                    shard=shard,
                    actor=actor,
                    async_job_id=async_job_id,
                )
                if shard_result is None:
                    await db.rollback()
                    return
                await db.commit()
                completed += 1
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
                failed += 1
        async with session_factory() as db:
            await async_job_svc.update_progress(
                db,
                async_job_id,
                int(((position + 1) / max(1, work_count)) * 100),
            )
            await db.commit()

    for offset, (item_index, item) in enumerate(candidate_items):
        async with session_factory() as db:
            if await stop_repair_execution_if_cancelled(
                db,
                batch_id=repair_id,
                async_job_id=async_job_id,
            ):
                await db.commit()
                return
            batch = await load_repair_batch(db, batch_id=repair_id)
            if batch is None:
                raise RuntimeError("mask repair batch disappeared")
            if batch.async_job_id != async_job_id:
                return
            existing = (
                (batch.result_json or {}).get("candidates", {}).get(str(item_index))
            )
            if isinstance(existing, dict) and existing.get("status") == "completed":
                completed += 1
                continue
            actor = (
                await IdentityAccountLockData(db).read_actor_status(
                    batch.requested_by_id
                )
                if batch.requested_by_id is not None
                else None
            )
            if actor is None:
                raise RuntimeError("mask repair actor no longer exists")
            try:
                if item["kind"] == "rerun_local_sam":
                    candidate_result = await _run_sam_candidate(
                        db,
                        item=item,
                        batch_id=repair_id,
                        actor=actor,
                        job_id=async_job_id,
                        dispatch_context_factory=dispatch_context_factory,
                    )
                else:
                    candidate_result = await _run_tracker_candidate(
                        db,
                        item=item,
                        actor=actor,
                        dispatcher=tracker_dispatcher,
                    )
                if item["kind"] == "rerun_local_sam":
                    # The write guard holds an AsyncJob share lock; release it
                    # before the candidate journal acquires the batch update lock.
                    await db.commit()
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
                await db.commit()
                completed += 1
            except Exception as exc:
                await db.rollback()
                async with session_factory() as journal_db:
                    recorded = await record_repair_candidate(
                        journal_db,
                        batch_id=repair_id,
                        async_job_id=async_job_id,
                        item_index=item_index,
                        result_value={
                            "status": "failed",
                            "kind": item["kind"],
                            "reason": str(exc)[:500],
                        },
                    )
                    await journal_db.commit()
                if not recorded:
                    return
                failed += 1
        async with session_factory() as db:
            done = len(shards) + offset + 1
            await async_job_svc.update_progress(
                db,
                async_job_id,
                int((done / max(1, work_count)) * 100),
            )
            await db.commit()

    async with session_factory() as db:
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
) -> None:
    repair_id = uuid.UUID(batch_id)
    failed_shard_id: str | None = None
    rolled_back = 0
    rollback_job_id: uuid.UUID | None = None
    try:
        async with session_factory() as db:
            batch = await load_repair_batch(db, batch_id=repair_id)
            rollback_job_id = batch.rollback_async_job_id if batch is not None else None
            rollback_state = await begin_repair_rollback(
                db,
                batch_id=repair_id,
                celery_task_id=celery_task_id,
            )
            await db.commit()
        if rollback_state is None:
            return
        rollback_job_id, plan_items, completed_shards = rollback_state
        for shard_id, shard_result in reversed(completed_shards):
            failed_shard_id = shard_id
            async with session_factory() as db:
                batch = await load_repair_batch(db, batch_id=repair_id)
                actor = (
                    await IdentityAccountLockData(db).read_actor_status(
                        batch.requested_by_id
                    )
                    if batch is not None and batch.requested_by_id is not None
                    else None
                )
                if batch is None or actor is None:
                    raise RuntimeError("mask repair rollback actor missing")
                mutation_count = await rollback_repair_shard(
                    db,
                    plan_items=plan_items,
                    shard_result=shard_result,
                    actor=actor,
                    batch_id=repair_id,
                    rollback_job_id=rollback_job_id,
                )
                if mutation_count is None:
                    await db.rollback()
                    return
                await db.commit()
                rolled_back += mutation_count
            failed_shard_id = None
        async with session_factory() as db:
            await finish_repair_rollback(
                db,
                batch_id=repair_id,
                rollback_job_id=rollback_job_id,
                rolled_back=rolled_back,
            )
            await db.commit()
    except Exception as exc:
        if rollback_job_id is not None:
            async with session_factory() as db:
                await fail_repair_rollback(
                    db,
                    batch_id=repair_id,
                    rollback_job_id=rollback_job_id,
                    error=str(exc),
                    failed_shard_id=failed_shard_id,
                    rolled_back=rolled_back,
                )
                await db.commit()
        raise
