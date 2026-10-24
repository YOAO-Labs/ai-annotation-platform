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
from app.services.content_snapshot import ContentError
from app.services.async_job_notify import notify_job_terminal
from app.services.async_job_terminal import committed_repair_units
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
    lock_raster_mask_references,
    reserve_raster_mask_upload,
    store_mask_reference_objects,
)
from app.services.storage import resolve_task_url
from app.services.video_frame_service import build_context_from_task
from app.services.video_tracking.jobs import (
    create_video_mask_correction_job,
)
from app.workers.celery_app import celery_app
from app.services.mask_repair_execution import (
    RepairAttemptStopped,
    start_attempt,
    lock_attempt,
    progress_counts,
    authorize_repair_task,
)
from app.services.reference_keys import is_reference
from app.services.reference_mask_repair import (
    ReferenceRepairUnit,
    unit_items,
    validate_receipt,
)


_MASK_UPLOAD_LIMIT = 1000


def _is_managed(owner) -> bool:
    return getattr(owner, "is_managed", False) is True


def _require_image_owner(task, owner) -> None:
    if task.file_type == "image" and not is_reference(task.file_path) and owner is None:
        raise ContentError("repair_original_requires_new_plan")


async def _validate_unit_receipt(db, task_id, actor_id, owner, payload):
    validator = validate_receipt
    if _is_managed(owner):
        from app.services.managed_mask_repair import validate_receipt as validator

    return await validator(
        db, task_id, actor_id, owner.receipt("unit", payload), "unit", payload
    )


def _mutation_receipt(owner, payload):
    if owner is None:
        return {}
    key = (
        "repair_original_receipt" if _is_managed(owner) else "reference_repair_receipt"
    )
    return {key: owner.receipt("mutation", payload.model_dump(mode="json"))}


def _prediction_receipt(owner, result):
    if owner is None:
        return {}
    key = (
        "repair_original_receipt" if _is_managed(owner) else "repair_reference_receipt"
    )
    return {key: owner.receipt("prediction", result)}


async def _close_repair_owner(db, owner, failure):
    if _is_managed(owner):
        from app.services.prediction_originals import settled

        try:
            await settled(db.rollback())
        finally:
            await settled(owner.close(failure))
    else:
        try:
            await db.rollback()
        finally:
            await owner.close()


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
    reference_owner=None,
) -> dict[str, Any]:
    plan_items = list((batch.plan_json or {}).get("items") or [])
    task_id = uuid.UUID(str(shard["task_id"]))
    task = await _repair_task(db, task_id, actor)
    _require_image_owner(task, reference_owner)
    if reference_owner is None:
        await MaskMutationService(db)._lock_task(task_id)
    else:
        await _validate_unit_receipt(db, task_id, actor.id, reference_owner, shard)
    shard_items = [plan_items[int(index)] for index in shard.get("item_indexes") or []]
    output_geometries = [
        _load_plan_payload(item["payload"])
        .mutations[0]
        .geometry.model_dump(mode="json", by_alias=True)
        for item in shard_items
    ]
    for item in shard_items:
        await reserve_raster_mask_upload(
            db,
            task_id=task_id,
            object_key=str(item["result_reference"]["object_key"]),
            limit=_MASK_UPLOAD_LIMIT,
        )
    # Acquire every output content lock before any Annotation row lock, in the
    # same global order even when another task produces the same masks.
    await lock_raster_mask_references(db, output_geometries, verify=False)
    for item, geometry in zip(shard_items, output_geometries, strict=True):
        await _store_result_rle(
            db,
            task_id=task_id,
            geometry=geometry,
            reference=dict(item["result_reference"]),
            rle=dict(item["result_rle"]),
        )
    progressed: dict[str, int] = {}
    for saved in (_result_copy(batch).get("shards") or {}).values():
        if saved.get("status") == "completed" and saved.get("task_id") == str(
            shard["task_id"]
        ):
            for entry in saved.get("items") or []:
                progressed[str(entry["annotation_id"])] = int(entry["after_version"])
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
            if version != progressed.get(annotation_id, frozen[annotation_id])
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
        response = await service.commit(
            task_id,
            payload,
            actor,
            **_mutation_receipt(reference_owner, payload),
        )
        if _is_managed(reference_owner):
            await reference_owner.note_committed_mutation(payload, response)
        after_version = int(response.result_versions[target_id])
        progressed[target_id] = after_version
        issue = await db.get(MaskQCIssue, uuid.UUID(str(item["issue_id"])))
        if issue is not None:
            issue.status = "resolved"
            issue.resolved_by_id = actor.id
            issue.resolved_at = datetime.now(timezone.utc)
        item_result = {
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
        if _is_managed(reference_owner):
            from app.services.managed_mask_repair import annotation_facts

            target = await db.get(Annotation, uuid.UUID(target_id))
            item_result["after_facts"] = annotation_facts(target)
        item_results.append(item_result)
    return {
        "status": "completed",
        "task_id": str(shard["task_id"]),
        "items": item_results,
        "operation_ids": [item["operation_id"] for item in item_results],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


async def _prepare_sam_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    batch_id: uuid.UUID,
    dispatch_context_factory,
    reference_owner=None,
) -> dict[str, Any]:
    task = await db.get(Task, uuid.UUID(str(item["task_id"])))
    annotation = await db.get(Annotation, uuid.UUID(str(item["annotation_id"])))
    if task is None or annotation is None or not annotation.is_active:
        raise RuntimeError("candidate_source_missing")
    if int(annotation.version or 1) != int(item["source_version"]):
        raise RuntimeError("candidate_source_version_conflict")
    _require_image_owner(task, reference_owner)
    from app.services.reference_access import reject_reference_scope
    from app.services.reference_bindings import task_binding

    if reference_owner is None:
        await reject_reference_scope(db, task_ids=[task.id])
    else:
        await reference_owner.validate()
    binding = task_binding(task)
    action = dict(item["action"])
    backend_id = uuid.UUID(str(action["backend_id"]))
    backend_service = MLBackendService(db)
    backend = await backend_service.get(backend_id)
    if backend is None or not await backend_service.is_enabled(
        task.project_id, backend_id
    ):
        raise RuntimeError("sam_backend_unavailable")
    if reference_owner is None:
        setup = await MLBackendClient(backend).setup()
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
            extract_capabilities(setup),
            task_id=str(task.id),
            requested_backend_id=str(backend_id),
        )
    else:
        from copy import deepcopy

        context = deepcopy(reference_owner.model["context"])
        model_id = reference_owner.model["model_id"]
    if _is_managed(reference_owner):
        expected_size = (reference_owner.width, reference_owner.height)
    else:
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
    await db.commit()

    async def before_dispatch(transport):
        if _is_managed(reference_owner):
            await reference_owner.before_dispatch(transport)
            return
        setup = await transport.setup()
        reference_owner.validate_capabilities(extract_capabilities(setup))
        await reference_owner.validate()
        await db.commit()

    response = await client.predict_interactive(
        task_data={
            "id": str(task.id),
            "file_path": reference_owner.url
            if reference_owner
            else resolve_task_url(task),
        },
        context=context,
        max_response_bytes=MAX_MASK_RESPONSE_BYTES,
        **({"before_dispatch": before_dispatch} if reference_owner else {}),
    )
    if reference_owner:
        reference_owner.route = {
            "instance_id": str(client.last_instance_id or backend_id),
            "pool_id": str(client.pool_id) if client.pool_id else None,
        }
    candidates, diagnostic = normalize_native_mask_response(
        response.result,
        getattr(response, "diagnostic", None),
        context=context,
        expected_size=expected_size,
    )
    return {
        "binding": binding,
        "candidates": candidates,
        "diagnostic": diagnostic,
        "backend_id": client.last_instance_id or backend_id,
        "pool_id": client.pool_id,
        "model_id": model_id,
        "model_version": getattr(response, "model_version", None),
        "inference_time_ms": getattr(response, "inference_time_ms", None),
    }


async def _repair_task(db, task_id, actor):
    from app.services.project_write_guard import (
        lock_actor_project_authority,
        lock_actor_scope,
    )
    from app.services.task_lock import TaskLockService
    from app.services.task_scope import lock_task_scope
    from app.services.content_snapshot import ContentError

    task = await db.get(Task, task_id, populate_existing=True)
    if task is None:
        raise ContentError("candidate_source_missing")
    await lock_actor_project_authority(db, actor.id, task.project_id)
    await lock_actor_scope(db, actor.id, task.project_id)
    await TaskLockService(db)._lock_task_scope(task_id, nowait=True)
    await lock_task_scope(db, [task_id])
    task = await db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    await authorize_repair_task(db, task, actor)
    return task


async def _candidate_source(db, item, actor, *, lock_annotation=True):
    task = await _repair_task(db, uuid.UUID(str(item["task_id"])), actor)
    query = select(Annotation).where(
        Annotation.id == uuid.UUID(str(item["annotation_id"]))
    )
    if lock_annotation:
        query = query.with_for_update(nowait=True)
    annotation = await db.scalar(query.execution_options(populate_existing=True))
    if (
        annotation is None
        or annotation.task_id != task.id
        or not annotation.is_active
        or annotation.was_cancelled
        or annotation.is_locked
        or int(annotation.version or 1) != int(item["source_version"])
    ):
        raise RuntimeError("candidate_source_version_conflict")
    return task, annotation


async def _commit_sam_candidate(
    db, *, item, batch_id, actor, prepared, reference_owner=None
):
    from app.services.reference_bindings import task_binding

    task, _ = await _candidate_source(db, item, actor, lock_annotation=False)
    _require_image_owner(task, reference_owner)
    if _is_managed(reference_owner):
        await _validate_unit_receipt(db, task.id, actor.id, reference_owner, item)
    if task_binding(task) != prepared["binding"]:
        raise RuntimeError("candidate_input_changed")
    candidates = prepared["candidates"]
    model_id = prepared["model_id"]
    geometries = [
        {
            "type": "raster_mask",
            "mask": build_rle_reference(dict(candidate["value"]["rle"])),
        }
        for candidate in candidates
    ]
    for geometry in geometries:
        await reserve_raster_mask_upload(
            db,
            task_id=task.id,
            object_key=str(geometry["mask"]["object_key"]),
            limit=_MASK_UPLOAD_LIMIT,
        )
    await lock_raster_mask_references(db, geometries, verify=False)
    for candidate, geometry in zip(candidates, geometries, strict=True):
        rle = dict(candidate["value"]["rle"])
        reference = geometry["mask"]
        await _store_result_rle(
            db,
            task_id=task.id,
            geometry=geometry,
            reference=reference,
            rle=rle,
        )
    # Raster upload/content locks precede annotation locks, matching ordinary
    # mask writes and GC. All candidates and the parent marker share one commit.
    task, annotation = await _candidate_source(db, item, actor)
    prediction_ids: list[str] = []
    candidate_records = {}
    for candidate_index, (candidate, geometry) in enumerate(
        zip(candidates, geometries, strict=True)
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
        prediction = await PredictionService(db).create_from_ml_result(
            task_id=task.id,
            project_id=task.project_id,
            ml_backend_id=prepared["backend_id"],
            ml_backend_pool_id=prepared["pool_id"],
            result=result,
            score=candidate.get("score"),
            model_version=prepared["model_version"],
            inference_time_ms=prepared["inference_time_ms"],
            source="mask_repair",
            **_prediction_receipt(reference_owner, result),
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
        )
        prediction_ids.append(str(prediction.id))
        if reference_owner:
            from app.db.models.prediction import PredictionMeta

            metadata = await db.scalar(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction.id
                )
            )
            candidate_records[str(prediction.id)] = {
                "result_digest": canonical_digest(prediction.result),
                "metadata_digest": canonical_digest(metadata.extra),
            }
    return {
        "status": "completed",
        "kind": "rerun_local_sam",
        "prediction_ids": prediction_ids,
        "candidate_count": len(prediction_ids),
        **({"candidate_records": candidate_records} if reference_owner else {}),
        "diagnostic": prepared["diagnostic"],
    }


async def _run_tracker_candidate(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    actor: User,
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
    from app.db.models.video_tracker_job import VideoTrackerJob

    child = await db.get(VideoTrackerJob, job.id)
    child.celery_task_id = str(child.id)
    return {
        "status": "dispatch_pending",
        "kind": "rerun_tracker",
        "tracker_job_id": str(job.id),
        "candidate_count": 1,
        "dispatch_state": "pending",
    }


def _failure_reason(exc):
    import re
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return str(
            detail.get("code") or detail.get("reason") or f"http_{exc.status_code}"
        )[:160]
    value = str(exc)
    return (
        value if re.fullmatch(r"[a-zA-Z0-9_:.,-]{1,160}", value) else type(exc).__name__
    )


async def _actor(db, attempt):
    actor = await db.get(User, attempt.actor_id, populate_existing=True)
    if actor is None or not actor.is_active:
        raise RuntimeError("repair_actor_unavailable")
    return actor


def _put_unit(batch, section, key, value):
    result = _result_copy(batch)
    result[section][key] = value
    batch.result_json = result


async def _progress(db, batch, attempt):
    _, _, total = progress_counts(batch)
    result = batch.result_json or {}
    observed = sum(
        len(result.get(section) or {}) for section in ("shards", "candidates")
    )
    await async_job_svc.update_progress(
        db, attempt.job_id, int(min(observed, total) / max(1, total) * 100)
    )


async def _dispatch_tracker_handoff(factory, batch_id, job_id, key, child_id):
    """Republish one durable child identity; an unknown broker result stays retryable."""
    from app.db.models.video_tracker_job import VideoTrackerJob
    from app.services.mask_repair_execution import lock_batch

    error = None
    async with factory() as db:
        child = await db.get(
            VideoTrackerJob, uuid.UUID(child_id), populate_existing=True
        )
        if child is None:
            raise RuntimeError("tracker_handoff_missing")
        should_publish = child.status == "queued"
        celery_id = child.celery_task_id
        await db.commit()
    if should_publish:
        try:
            celery_app.send_task(
                "app.workers.video_tracker.run_video_tracker_job",
                args=[child_id],
                task_id=celery_id,
                queue="gpu",
            )
        except Exception:
            error = "tracker_dispatch_unknown"
    async with factory() as db:
        await async_job_svc.lock_job(db, job_id)
        batch = await lock_batch(db, batch_id)
        if batch is None:
            return
        result = _result_copy(batch)
        current = dict(result["candidates"].get(key) or {})
        if current.get("tracker_job_id") != child_id:
            return
        if (
            current.get("dispatch_state") == "published"
            or current.get("status") == "completed"
        ):
            return
        child = await db.get(
            VideoTrackerJob, uuid.UUID(child_id), populate_existing=True
        )
        accepted = error is None or (child is not None and child.status != "queued")
        current.update(
            status="completed" if accepted else "dispatch_pending",
            dispatch_state="published" if accepted else "unknown",
        )
        if accepted:
            current.pop("reason", None)
        else:
            current["reason"] = error
        result["candidates"][key] = current
        batch.result_json = result
        await db.commit()


async def _reference_owner(db, factory, attempt, batch, section, unit, node_id):
    """Select one explicit source owner while keeping legacy reference call sites."""
    items = unit_items(batch, section, unit)
    if not items:
        return None
    task = await db.get(
        Task, uuid.UUID(str(items[0]["task_id"])), populate_existing=True
    )
    if any(item.get("reference_input") for item in items) or (
        task and is_reference(task.file_path)
    ):
        return ReferenceRepairUnit(db, factory, attempt, batch, items, node_id)
    if task and task.file_type == "image" and unit.get("kind") != "rerun_tracker":
        from app.services.managed_mask_repair import ManagedRepairUnit

        if any(not item.get("original_input") for item in items):
            raise ContentError("repair_original_requires_new_plan")
        return ManagedRepairUnit(db, factory, attempt, batch, items, node_id)
    if any(item.get("original_input") for item in items):
        raise ContentError("content_binding_changed")
    return None


async def _run_forward(
    factory, batch_id, celery_task_id, async_job_id=None, worker_node_id=None
):
    dispatch_context_factory = build_gpu_dispatch_context_factory(factory)
    async with factory() as db:
        attempt = await start_attempt(db, batch_id, async_job_id, celery_task_id)
        await db.commit()
        if attempt is None:
            return
        batch = await db.get(MaskRepairBatch, attempt.batch_id)
        plan = dict(batch.plan_json or {})
        items = list(plan.get("items") or [])
        units = [("shards", str(row["id"]), row) for row in plan.get("shards") or []]
        units += [
            ("candidates", str(index), item)
            for index, item in enumerate(items)
            if item.get("kind") in {"rerun_local_sam", "rerun_tracker"}
        ]
    for section, key, unit in units:
        child_id = None
        async with factory() as db:
            reference_owner = None
            failure = None
            try:
                batch, job = await lock_attempt(db, attempt)
                existing = (_result_copy(batch).get(section) or {}).get(key) or {}
                if existing.get("status") == "completed":
                    await db.commit()
                    continue
                child_id = (
                    existing.get("tracker_job_id") if section == "candidates" else None
                )
                if child_id:
                    await db.commit()
                else:
                    actor = await _actor(db, attempt)
                    reference_owner = await _reference_owner(
                        db, factory, attempt, batch, section, unit, worker_node_id
                    )
                    if section == "shards":
                        if reference_owner:
                            await db.commit()
                            await reference_owner.open()
                            await db.rollback()
                            batch, job = await lock_attempt(db, attempt)
                            actor = await _actor(db, attempt)
                            await reference_owner.final()
                        value = await _execute_shard(
                            db,
                            batch=batch,
                            shard=unit,
                            actor=actor,
                            **(
                                {"reference_owner": reference_owner}
                                if reference_owner
                                else {}
                            ),
                        )
                        batch.rollback_expires_at = (
                            datetime.now(timezone.utc) + ROLLBACK_TTL
                        )
                    elif unit["kind"] == "rerun_local_sam":
                        await _candidate_source(db, unit, actor, lock_annotation=False)
                        await db.commit()
                        if reference_owner:
                            await reference_owner.open()
                        prepared = await _prepare_sam_candidate(
                            db,
                            item=unit,
                            batch_id=attempt.batch_id,
                            dispatch_context_factory=dispatch_context_factory,
                            **(
                                {"reference_owner": reference_owner}
                                if reference_owner
                                else {}
                            ),
                        )
                        await db.rollback()
                        batch, job = await lock_attempt(db, attempt)
                        actor = await _actor(db, attempt)
                        if reference_owner:
                            await reference_owner.final()
                        value = await _commit_sam_candidate(
                            db,
                            item=unit,
                            batch_id=attempt.batch_id,
                            actor=actor,
                            prepared=prepared,
                            **(
                                {"reference_owner": reference_owner}
                                if reference_owner
                                else {}
                            ),
                        )
                    else:
                        await _candidate_source(db, unit, actor)
                        value = await _run_tracker_candidate(db, item=unit, actor=actor)
                        child_id = value["tracker_job_id"]
                    _put_unit(batch, section, key, value)
                    await _progress(db, batch, attempt)
                    if _is_managed(reference_owner):
                        await reference_owner.recheck_credentials()
                    await db.commit()
            except RepairAttemptStopped as exc:
                failure = exc
                await db.rollback()
                return
            except Exception as exc:
                failure = exc
                await db.rollback()
                try:
                    batch, job = await lock_attempt(db, attempt)
                except RepairAttemptStopped:
                    await db.rollback()
                    return
                saved = (_result_copy(batch).get(section) or {}).get(key) or {}
                # COMMIT may have succeeded before its response was lost.
                # Never replace durable work with an inferred failure.
                child_id = (
                    saved.get("tracker_job_id") if section == "candidates" else None
                )
                if saved.get("status") != "completed" and not child_id:
                    _put_unit(
                        batch,
                        section,
                        key,
                        {
                            "status": "failed",
                            "kind": unit.get("kind"),
                            "task_id": str(unit.get("task_id")),
                            "reason": _failure_reason(exc),
                        },
                    )
                    await _progress(db, batch, attempt)
                await db.commit()
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if reference_owner:
                    await _close_repair_owner(db, reference_owner, failure)
        if child_id:
            await _dispatch_tracker_handoff(
                factory, attempt.batch_id, attempt.job_id, key, child_id
            )
    async with factory() as db:
        try:
            batch, job = await lock_attempt(db, attempt)
        except RepairAttemptStopped:
            await db.rollback()
            return
        completed, failed, total = progress_counts(batch)
        batch.status = (
            "completed"
            if failed == 0 and total
            else ("partial" if committed_repair_units(batch) else "failed")
        )
        batch.completed_at = datetime.now(timezone.utc)
        summary = {
            "batch_id": str(batch.id),
            "success_count": completed,
            "failed_count": failed,
            "result_digest": canonical_digest(batch.result_json or {}),
        }
        if completed:
            await async_job_svc.mark_complete(db, attempt.job_id, result=summary)
        else:
            await async_job_svc.mark_failed(
                db, attempt.job_id, error="mask repair units unfinished", result=summary
            )
        await notify_job_terminal(db, job_id=attempt.job_id)
        await db.commit()


async def _run_mask_repair(
    batch_id, celery_task_id, async_job_id=None, worker_node_id=None
):
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _run_forward(
            factory, batch_id, celery_task_id, async_job_id, worker_node_id
        )
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
    reference_owner=None,
) -> int:
    entries = list(shard_result.get("items") or [])
    if entries:
        task_id = uuid.UUID(str(plan_items[int(entries[0]["item_index"])]["task_id"]))
        task = await _repair_task(db, task_id, actor)
        _require_image_owner(task, reference_owner)
        if reference_owner is None:
            await MaskMutationService(db)._lock_task(task_id)
        else:
            await _validate_unit_receipt(
                db, task_id, actor.id, reference_owner, shard_result
            )
        await lock_raster_mask_references(
            db,
            [
                plan_items[int(entry["item_index"])]["before_geometry"]
                for entry in entries
            ],
            verify=False,
        )
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
        response = await service.commit(
            uuid.UUID(str(item["task_id"])),
            payload,
            actor,
            **_mutation_receipt(reference_owner, payload),
        )
        if _is_managed(reference_owner):
            await reference_owner.note_committed_mutation(payload, response)
    return len(entries)


async def _run_rollback(
    factory, batch_id, celery_task_id, async_job_id=None, worker_node_id=None
):
    async with factory() as db:
        attempt = await start_attempt(
            db, batch_id, async_job_id, celery_task_id, rollback=True
        )
        await db.commit()
        if attempt is None:
            return
        batch = await db.get(MaskRepairBatch, attempt.batch_id)
        plan_items = list((batch.plan_json or {}).get("items") or [])
        # JSONB object key order is not execution order. Follow the immutable
        # plan so reversing this list really reverses the forward shards.
        saved = (batch.result_json or {}).get("shards") or {}
        completed_shards = [
            (str(shard["id"]), saved[str(shard["id"])])
            for shard in (batch.plan_json or {}).get("shards") or []
            if (saved.get(str(shard["id"])) or {}).get("status") == "completed"
        ]
    for shard_id, shard_result in reversed(completed_shards):
        async with factory() as db:
            reference_owner = None
            failure = None
            try:
                batch, job = await lock_attempt(db, attempt)
                result = _result_copy(batch)
                rollback = dict(result.get("rollback") or {})
                checkpoints = dict(rollback.get("shards") or {})
                if (checkpoints.get(shard_id) or {}).get("status") == "completed":
                    await db.commit()
                    continue
                actor = await _actor(db, attempt)
                shard_plan = next(
                    row
                    for row in batch.plan_json["shards"]
                    if str(row["id"]) == shard_id
                )
                reference_owner = await _reference_owner(
                    db, factory, attempt, batch, "shards", shard_plan, worker_node_id
                )
                if reference_owner:
                    await db.commit()
                    await reference_owner.open()
                    await db.rollback()
                    batch, job = await lock_attempt(db, attempt)
                    actor = await _actor(db, attempt)
                    await reference_owner.final()
                    result = _result_copy(batch)
                    rollback = dict(result.get("rollback") or {})
                    checkpoints = dict(rollback.get("shards") or {})
                count = await _rollback_shard(
                    db,
                    plan_items=plan_items,
                    shard_result=shard_result,
                    actor=actor,
                    **({"reference_owner": reference_owner} if reference_owner else {}),
                )
                versions = {}
                facts = {}
                for entry in shard_result.get("items") or []:
                    annotation_id = uuid.UUID(str(entry["annotation_id"]))
                    annotation = await db.get(
                        Annotation, annotation_id, populate_existing=True
                    )
                    versions[str(annotation_id)] = annotation.version
                    if _is_managed(reference_owner):
                        from app.services.managed_mask_repair import annotation_facts

                        facts[str(annotation_id)] = annotation_facts(annotation)
                checkpoints[shard_id] = {
                    "status": "completed",
                    "mutation_count": count,
                    "versions": versions,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    **(
                        {"annotation_facts": facts}
                        if _is_managed(reference_owner)
                        else {}
                    ),
                }
                rollback.update(status="running", shards=checkpoints)
                result["rollback"] = rollback
                batch.result_json = result
                if _is_managed(reference_owner):
                    await reference_owner.recheck_credentials()
                await db.commit()
            except RepairAttemptStopped as exc:
                failure = exc
                await db.rollback()
                return
            except Exception as exc:
                failure = exc
                await db.rollback()
                try:
                    batch, job = await lock_attempt(db, attempt)
                except RepairAttemptStopped:
                    await db.rollback()
                    return
                result = _result_copy(batch)
                rollback = dict(result.get("rollback") or {})
                if ((rollback.get("shards") or {}).get(shard_id) or {}).get(
                    "status"
                ) == "completed":
                    await db.commit()
                    continue
                rollback.update(
                    status="failed",
                    reason=_failure_reason(exc),
                    failed_shard_id=shard_id,
                    failed_at=datetime.now(timezone.utc).isoformat(),
                )
                result["rollback"] = rollback
                batch.result_json = result
                batch.status = "rollback_failed"
                await async_job_svc.mark_failed(
                    db, attempt.job_id, error=_failure_reason(exc)
                )
                await notify_job_terminal(db, job_id=attempt.job_id)
                await db.commit()
                return
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if reference_owner:
                    await _close_repair_owner(db, reference_owner, failure)
    async with factory() as db:
        try:
            batch, job = await lock_attempt(db, attempt)
        except RepairAttemptStopped:
            await db.rollback()
            return
        result = _result_copy(batch)
        rollback = dict(result.get("rollback") or {})
        count = sum(
            int(value.get("mutation_count") or 0)
            for value in (rollback.get("shards") or {}).values()
            if value.get("status") == "completed"
        )
        batch.rolled_back_at = datetime.now(timezone.utc)
        batch.status = "rolled_back"
        for key in ("reason", "failed_shard_id", "failed_at"):
            rollback.pop(key, None)
        rollback.update(
            status="completed",
            mutation_count=count,
            completed_at=batch.rolled_back_at.isoformat(),
        )
        result["rollback"] = rollback
        batch.result_json = result
        await async_job_svc.mark_complete(
            db,
            attempt.job_id,
            result={"batch_id": str(batch.id), "rolled_back_count": count},
        )
        await notify_job_terminal(db, job_id=attempt.job_id)
        await db.commit()


async def _rollback_mask_repair(
    batch_id, celery_task_id, async_job_id=None, worker_node_id=None
):
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        await _run_rollback(
            factory, batch_id, celery_task_id, async_job_id, worker_node_id
        )
    finally:
        await engine.dispose()


@celery_app.task(bind=True, name="app.workers.mask_repair.run_mask_repair")
def run_mask_repair(self, batch_id: str, *, async_job_id: str | None = None) -> None:
    asyncio.run(
        _run_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
            async_job_id,
            f"worker:{self.request.hostname}"
            if getattr(self.request, "hostname", None)
            else None,
        )
    )


@celery_app.task(bind=True, name="app.workers.mask_repair.rollback_mask_repair")
def rollback_mask_repair(
    self, batch_id: str, *, async_job_id: str | None = None
) -> None:
    asyncio.run(
        _rollback_mask_repair(
            batch_id,
            getattr(getattr(self, "request", None), "id", None),
            async_job_id,
            f"worker:{self.request.hostname}"
            if getattr(self.request, "hostname", None)
            else None,
        )
    )
