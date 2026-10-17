from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from aap_protocol_v2 import MAX_MASK_RESPONSE_BYTES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.mask_mutation import (
    MaskExpectedVersion,
    MaskMutationCommitRequest,
    MaskUpdateMutation,
)
from app.schemas.video_tracker_job import VideoMaskCorrectionRequest
from app.services import async_job as async_job_svc
from app.services.async_job_notify import notify_job_terminal
from app.services.gpu_arbitration.dispatch import build_gpu_dispatch_context_factory
from app.services.mask_mutation import MaskMutationService, scope_fingerprint
from app.services.mask_repair import ROLLBACK_TTL, canonical_digest
from app.services.ml_backend import MLBackendService
from app.services.ml_capabilities import extract_capabilities
from app.services.ml_client import MLBackendClient
from app.services.ml_interaction_proxy import (
    normalize_native_mask_response,
    prepare_interactive_context,
)
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.prediction import PredictionService
from app.services.raster_mask_storage import (
    build_rle_reference,
    reserve_raster_mask_upload,
    store_mask_reference_objects,
)
from app.services.storage import resolve_task_url
from app.services.video_frame_service import build_context_from_task
from app.services.video_tracking.jobs import (
    create_video_mask_correction_job,
    enqueue_tracker_job,
)
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher
from app.workers.video_tracker_dispatch import CeleryVideoTrackerJobDispatcher
from app.workers.celery_app import celery_app


_MASK_UPLOAD_LIMIT = 1000


def _result_copy(batch: MaskRepairBatch) -> dict[str, Any]:
    result = dict(batch.result_json or {})
    result["shards"] = dict(result.get("shards") or {})
    result["candidates"] = dict(result.get("candidates") or {})
    return result


def _load_plan_payload(raw: dict[str, Any]) -> MaskMutationCommitRequest:
    """Validate a persisted plan, including plans written before null omission."""

    normalized = dict(raw)
    if normalized.get("cut_path") is None:
        normalized.pop("cut_path", None)
    return MaskMutationCommitRequest.model_validate(normalized)


async def _store_result_rle(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    geometry: dict[str, Any],
    reference: dict[str, Any],
    rle: dict[str, Any],
) -> None:
    await reserve_raster_mask_upload(
        db,
        task_id=task_id,
        object_key=str(reference["object_key"]),
        limit=_MASK_UPLOAD_LIMIT,
    )
    await store_mask_reference_objects(
        db,
        geometry,
        [(reference, rle)],
        task_id=task_id,
    )


async def _execute_shard(
    db: AsyncSession,
    *,
    batch: MaskRepairBatch,
    shard: dict[str, Any],
    actor: User,
) -> dict[str, Any]:
    plan_items = list((batch.plan_json or {}).get("items") or [])
    progressed: set[str] = set()
    item_results: list[dict[str, Any]] = []
    for item_index in shard.get("item_indexes") or []:
        item = plan_items[int(item_index)]
        payload = _load_plan_payload(item["payload"])
        task_id = uuid.UUID(str(item["task_id"]))
        service = MaskMutationService(db)
        members = await service._lock_scope(
            task_id,
            payload.scope,
            for_update=False,
        )
        current = {str(row.id): int(row.version or 1) for row in members}
        frozen = {
            str(row.annotation_id): row.version for row in payload.expected_versions
        }
        if set(current) != set(frozen):
            raise RuntimeError("scope_stale")
        conflicts = [
            annotation_id
            for annotation_id, version in current.items()
            if version != frozen[annotation_id] and annotation_id not in progressed
        ]
        if conflicts:
            raise RuntimeError(f"version_conflict:{','.join(sorted(conflicts))}")
        payload = payload.model_copy(
            update={
                "scope_fingerprint": scope_fingerprint(payload.scope, members),
                "expected_versions": [
                    MaskExpectedVersion(
                        annotation_id=row.id,
                        version=int(row.version or 1),
                    )
                    for row in members
                ],
            }
        )
        target_id = str(item["annotation_id"])
        mutation = payload.mutations[0]
        geometry = mutation.geometry.model_dump(mode="json", by_alias=True)
        await _store_result_rle(
            db,
            task_id=task_id,
            geometry=geometry,
            reference=dict(item["result_reference"]),
            rle=dict(item["result_rle"]),
        )
        response = await service.commit(task_id, payload, actor)
        after_version = int(response.result_versions[target_id])
        progressed.add(target_id)
        issue = await db.get(MaskQCIssue, uuid.UUID(str(item["issue_id"])))
        if issue is not None:
            issue.status = "resolved"
            issue.resolved_by_id = actor.id
            issue.resolved_at = datetime.now(timezone.utc)
        item_results.append(
            {
                "item_index": int(item_index),
                "issue_id": str(item["issue_id"]),
                "annotation_id": target_id,
                "before_version": int(item["before_version"]),
                "after_version": after_version,
                "operation_id": str(response.operation_id),
                "changed_pixels": int(
                    response.result_versions.get(target_id) is not None
                    and payload.report.changed_pixels
                    or 0
                ),
            }
        )
    return {
        "status": "completed",
        "task_id": str(shard["task_id"]),
        "items": item_results,
        "operation_ids": [item["operation_id"] for item in item_results],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def _run_sam_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    batch_id: uuid.UUID,
    actor: User,
    job_id: uuid.UUID,
    dispatch_context_factory,
) -> dict[str, Any]:
    task = await db.get(Task, uuid.UUID(str(item["task_id"])))
    annotation = await db.get(Annotation, uuid.UUID(str(item["annotation_id"])))
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
        extract_capabilities(setup),
        task_id=str(task.id),
        requested_backend_id=str(backend_id),
    )
    dataset_item = (
        await db.get(DatasetItem, task.dataset_item_id)
        if task.dataset_item_id is not None
        else None
    )
    expected_size = (
        (int(dataset_item.width), int(dataset_item.height))
        if dataset_item is not None and dataset_item.width and dataset_item.height
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
        await _store_result_rle(
            db,
            task_id=task.id,
            geometry=geometry,
            reference=reference,
            rle=rle,
        )
        prediction = await PredictionService(db).create_from_ml_result(
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
    actor: User,
    dispatcher: VideoTrackerJobDispatcher,
) -> dict[str, Any]:
    task = await db.get(Task, uuid.UUID(str(item["task_id"])))
    annotation = await db.get(Annotation, uuid.UUID(str(item["annotation_id"])))
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
    await db.commit()
    job = await enqueue_tracker_job(db, job.id, dispatcher=dispatcher, fail_closed=True)
    return {
        "status": "completed",
        "kind": "rerun_tracker",
        "tracker_job_id": str(job.id),
        "candidate_count": 1,
    }


async def _mark_batch_start(
    SessionLocal: async_sessionmaker[AsyncSession],
    batch_id: uuid.UUID,
    celery_task_id: str | None,
) -> tuple[uuid.UUID, list[dict[str, Any]], list[dict[str, Any]], bool]:
    async with SessionLocal() as db:
        batch = (
            await db.execute(
                select(MaskRepairBatch)
                .where(MaskRepairBatch.id == batch_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if batch is None or batch.async_job_id is None:
            raise RuntimeError("mask repair batch not found")
        job = await db.get(AsyncJob, batch.async_job_id)
        if job is None:
            raise RuntimeError("mask repair async job not found")
        if job is not None and celery_task_id and not job.celery_task_id:
            job.celery_task_id = celery_task_id
        if job.status == "cancelled" or bool(
            (job.payload or {}).get("cancel_requested")
        ):
            completed_shards = [
                value
                for value in (_result_copy(batch).get("shards") or {}).values()
                if isinstance(value, dict) and value.get("status") == "completed"
            ]
            batch.status = "partial" if completed_shards else "cancelled"
            batch.completed_at = datetime.now(timezone.utc)
            await async_job_svc.mark_cancelled(
                db, batch.async_job_id, result={"reason": "cancelled_by_user"}
            )
            await notify_job_terminal(db, job_id=batch.async_job_id)
            await db.commit()
            return batch.async_job_id, [], [], True
        if batch.status not in {"pending", "running"}:
            await db.commit()
            return batch.async_job_id, [], [], True
        batch.status = "running"
        await async_job_svc.mark_running(
            db, batch.async_job_id, celery_task_id=celery_task_id
        )
        await db.commit()
        plan = dict(batch.plan_json or {})
        return (
            batch.async_job_id,
            list(plan.get("shards") or []),
            list(plan.get("items") or []),
            False,
        )


async def _stop_if_cancelled(
    db: AsyncSession,
    *,
    batch_id: uuid.UUID,
    async_job_id: uuid.UUID,
) -> bool:
    job = await db.get(AsyncJob, async_job_id)
    if job is None or (
        job.status != "cancelled"
        and not bool((job.payload or {}).get("cancel_requested"))
    ):
        return False
    batch = (
        await db.execute(
            select(MaskRepairBatch)
            .where(MaskRepairBatch.id == batch_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if batch is not None:
        completed_shards = [
            value
            for value in (_result_copy(batch).get("shards") or {}).values()
            if isinstance(value, dict) and value.get("status") == "completed"
        ]
        batch.status = "partial" if completed_shards else "cancelled"
        batch.completed_at = datetime.now(timezone.utc)
    await async_job_svc.mark_cancelled(
        db, async_job_id, result={"reason": "cancelled_by_user"}
    )
    await notify_job_terminal(db, job_id=async_job_id)
    await db.commit()
    return True


async def _run_mask_repair(
    batch_id: str,
    celery_task_id: str | None,
    *,
    tracker_dispatcher: VideoTrackerJobDispatcher,
) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    dispatch_context_factory = build_gpu_dispatch_context_factory(SessionLocal)
    repair_id = uuid.UUID(batch_id)
    try:
        async_job_id, shards, items, cancelled = await _mark_batch_start(
            SessionLocal, repair_id, celery_task_id
        )
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
            async with SessionLocal() as db:
                if await _stop_if_cancelled(
                    db, batch_id=repair_id, async_job_id=async_job_id
                ):
                    return
                batch = await db.get(MaskRepairBatch, repair_id)
                if batch is None:
                    raise RuntimeError("mask repair batch disappeared")
                existing = (_result_copy(batch).get("shards") or {}).get(shard_id)
                if isinstance(existing, dict) and existing.get("status") == "completed":
                    completed += 1
                    continue
                actor = (
                    await db.get(User, batch.requested_by_id)
                    if batch.requested_by_id is not None
                    else None
                )
                if actor is None:
                    raise RuntimeError("mask repair actor no longer exists")
                try:
                    shard_result = await _execute_shard(
                        db, batch=batch, shard=shard, actor=actor
                    )
                    result = _result_copy(batch)
                    result["shards"][shard_id] = shard_result
                    batch.result_json = result
                    batch.rollback_expires_at = (
                        datetime.now(timezone.utc) + ROLLBACK_TTL
                    )
                    await db.commit()
                    completed += 1
                except Exception as exc:
                    await db.rollback()
                    batch = (
                        await db.execute(
                            select(MaskRepairBatch)
                            .where(MaskRepairBatch.id == repair_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    result = _result_copy(batch)
                    result["shards"][shard_id] = {
                        "status": "failed",
                        "task_id": str(shard["task_id"]),
                        "reason": str(exc)[:500],
                    }
                    batch.result_json = result
                    await db.commit()
                    failed += 1
            async with SessionLocal() as db:
                await async_job_svc.update_progress(
                    db,
                    async_job_id,
                    int(((position + 1) / max(1, work_count)) * 100),
                )
                await db.commit()

        for offset, (item_index, item) in enumerate(candidate_items):
            key = str(item_index)
            async with SessionLocal() as db:
                if await _stop_if_cancelled(
                    db, batch_id=repair_id, async_job_id=async_job_id
                ):
                    return
                batch = await db.get(MaskRepairBatch, repair_id)
                if batch is None:
                    raise RuntimeError("mask repair batch disappeared")
                existing = (_result_copy(batch).get("candidates") or {}).get(key)
                if isinstance(existing, dict) and existing.get("status") == "completed":
                    completed += 1
                    continue
                actor = (
                    await db.get(User, batch.requested_by_id)
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
                        await db.commit()
                    else:
                        candidate_result = await _run_tracker_candidate(
                            db,
                            item=item,
                            actor=actor,
                            dispatcher=tracker_dispatcher,
                        )
                    batch = await db.get(MaskRepairBatch, repair_id)
                    result = _result_copy(batch)
                    result["candidates"][key] = candidate_result
                    batch.result_json = result
                    await db.commit()
                    completed += 1
                except Exception as exc:
                    await db.rollback()
                    batch = (
                        await db.execute(
                            select(MaskRepairBatch)
                            .where(MaskRepairBatch.id == repair_id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    result = _result_copy(batch)
                    result["candidates"][key] = {
                        "status": "failed",
                        "kind": item["kind"],
                        "reason": str(exc)[:500],
                    }
                    batch.result_json = result
                    await db.commit()
                    failed += 1
            async with SessionLocal() as db:
                done = len(shards) + offset + 1
                await async_job_svc.update_progress(
                    db,
                    async_job_id,
                    int((done / max(1, work_count)) * 100),
                )
                await db.commit()

        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskRepairBatch)
                    .where(MaskRepairBatch.id == repair_id)
                    .with_for_update()
                )
            ).scalar_one()
            batch.status = (
                "completed" if failed == 0 else ("partial" if completed else "failed")
            )
            batch.completed_at = datetime.now(timezone.utc)
            summary = {
                "batch_id": str(batch.id),
                "success_count": completed,
                "failed_count": failed,
                "result_digest": canonical_digest(batch.result_json or {}),
            }
            if completed:
                await async_job_svc.mark_complete(db, async_job_id, result=summary)
            else:
                await async_job_svc.mark_failed(
                    db,
                    async_job_id,
                    error="all mask repair shards failed",
                    result=summary,
                )
            await notify_job_terminal(db, job_id=async_job_id)
            await db.commit()
    finally:
        await engine.dispose()


def _rollback_payload(
    *,
    item: dict[str, Any],
    current_members: list[Annotation],
) -> MaskMutationCommitRequest:
    original = _load_plan_payload(item["payload"])
    target_id = uuid.UUID(str(item["annotation_id"]))
    mutation = MaskUpdateMutation(
        kind="update",
        annotation_id=target_id,
        geometry=item["before_geometry"],
    )
    return MaskMutationCommitRequest(
        idempotency_key=f"mask-repair-rollback:{item['issue_id']}",
        operation="mask_repair_rollback",
        scope=original.scope,
        scope_fingerprint=scope_fingerprint(original.scope, current_members),
        expected_versions=[
            MaskExpectedVersion(
                annotation_id=row.id,
                version=int(row.version or 1),
            )
            for row in current_members
        ],
        mutations=[mutation],
    )


async def _rollback_shard(
    db: AsyncSession,
    *,
    plan_items: list[dict[str, Any]],
    shard_result: dict[str, Any],
    actor: User,
) -> int:
    entries = list(shard_result.get("items") or [])
    for entry in entries:
        item = plan_items[int(entry["item_index"])]
        target = await db.get(Annotation, uuid.UUID(str(item["annotation_id"])))
        if target is None or int(target.version or 1) != int(entry["after_version"]):
            raise RuntimeError(f"rollback_version_conflict:{item['annotation_id']}")
    for entry in reversed(entries):
        item = plan_items[int(entry["item_index"])]
        original = _load_plan_payload(item["payload"])
        service = MaskMutationService(db)
        members = await service._lock_scope(
            uuid.UUID(str(item["task_id"])),
            original.scope,
            for_update=False,
        )
        payload = _rollback_payload(item=item, current_members=members)
        await service.commit(uuid.UUID(str(item["task_id"])), payload, actor)
    return len(entries)


async def _rollback_mask_repair(batch_id: str, celery_task_id: str | None) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    repair_id = uuid.UUID(batch_id)
    failed_shard_id: str | None = None
    rolled_back = 0
    try:
        async with SessionLocal() as db:
            batch = await db.get(MaskRepairBatch, repair_id)
            if batch is None or batch.rollback_async_job_id is None:
                raise RuntimeError("mask repair rollback batch not found")
            job_id = batch.rollback_async_job_id
            await async_job_svc.mark_running(db, job_id, celery_task_id=celery_task_id)
            await db.commit()
        async with SessionLocal() as db:
            batch = await db.get(MaskRepairBatch, repair_id)
            assert batch is not None
            plan_items = list((batch.plan_json or {}).get("items") or [])
            shard_results = dict((batch.result_json or {}).get("shards") or {})
            completed_shards = [
                (shard_id, shard)
                for shard_id, shard in shard_results.items()
                if isinstance(shard, dict) and shard.get("status") == "completed"
            ]
        for shard_id, shard_result in reversed(completed_shards):
            failed_shard_id = shard_id
            async with SessionLocal() as db:
                batch = await db.get(MaskRepairBatch, repair_id)
                actor = (
                    await db.get(User, batch.requested_by_id)
                    if batch is not None and batch.requested_by_id is not None
                    else None
                )
                if batch is None or actor is None:
                    raise RuntimeError("mask repair rollback actor missing")
                mutation_count = await _rollback_shard(
                    db,
                    plan_items=plan_items,
                    shard_result=shard_result,
                    actor=actor,
                )
                await db.commit()
                rolled_back += mutation_count
            failed_shard_id = None
        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskRepairBatch)
                    .where(MaskRepairBatch.id == repair_id)
                    .with_for_update()
                )
            ).scalar_one()
            batch.status = "rolled_back"
            batch.rolled_back_at = datetime.now(timezone.utc)
            result = _result_copy(batch)
            result["rollback"] = {
                "status": "completed",
                "mutation_count": rolled_back,
                "completed_at": batch.rolled_back_at.isoformat(),
            }
            batch.result_json = result
            await async_job_svc.mark_complete(
                db,
                batch.rollback_async_job_id,
                result={"batch_id": str(batch.id), "rolled_back_count": rolled_back},
            )
            await notify_job_terminal(db, job_id=batch.rollback_async_job_id)
            await db.commit()
    except Exception as exc:
        async with SessionLocal() as db:
            batch = (
                await db.execute(
                    select(MaskRepairBatch)
                    .where(MaskRepairBatch.id == repair_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is not None:
                batch.status = "rollback_failed"
                result = _result_copy(batch)
                result["rollback"] = {
                    "status": "failed",
                    "reason": str(exc)[:500],
                    "failed_shard_id": failed_shard_id,
                    "rolled_back_count": rolled_back,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                }
                batch.result_json = result
                if batch.rollback_async_job_id is not None:
                    await async_job_svc.mark_failed(
                        db, batch.rollback_async_job_id, error=str(exc)
                    )
                    await notify_job_terminal(db, job_id=batch.rollback_async_job_id)
                await db.commit()
        raise
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.workers.mask_repair.run_mask_repair")
def run_mask_repair(self, batch_id: str) -> None:
    tracker_dispatcher = CeleryVideoTrackerJobDispatcher()
    asyncio.run(
        _run_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
            tracker_dispatcher=tracker_dispatcher,
        )
    )


@celery_app.task(bind=True, name="app.workers.mask_repair.rollback_mask_repair")
def rollback_mask_repair(self, batch_id: str) -> None:
    asyncio.run(
        _rollback_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
        )
    )
