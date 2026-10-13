from __future__ import annotations

import logging
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import VideoSegment
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import (
    VideoTrackerJob,
    VideoTrackerJobKind,
    VideoTrackerJobStatus,
)
from app.schemas.video_tracker_job import (
    VideoMaskCorrectionRequest,
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoTrackerDecisionRequest,
    VideoTrackerJobOut,
    VideoTrackerPropagateRequest,
)
from app.services.project_access import is_privileged_for_project
from app.services.video_collaboration import (
    assert_task_lock_for_legacy_video,
    collaboration_config,
    segment_work_bounds,
)
from app.services.task_lock import TaskLockConflictError
from app.services.ml_backend import MLBackendService
from app.services.raster_mask_storage import (
    RasterMaskContractError,
    load_coco_rle,
    lock_raster_mask_references,
    validate_mask_geometry_for_task,
)
from app.services.video_frame_service import VideoContext
from app.services.video_segment_service import ensure_segments
from app.services.video_tracking.adapters import registered_tracker_models
from app.services.video_tracking import runner as _runner
from app.services.video_tracks import (
    is_polyline_track,
    normalize_outside_ranges,
    remove_manual_frame_from_outside_ranges,
    remove_frame_from_outside_ranges,
    resolve_track_at_frame,
)
from app.utils.raster_mask_rle import coco_rle_bbox_norm


log = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    VideoTrackerJobStatus.COMPLETED.value,
    VideoTrackerJobStatus.FAILED.value,
    VideoTrackerJobStatus.CANCELLED.value,
    # v0.21.28 · 候选/接受: 追踪已完成 (待审/已接受/已丢弃) 均不可再「取消追踪」。
    VideoTrackerJobStatus.PENDING_REVIEW.value,
    VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
    VideoTrackerJobStatus.ACCEPTED.value,
    VideoTrackerJobStatus.DISCARDED.value,
}

_ACTIVE_CORRECTION_STATUSES = {
    VideoTrackerJobStatus.QUEUED.value,
    VideoTrackerJobStatus.RUNNING.value,
    VideoTrackerJobStatus.PENDING_REVIEW.value,
    VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _assert_frame_range(ctx: VideoContext, from_frame: int, to_frame: int) -> None:
    if from_frame > to_frame:
        raise HTTPException(status_code=400, detail="Invalid frame range")
    if not ctx.metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")
    last_frame = max(0, int(ctx.metadata.frame_count) - 1)
    if to_frame > last_frame:
        raise HTTPException(status_code=400, detail="Frame range exceeds video length")


async def _load_annotation(
    db: AsyncSession, task: Task, annotation_id: uuid.UUID
) -> Annotation:
    annotation = await db.get(Annotation, annotation_id)
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    return annotation


async def _is_privileged(db: AsyncSession, task: Task, user: User) -> bool:
    project = await db.get(Project, task.project_id)
    return bool(project and is_privileged_for_project(user, project))


def _lock_valid_for_user(row: VideoSegment, user: User, now: datetime) -> bool:
    return bool(
        row.locked_by == user.id
        and row.lock_expires_at is not None
        and row.lock_expires_at > now
    )


async def _assert_segment_lock(
    db: AsyncSession,
    ctx: VideoContext,
    payload: VideoTrackerPropagateRequest,
    user: User,
    *,
    privileged: bool,
) -> uuid.UUID:
    await ensure_segments(db, ctx)
    now = _now()
    project = await db.get(Project, ctx.task.project_id) if ctx.task else None
    collaboration = collaboration_config(project)

    if collaboration.enabled and payload.segment_id is None:
        raise HTTPException(
            status_code=422, detail={"reason": "video_segment_required"}
        )

    async def _work_bounds(segment: VideoSegment) -> tuple[int, int]:
        if not collaboration.enabled:
            return segment.start_frame, segment.end_frame
        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(VideoSegment)
                    .where(VideoSegment.dataset_item_id == ctx.item.id)
                )
            ).scalar_one()
        )
        return segment_work_bounds(
            start_frame=segment.start_frame,
            end_frame=segment.end_frame,
            segment_index=segment.segment_index,
            segment_count=max(1, count),
            frame_count=max(1, int(ctx.metadata.frame_count or 1)),
            overlap_frames=collaboration.overlap_frames,
        )

    if payload.segment_id is not None:
        segment = (
            await db.execute(
                select(VideoSegment)
                .where(
                    VideoSegment.id == payload.segment_id,
                    VideoSegment.dataset_item_id == ctx.item.id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if segment is None:
            raise HTTPException(status_code=404, detail="Video segment not found")
        work_start, work_end = await _work_bounds(segment)
        if payload.from_frame < work_start or payload.to_frame > work_end:
            raise HTTPException(
                status_code=400, detail="Frame range is outside the video segment"
            )
        if (collaboration.enabled or not privileged) and not _lock_valid_for_user(
            segment, user, now
        ):
            raise HTTPException(
                status_code=409, detail="Video segment must be locked by current user"
            )
        if collaboration.enabled and segment.assignee_id != user.id:
            raise HTTPException(
                status_code=409, detail="Video segment must be assigned to current user"
            )
        return segment.id

    overlapping = (
        (
            await db.execute(
                select(VideoSegment)
                .where(
                    VideoSegment.dataset_item_id == ctx.item.id,
                    VideoSegment.start_frame <= payload.to_frame,
                    VideoSegment.end_frame >= payload.from_frame,
                )
                .order_by(VideoSegment.segment_index.asc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not overlapping:
        raise HTTPException(status_code=404, detail="Video segment not found")
    if len(overlapping) > 1:
        raise HTTPException(status_code=400, detail="Frame range crosses segments")
    if not privileged and not _lock_valid_for_user(overlapping[0], user, now):
        raise HTTPException(
            status_code=409, detail="Video segment must be locked by current user"
        )
    return overlapping[0].id


def _keyframe_digest(value: dict | None) -> str | None:
    if not value:
        return None
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


async def _assert_task_write_allowed(
    db: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    task = await db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        await assert_task_lock_for_legacy_video(db, task, user_id)
    except TaskLockConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"reason": "task_lock_conflict"}
        ) from exc


async def save_video_mask_keyframe(
    db: AsyncSession,
    *,
    task: Task,
    ctx: VideoContext,
    annotation_id: uuid.UUID,
    frame_index: int,
    payload: VideoMaskKeyframeSaveRequest,
    expected_version: int,
    user: User,
) -> tuple[Annotation, dict[str, Any]]:
    """Persist one Mask keyframe without accepting a whole-track geometry."""

    _assert_frame_range(ctx, frame_index, frame_index)
    # Match atomic Mask mutation ordering. The Task row is the first database
    # row lock and serializes same-task writers before content/annotation locks.
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_write_allowed(db, task.id, user.id)
    annotation = (
        await db.execute(select(Annotation).where(Annotation.id == annotation_id))
    ).scalar_one_or_none()
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if annotation.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})
    if int(annotation.version or 1) != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_version_conflict",
                "current_version": int(annotation.version or 1),
            },
        )
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_mask":
        raise HTTPException(
            status_code=409,
            detail={"reason": "video_mask_track_required"},
        )

    privileged = await _is_privileged(db, task, user)
    segment_id = await _assert_segment_lock(
        db,
        ctx,
        SimpleNamespace(
            from_frame=frame_index,
            to_frame=frame_index,
            segment_id=getattr(annotation, "video_segment_id", None),
        ),
        user,
        privileged=privileged,
    )
    keyframes = [dict(item) for item in geometry.get("keyframes") or []]
    before = next(
        (
            dict(item)
            for item in keyframes
            if int(item.get("frame_index", -1)) == frame_index
        ),
        None,
    )
    keyframe = {
        "frame_index": frame_index,
        "mask": payload.mask.model_dump(mode="json"),
        "source": payload.source,
        "occluded": payload.occluded,
        **(
            {"attributes": payload.attributes} if payload.attributes is not None else {}
        ),
    }
    next_geometry = {
        **geometry,
        "keyframes": sorted(
            [
                item
                for item in keyframes
                if int(item.get("frame_index", -1)) != frame_index
            ]
            + [keyframe],
            key=lambda item: int(item.get("frame_index", 0)),
        ),
        "outside": remove_frame_from_outside_ranges(
            geometry.get("outside") or [], frame_index
        ),
    }
    try:
        await validate_mask_geometry_for_task(db, task, next_geometry)
        await lock_raster_mask_references(
            db, next_geometry, task_id=task.id, require_raster_foreground=False
        )
    except RasterMaskContractError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # RLE locks precede the Annotation row lock. Recheck every mutable
    # precondition after acquiring the row so a non-compliant legacy writer
    # cannot publish over this save.
    annotation = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if annotation.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})
    if int(annotation.version or 1) != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_version_conflict",
                "current_version": int(annotation.version or 1),
            },
        )
    if (annotation.geometry or {}).get("type") != "video_track_mask":
        raise HTTPException(
            status_code=409,
            detail={"reason": "video_mask_track_required"},
        )

    source_version = int(annotation.version or 1)
    annotation.geometry = next_geometry
    annotation.version = source_version + 1
    await db.flush()
    await db.refresh(annotation)
    return annotation, {
        "task_id": str(task.id),
        "track_id": str(annotation.track_id or geometry.get("track_id") or ""),
        "segment_id": str(segment_id),
        "frame_index": frame_index,
        "source_version": source_version,
        "result_version": int(annotation.version),
        "before_digest": _keyframe_digest(before),
        "after_digest": _keyframe_digest(keyframe),
        "mask_digest": payload.mask.sha256,
    }


async def operate_video_mask_keyframe(
    db: AsyncSession,
    *,
    task: Task,
    ctx: VideoContext,
    annotation_id: uuid.UUID,
    frame_index: int,
    payload: VideoMaskKeyframeOperationRequest,
    expected_version: int,
    user: User,
) -> tuple[Annotation, dict[str, Any]]:
    """Apply one versioned Mask-track frame-state operation.

    The lock order mirrors Mask persistence: Task, video segment, then Annotation.
    These operations never introduce a new content reference, so no RLE lock is
    needed between the segment and Annotation locks.
    """

    _assert_frame_range(ctx, frame_index, frame_index)
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_write_allowed(db, task.id, user.id)

    annotation = (
        await db.execute(select(Annotation).where(Annotation.id == annotation_id))
    ).scalar_one_or_none()
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if annotation.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})
    if int(annotation.version or 1) != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_version_conflict",
                "current_version": int(annotation.version or 1),
            },
        )
    if (annotation.geometry or {}).get("type") != "video_track_mask":
        raise HTTPException(
            status_code=409, detail={"reason": "video_mask_track_required"}
        )

    privileged = await _is_privileged(db, task, user)
    segment_id = await _assert_segment_lock(
        db,
        ctx,
        SimpleNamespace(
            from_frame=frame_index,
            to_frame=frame_index,
            segment_id=getattr(annotation, "video_segment_id", None),
        ),
        user,
        privileged=privileged,
    )

    annotation = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if annotation.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})
    if int(annotation.version or 1) != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_version_conflict",
                "current_version": int(annotation.version or 1),
            },
        )
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_mask":
        raise HTTPException(
            status_code=409, detail={"reason": "video_mask_track_required"}
        )

    keyframes = [dict(item) for item in geometry.get("keyframes") or []]
    outside = [dict(item) for item in geometry.get("outside") or []]
    exact = next(
        (item for item in keyframes if int(item.get("frame_index", -1)) == frame_index),
        None,
    )
    before_geometry = {**geometry, "keyframes": keyframes, "outside": outside}

    if payload.operation == "delete_keyframe":
        if exact is None:
            raise HTTPException(status_code=409, detail={"reason": "keyframe_missing"})
        if len(keyframes) <= 1:
            raise HTTPException(
                status_code=422, detail={"reason": "last_keyframe_required"}
            )
        next_geometry = {
            **geometry,
            "keyframes": [item for item in keyframes if item is not exact],
            "outside": outside,
        }
    elif payload.operation == "mark_outside":
        already_outside = any(
            item.get("source") != "prediction"
            and int(item.get("from", -1)) <= frame_index <= int(item.get("to", -1))
            for item in normalize_outside_ranges(outside)
        )
        if already_outside:
            raise HTTPException(
                status_code=409, detail={"reason": "frame_already_outside"}
            )
        next_geometry = {
            **geometry,
            "keyframes": keyframes,
            "outside": normalize_outside_ranges(
                [
                    *outside,
                    {"from": frame_index, "to": frame_index, "source": "manual"},
                ]
            ),
        }
    else:
        next_outside, removed = remove_manual_frame_from_outside_ranges(
            outside, frame_index
        )
        if not removed:
            raise HTTPException(
                status_code=409, detail={"reason": "manual_outside_missing"}
            )
        next_geometry = {
            **geometry,
            "keyframes": keyframes,
            "outside": next_outside,
        }

    source_version = int(annotation.version or 1)
    annotation.geometry = next_geometry
    annotation.version = source_version + 1
    await db.flush()
    await db.refresh(annotation)
    resolved = resolve_track_at_frame(next_geometry, frame_index)
    normalized_outside = normalize_outside_ranges(next_geometry.get("outside") or [])
    visible_keyframes = [
        item
        for item in next_geometry.get("keyframes") or []
        if not any(
            int(range_["from"]) <= int(item.get("frame_index", -1)) <= int(range_["to"])
            for range_ in normalized_outside
        )
    ]
    resolved_keyframe = (
        min(
            visible_keyframes,
            key=lambda item: (
                abs(int(item.get("frame_index", 0)) - frame_index),
                int(item.get("frame_index", 0)),
            ),
        )
        if resolved is not None and visible_keyframes
        else None
    )
    resolved_keyframe_frame = (
        int(resolved_keyframe["frame_index"]) if resolved_keyframe is not None else None
    )
    return annotation, {
        "task_id": str(task.id),
        "track_id": str(annotation.track_id or geometry.get("track_id") or ""),
        "segment_id": str(segment_id),
        "frame_index": frame_index,
        "operation": payload.operation,
        "source_version": source_version,
        "result_version": int(annotation.version),
        "before_digest": _keyframe_digest(before_geometry),
        "after_digest": _keyframe_digest(next_geometry),
        "resolved_keyframe_frame": resolved_keyframe_frame,
        "restored_held": bool(
            resolved_keyframe_frame is not None
            and resolved_keyframe_frame != frame_index
        ),
    }


def _tracker_model_capability(
    backend: Any, model_key: str, model_id: str
) -> dict | None:
    capabilities = (backend.health_meta or {}).get("capabilities") or {}
    for model in capabilities.get("models") or []:
        if str(model.get("id") or "") == model_id and model_key in set(
            model.get("supported_trackers") or []
        ):
            return model
    return None


def _native_correction_supported(model: dict) -> bool:
    prompts = set(model.get("supported_prompts") or [])
    inputs = set(model.get("supported_inputs") or [])
    outputs = set(model.get("supported_geometric_outputs") or [])
    return (
        "correction_frame" in prompts
        and "mask_prompt" in inputs
        and "video" in inputs
        and "mask" in outputs
    )


def _bbox_correction_supported(model: dict) -> bool:
    inputs = set(model.get("supported_inputs") or [])
    outputs = set(model.get("supported_geometric_outputs") or [])
    return "video" in inputs and "bbox_prompt" in inputs and "mask" in outputs


async def enqueue_tracker_job(
    db: AsyncSession,
    row_or_id: VideoTrackerJob | uuid.UUID,
    *,
    fail_closed: bool = False,
) -> VideoTrackerJobOut:
    row = (
        row_or_id
        if isinstance(row_or_id, VideoTrackerJob)
        else await db.get(VideoTrackerJob, row_or_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    row_id = row.id
    try:
        from celery import current_app

        result = current_app.send_task(
            "app.workers.video_tracker.run_video_tracker_job",
            args=[str(row_id)],
            queue="gpu",
        )
        row.celery_task_id = result.id
        await db.commit()
        await db.refresh(row)
    except Exception as exc:
        log.warning("video tracker job enqueue failed job_id=%s err=%s", row_id, exc)
        await db.rollback()
        row = (
            await db.execute(
                select(VideoTrackerJob)
                .where(VideoTrackerJob.id == row_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(
                status_code=404, detail="Video tracker job not found"
            ) from exc
        if row.status == VideoTrackerJobStatus.QUEUED.value:
            row.status = VideoTrackerJobStatus.FAILED.value
            row.error_message = "tracker_enqueue_failed"
            row.completed_at = _now()
            row.staged_result = None
        await db.commit()
        await db.refresh(row)
        if fail_closed:
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "tracker_enqueue_failed",
                    "job_id": str(row.id),
                    "retryable": True,
                },
            ) from exc
    return _job_out(row)


async def create_video_mask_correction_job(
    db: AsyncSession,
    *,
    task: Task,
    ctx: VideoContext,
    annotation_id: uuid.UUID,
    payload: VideoMaskCorrectionRequest,
    user: User,
) -> VideoTrackerJobOut:
    _assert_frame_range(ctx, payload.from_frame, payload.to_frame)
    annotation = (
        await db.execute(
            select(Annotation)
            .where(Annotation.id == annotation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if annotation is None or not annotation.is_active:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if annotation.task_id != task.id:
        raise HTTPException(
            status_code=400, detail="Annotation does not belong to this task"
        )
    if annotation.is_locked:
        raise HTTPException(status_code=409, detail={"reason": "annotation_locked"})
    geometry = annotation.geometry or {}
    if geometry.get("type") != "video_track_mask":
        raise HTTPException(
            status_code=409, detail={"reason": "video_mask_track_required"}
        )
    current_version = int(annotation.version or 1)
    if current_version != payload.source_annotation_version:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "source_version_conflict",
                "current_version": current_version,
            },
        )
    track_id = str(annotation.track_id or geometry.get("track_id") or "")
    if not track_id:
        raise HTTPException(
            status_code=409, detail={"reason": "track_identity_missing"}
        )
    keyframe = next(
        (
            item
            for item in geometry.get("keyframes") or []
            if int(item.get("frame_index", -1)) == payload.correction_frame
        ),
        None,
    )
    if not isinstance(keyframe, dict) or keyframe.get("source") != "manual":
        raise HTTPException(
            status_code=409, detail={"reason": "manual_correction_frame_required"}
        )
    reference = keyframe.get("mask") or {}
    current_digest = str(reference.get("sha256") or "")
    if current_digest != payload.corrected_mask_digest:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "corrected_mask_digest_mismatch",
                "current_digest": current_digest,
            },
        )

    existing = (
        await db.execute(
            select(VideoTrackerJob.id).where(
                VideoTrackerJob.task_id == task.id,
                VideoTrackerJob.job_kind == VideoTrackerJobKind.CORRECTION.value,
                VideoTrackerJob.track_id_snapshot == track_id,
                VideoTrackerJob.status.in_(_ACTIVE_CORRECTION_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"reason": "correction_job_active", "job_id": str(existing)},
        )

    backend_service = MLBackendService(db)
    backend = await backend_service.get(payload.backend_id)
    capabilities = (backend.health_meta or {}).get("capabilities") if backend else {}
    if (
        backend is None
        or backend.state != "connected"
        or not await backend_service.is_enabled(task.project_id, payload.backend_id)
        or payload.model_key
        not in set((capabilities or {}).get("supported_trackers") or [])
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "tracker_backend_unavailable",
                "model_key": payload.model_key,
                "backend_id": str(payload.backend_id),
            },
        )
    model = _tracker_model_capability(backend, payload.model_key, payload.model_id)
    if model is None:
        raise HTTPException(
            status_code=422,
            detail={"reason": "tracker_model_capability_missing"},
        )
    outputs = set(model.get("supported_geometric_outputs") or [])
    if "mask" not in outputs:
        raise HTTPException(
            status_code=422,
            detail={"reason": "mask_output_unsupported"},
        )
    native_mask = _native_correction_supported(model)
    bbox_fallback = _bbox_correction_supported(model)
    rle = await load_coco_rle(reference)
    seed_bbox = coco_rle_bbox_norm(rle)
    if not seed_bbox:
        raise HTTPException(status_code=422, detail={"reason": "empty_correction_mask"})
    if not native_mask and not bbox_fallback:
        raise HTTPException(
            status_code=422,
            detail={"reason": "correction_prompt_unsupported"},
        )
    if not native_mask and not payload.allow_bbox_fallback:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "mask_prompt_unsupported",
                "fallback_available": True,
                "seed_bbox": seed_bbox,
                "model_key": payload.model_key,
            },
        )
    text_driven = payload.model_key in set(model.get("text_driven_trackers") or [])
    if not native_mask and text_driven and not payload.text:
        raise HTTPException(
            status_code=422,
            detail={"reason": "text_required_for_bbox_fallback"},
        )

    platform_window_limit = (
        int(settings.video_tracker_sam3_window_size_frames)
        if payload.model_key.startswith("sam3")
        else int(settings.video_tracker_window_size_frames)
    )
    backend_window_limit = model.get("max_window_frames")
    if type(backend_window_limit) is not int or backend_window_limit <= 0:
        raise HTTPException(
            status_code=422,
            detail={"reason": "tracker_window_capability_missing"},
        )
    native_window_limit = min(platform_window_limit, backend_window_limit)
    directional_spans = []
    if payload.direction in {"backward", "bidirectional"}:
        directional_spans.append(payload.correction_frame - payload.from_frame + 1)
    if payload.direction in {"forward", "bidirectional"}:
        directional_spans.append(payload.to_frame - payload.correction_frame + 1)
    if native_mask and any(span > native_window_limit for span in directional_spans):
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "correction_window_exceeds_native_limit",
                "max_frames_per_direction": native_window_limit,
            },
        )

    privileged = await _is_privileged(db, task, user)
    segment_id = await _assert_segment_lock(
        db, ctx, payload, user, privileged=privileged
    )
    segment = await db.get(VideoSegment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Video segment not found")
    pool_id = await backend_service.pool_id_for_registry(backend.id)
    if pool_id is None:
        raise HTTPException(
            status_code=422, detail={"reason": "tracker_backend_pool_missing"}
        )
    correction = {
        "schema_version": 1,
        "source_annotation_id": str(annotation.id),
        "track_id": track_id,
        "source_version": current_version,
        "correction_frame": payload.correction_frame,
        "corrected_digest": current_digest,
        "from_frame": payload.from_frame,
        "to_frame": payload.to_frame,
        "direction": payload.direction,
        "protect_manual": True,
        "segment": {
            "id": str(segment_id),
            "lease_enforced": not privileged,
            "locked_by": str(segment.locked_by) if segment.locked_by else None,
            "locked_at": segment.locked_at.isoformat() if segment.locked_at else None,
        },
        "routing": {
            "requested_backend_id": str(backend.id),
            "backend_pool_id": str(pool_id),
            "model_id": payload.model_id,
            "model_key": payload.model_key,
            "max_window_frames": native_window_limit,
        },
        "seed_mode": "native_mask" if native_mask else "bbox",
        "fallback_reason": None if native_mask else "mask_prompt_unsupported",
        "fallback_confirmed": bool(not native_mask and payload.allow_bbox_fallback),
        "seed_bbox": None if native_mask else seed_bbox,
    }
    row = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=ctx.item.id,
        annotation_id=annotation.id,
        segment_id=segment_id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        job_kind=VideoTrackerJobKind.CORRECTION.value,
        track_id_snapshot=track_id,
        correction_frame=payload.correction_frame,
        model_key=payload.model_key,
        direction=payload.direction,
        from_frame=payload.from_frame,
        to_frame=payload.to_frame,
        prompt={
            "output_geometry": "mask",
            "expected_source_versions": {str(annotation.id): current_version},
            "correction": correction,
            **({"text": payload.text} if payload.text else {}),
            **({"sam_variant": payload.sam_variant} if payload.sam_variant else {}),
        },
        event_channel="pending",
    )
    db.add(row)
    try:
        await db.flush()
        row.event_channel = f"video-tracker-job:{row.id}"
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail={"reason": "correction_job_active"}
        ) from exc
    return _job_out(row)


def _job_out(row: VideoTrackerJob) -> VideoTrackerJobOut:
    return VideoTrackerJobOut.model_validate(row, from_attributes=True)


async def _seed_geometry_at_frame(annotation: Annotation, from_frame: int) -> dict:
    """多源 seed 的源几何: 视频轨迹取 from_frame 处的几何 (转成 backend 可解析的 result-style
    geometry), 取不到 (非轨迹 / outside / 无该帧) 则回退整条 annotation.geometry。

    backend _seed_bbox_from_video_ctx 吃 {type: bbox/polygon, ...} 或 video_track_*, 但不吃
    resolve_track_at_frame 返回的裸关键帧 (无 type、bbox 嵌套) → bbox/polygon 帧转 result-style。
    """
    geometry = annotation.geometry or {}
    resolved = resolve_track_at_frame(geometry, from_frame)
    if resolved is None:
        return geometry
    if resolved.get("points") is not None:
        return {"type": "polygon", "points": resolved["points"]}
    if resolved.get("bbox") is not None:
        return {"type": "bbox", **resolved["bbox"]}
    if resolved.get("mask") is not None:
        bbox = coco_rle_bbox_norm(await load_coco_rle(resolved["mask"]))
        if bbox:
            return {"type": "bbox", **bbox}
    return geometry


async def _validate_sourceless_target(
    db: AsyncSession,
    project_id: uuid.UUID,
    class_name: str | None,
    tool_unit_id: str | None,
) -> None:
    if not class_name or not tool_unit_id:
        raise HTTPException(
            status_code=422,
            detail="Sourceless tracking requires target_class_name and target_tool_unit_id",
        )
    project = await db.get(Project, project_id)
    binding = (project.tool_bindings or {}).get(tool_unit_id) if project else None
    classes = binding.get("classes") if isinstance(binding, dict) else None
    allowed = {
        item.get("name")
        for item in (classes or [])
        if isinstance(item, dict) and item.get("name")
    }
    if (
        not isinstance(binding, dict)
        or not binding.get("enabled")
        or class_name not in allowed
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"target class '{class_name}' is not configured for tool unit "
                f"'{tool_unit_id}'"
            ),
        )


async def create_tracker_job(
    db: AsyncSession,
    *,
    task: Task,
    ctx: VideoContext,
    annotation_id: uuid.UUID | None,
    payload: VideoTrackerPropagateRequest,
    user: User,
) -> VideoTrackerJobOut:
    _assert_frame_range(ctx, payload.from_frame, payload.to_frame)
    known_models = set(registered_tracker_models()) | {"sam3_video_combo"}
    if payload.model_key not in known_models:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported tracker model: {payload.model_key}",
        )
    if payload.model_key != "mock_bbox":
        required = (
            ["sam3_video", "sam3_video_interactive"]
            if payload.model_key == "sam3_video_combo"
            else [payload.model_key]
        )
        backend = await MLBackendService(db).get_tracker_backend_for_capabilities(
            task.project_id, required
        )
        if backend is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "No connected project ML backend supports tracker model: "
                    f"{payload.model_key}"
                ),
            )
    # v0.22.2 · M · 多选批量: source_annotation_ids 有 ≥1 条 → 多源分支。各源在 from_frame 处
    # 的几何写成带 source_annotation_id + obj_id (1..N) 的 seed; annotation_id 强制 NULL (§8 ·
    # 多源不认单主, accept 各 obj 各回填各源, 见 _seed_source_map)。obj_id 与 instance_id ==
    # str(obj_id) 契约一致。单数 source_annotation_id (单源延展) / 无源路径保持现状不变。
    multi_source_ids = payload.source_annotation_ids or []
    seed_prompt: dict = {}
    # v0.23.5 · WS-D · D4 · record source annotation versions at job creation
    # so accept_tracker_job can detect a concurrent mutation of any source
    # annotation and refuse with 409 instead of last-writer-wins. Keyed by the
    # stringified annotation id (matches ``_seed_source_map``'s keying).
    expected_source_versions: dict[str, int] = {}
    if multi_source_ids:
        annotation_id = None
        seeds: list[dict] = []
        for obj_id, sid in enumerate(multi_source_ids, start=1):
            source = await _load_annotation(db, task, sid)
            if is_polyline_track(source.geometry or {}):
                raise HTTPException(status_code=400, detail="polyline 轨迹追踪暂不支持")
            expected_source_versions[str(sid)] = int(source.version or 1)
            seeds.append(
                {
                    "obj_id": obj_id,
                    "source_annotation_id": str(sid),
                    "geometry": await _seed_geometry_at_frame(
                        source, payload.from_frame
                    ),
                }
            )
        seed_prompt = {"seeds": seeds}
    # v0.22.1 · B · 源轨迹可选: 无源检测 (annotation_id is None) 不加载 source / 不查 polyline,
    # 新建轨迹类别由 payload.target_* 显式指定。有源延展保留 polyline 400 拒绝。
    elif annotation_id is not None:
        annotation = await _load_annotation(db, task, annotation_id)
        # polyline 轨迹追踪暂不支持: runner 只识别 polygon/bbox track, polyline 会命中 bbox
        # fallback → 原关键帧被静默改写成空 bbox 轨迹 (points 全丢), 故在入口用 400 明确拒绝。
        if is_polyline_track(annotation.geometry or {}):
            raise HTTPException(status_code=400, detail="polyline 轨迹追踪暂不支持")
        expected_source_versions[str(annotation_id)] = int(annotation.version or 1)
    sourceless = annotation_id is None and not multi_source_ids
    if sourceless:
        await _validate_sourceless_target(
            db,
            task.project_id,
            payload.target_class_name,
            payload.target_tool_unit_id,
        )
    privileged = await _is_privileged(db, task, user)
    segment_id = await _assert_segment_lock(
        db, ctx, payload, user, privileged=privileged
    )

    # v0.22.1 · B · 无源检测 (无单主且非多源) 才存显式目标类别; 有源延展 / 多源批量各自继承源。
    row = VideoTrackerJob(
        task_id=task.id,
        dataset_item_id=ctx.item.id,
        annotation_id=annotation_id,
        target_class_name=payload.target_class_name if sourceless else None,
        target_tool_unit_id=payload.target_tool_unit_id if sourceless else None,
        segment_id=segment_id,
        created_by=user.id,
        status=VideoTrackerJobStatus.QUEUED.value,
        model_key=payload.model_key,
        direction=payload.direction,
        from_frame=payload.from_frame,
        to_frame=payload.to_frame,
        # v0.10.36 · sam_variant 存进自由 JSONB prompt (无需 DB 迁移).
        # v0.21.19 · text-driven 追踪的 text/exemplars 同样落 prompt JSONB, runner 读出后
        # 经 TrackerContext 显式字段透传到 backend (见 video_tracker_adapters context)。
        prompt={
            **(payload.prompt or {}),
            # v0.22.2 · M · 多源 seeds (各 obj_id ↔ 各源) 覆盖 payload.prompt 里同名键。
            **seed_prompt,
            **({"sam_variant": payload.sam_variant} if payload.sam_variant else {}),
            **({"text": payload.text} if payload.text else {}),
            **(
                {"output_geometry": payload.output_geometry}
                if payload.output_geometry
                else {}
            ),
            **(
                {"exemplars": [e.model_dump() for e in payload.exemplars]}
                if payload.exemplars
                else {}
            ),
            # v0.23.5 · WS-D · D4 · snapshot of source annotation versions at job
            # creation; accept_tracker_job re-reads with_for_update and 409s on
            # any drift. Source-backed legacy jobs without this proof fail closed.
            **(
                {"expected_source_versions": expected_source_versions}
                if expected_source_versions
                else {}
            ),
        },
        event_channel="pending",
    )
    db.add(row)
    await db.flush()
    row.event_channel = f"video-tracker-job:{row.id}"
    await db.commit()
    await db.refresh(row)

    await enqueue_tracker_job(db, row)

    return _job_out(row)


async def get_tracker_job(db: AsyncSession, job_id: uuid.UUID) -> VideoTrackerJob:
    row = await db.get(VideoTrackerJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    return row


async def cancel_tracker_job(db: AsyncSession, job_id: uuid.UUID) -> VideoTrackerJobOut:
    row = (
        await db.execute(
            select(VideoTrackerJob)
            .where(VideoTrackerJob.id == job_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    is_correction = row.job_kind == VideoTrackerJobKind.CORRECTION.value
    if is_correction and row.status in {
        VideoTrackerJobStatus.ACCEPTED.value,
        VideoTrackerJobStatus.DISCARDED.value,
        VideoTrackerJobStatus.FAILED.value,
    }:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"reason": "correction_job_terminal", "status": row.status},
        )
    if is_correction and row.status in {
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
    }:
        now = _now()
        row.status = VideoTrackerJobStatus.CANCELLED.value
        row.staged_result = None
        row.cancel_requested_at = row.cancel_requested_at or now
        row.completed_at = row.completed_at or now
    elif row.status in {
        VideoTrackerJobStatus.PENDING_REVIEW.value,
        VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
    }:
        # 候选待审不是"运行中"任务, 不能 cancel; 让前端据 409 引导用户改用 discard,
        # 而不是静默返回 200 让人以为取消没生效。
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Candidate awaiting review cannot be cancelled; discard it instead",
        )
    if row.status not in _TERMINAL_STATUSES:
        now = _now()
        row.status = VideoTrackerJobStatus.CANCELLED.value
        if is_correction:
            row.staged_result = None
        row.cancel_requested_at = row.cancel_requested_at or now
        row.completed_at = row.completed_at or now
    await db.flush()
    await db.refresh(row)
    return _job_out(row)


async def accept_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
    privileged: bool = False,
) -> VideoTrackerJobOut:
    """v0.21.28 · 接受候选: 把 job.staged_result 应用到 annotation, status=ACCEPTED。"""
    try:
        row = await _runner.accept_tracker_job(
            db, job_id, actor_id=actor_id, privileged=privileged
        )
    except _runner.TrackerJobStateConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    return _job_out(row)


async def discard_tracker_job(
    db: AsyncSession, job_id: uuid.UUID
) -> VideoTrackerJobOut:
    """v0.21.28 · 丢弃候选: status=DISCARDED, 清 staged_result, annotation 零改动。"""
    try:
        row = await _runner.discard_tracker_job(db, job_id)
    except _runner.TrackerJobStateConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    return _job_out(row)


async def decide_tracker_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    body: VideoTrackerDecisionRequest,
    *,
    actor_id: uuid.UUID | None = None,
    privileged: bool = False,
) -> VideoTrackerJobOut:
    try:
        row = await _runner.decide_tracker_job(
            db,
            job_id,
            instance_ids=body.instance_ids,
            from_frame=body.from_frame,
            to_frame=body.to_frame,
            decision=body.decision,
            expected_source_versions=body.expected_source_versions,
            job_revision=body.job_revision,
            qc_issue_id=body.qc_issue_id,
            candidate_digest=body.candidate_digest,
            override_manual=body.override_manual,
            actor_id=actor_id,
            privileged=privileged,
            commit=False,
        )
    except _runner.TrackerJobStateConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    result = _job_out(row)
    result.review_replayed = bool(getattr(row, "_review_replayed", False))
    return result


async def tracker_job_preview(db: AsyncSession, row: VideoTrackerJob) -> dict[str, Any]:
    staged = dict(row.staged_result or {})
    results = [
        _runner._ensure_candidate_contract(item)
        for item in (staged.get("results") or [])
        if isinstance(item, dict)
    ]
    source_ids = _runner._review_source_map_ids(row, staged)
    annotation_ids = set(source_ids.values())
    annotations = {}
    if annotation_ids:
        annotations = {
            annotation.id: annotation
            for annotation in (
                (
                    await db.execute(
                        select(Annotation).where(Annotation.id.in_(annotation_ids))
                    )
                )
                .scalars()
                .all()
            )
        }
    state = _runner._review_state(row)
    instance_annotations = dict(state.get("instance_annotations") or {})
    hydrated: list[dict[str, Any]] = []
    for item in results:
        instance_id = _runner._tracker_instance_key(item)
        source_id = source_ids.get(instance_id)
        target_id = instance_annotations.get(instance_id) or source_id
        source = annotations.get(source_id) if source_id else None
        frame_index = int(item["frame_index"])
        manual_protected = bool(
            source
            and any(
                int(keyframe.get("frame_index", -1)) == frame_index
                and keyframe.get("source", "manual") == "manual"
                for keyframe in (source.geometry or {}).get("keyframes") or []
            )
        )
        hydrated.append(
            {
                **item,
                "source_annotation_id": source_id,
                "target_annotation_id": target_id,
                "manual_protected": manual_protected,
            }
        )
    decisions = state.get("decisions") or {}
    accepted = sum(
        1
        for item in decisions.values()
        if isinstance(item, dict) and item.get("decision") == "accept"
    )
    rejected = sum(
        1
        for item in decisions.values()
        if isinstance(item, dict) and item.get("decision") == "reject"
    )
    expected_versions = {
        str(key): int(value)
        for key, value in (
            (row.prompt or {}).get("expected_source_versions") or {}
        ).items()
    }
    correction = (row.prompt or {}).get("correction") or {}
    return {
        "job_id": row.id,
        "status": row.status,
        "annotation_id": row.annotation_id,
        "job_kind": row.job_kind,
        "correction_frame": row.correction_frame,
        "direction": row.direction,
        "from_frame": row.from_frame,
        "to_frame": row.to_frame,
        "fallback_reason": correction.get("fallback_reason"),
        "seed_mode": correction.get("seed_mode"),
        "protect_manual": bool(correction.get("protect_manual", False)),
        "results": hydrated,
        "grid_step": int(staged.get("grid_step", 1)),
        "output_geometry": staged.get("output_geometry", "bbox"),
        "job_revision": int(row.revision or 1),
        "expected_source_versions": expected_versions,
        "candidate_total": len(hydrated) + accepted + rejected,
        "candidate_pending": len(hydrated),
        "candidate_accepted": accepted,
        "candidate_rejected": rejected,
    }


async def list_reviewable_tracker_jobs(
    db: AsyncSession,
    *,
    task: Task,
    user: User,
) -> list[VideoTrackerJobOut]:
    """Return server-side candidates that the current workbench user can resume."""
    conditions = [
        VideoTrackerJob.task_id == task.id,
        VideoTrackerJob.status.in_(
            [
                VideoTrackerJobStatus.PENDING_REVIEW.value,
                VideoTrackerJobStatus.PARTIALLY_REVIEWED.value,
                VideoTrackerJobStatus.CANCELLED.value,
            ]
        ),
        VideoTrackerJob.staged_result.is_not(None),
    ]
    claimed_reviewer = bool(
        task.status == "review"
        and task.reviewer_id == user.id
        and task.reviewer_claimed_at is not None
    )
    if not claimed_reviewer and not await _is_privileged(db, task, user):
        conditions.append(VideoTrackerJob.created_by == user.id)
    rows = (
        (
            await db.execute(
                select(VideoTrackerJob)
                .where(*conditions)
                .order_by(VideoTrackerJob.created_at.desc(), VideoTrackerJob.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_job_out(row) for row in rows]


async def list_active_tracker_jobs(
    db: AsyncSession,
    *,
    task: Task,
    user: User,
) -> list[VideoTrackerJobOut]:
    """Return in-flight (queued/running) jobs so the workbench can reconnect their WS after reload."""
    conditions = [
        VideoTrackerJob.task_id == task.id,
        VideoTrackerJob.status.in_(
            [
                VideoTrackerJobStatus.QUEUED.value,
                VideoTrackerJobStatus.RUNNING.value,
            ]
        ),
    ]
    if not await _is_privileged(db, task, user):
        conditions.append(VideoTrackerJob.created_by == user.id)
    rows = (
        (
            await db.execute(
                select(VideoTrackerJob)
                .where(*conditions)
                .order_by(VideoTrackerJob.created_at.desc(), VideoTrackerJob.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_job_out(row) for row in rows]


def tracker_job_out(row: VideoTrackerJob) -> VideoTrackerJobOut:
    return _job_out(row)
