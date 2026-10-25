from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.project_access import get_workspace_project
from app.db.models.dataset import DatasetItem, VideoSegment
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import VideoTrackerJobStatus
from aap_workspace.configuration_values import VideoCollaborationConfig
from app.schemas.task import VideoMetadata
from app.services.assets_media_summary import AssetsTaskMediaSummaryReader
from app.services.video_context import derive_step
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import (
    annotation_row,
    task_row,
    video_segment_row,
    video_tracker_job_row,
)


VIDEO_GEOMETRY_TYPES = frozenset(
    {
        "video_bbox",
        "video_polygon",
        "video_polyline",
        "video_keypoint",
        "video_rotated_bbox",
        "video_mask",
        "video_track_bbox",
        "video_track_polygon",
        "video_track_polyline",
        "video_track_mask",
    }
)
VIDEO_TOOL_UNITS = frozenset({"bbox", "polyline", "keypoint", "region", "rotated_bbox"})


def collaboration_config(project: Project | None) -> VideoCollaborationConfig:
    return VideoCollaborationConfig.model_validate(
        project.video_collaboration if project is not None else {}
    )


def collaboration_enabled(project: Project | None) -> bool:
    return collaboration_config(project).enabled


def segment_work_bounds(
    *,
    start_frame: int,
    end_frame: int,
    segment_index: int,
    segment_count: int,
    frame_count: int,
    overlap_frames: int,
) -> tuple[int, int]:
    if overlap_frames <= 0:
        return start_frame, end_frame
    left = overlap_frames // 2
    right = overlap_frames - left
    work_start = start_frame if segment_index == 0 else start_frame - left
    work_end = end_frame if segment_index == segment_count - 1 else end_frame + right
    return max(0, work_start), min(max(0, frame_count - 1), work_end)


def geometry_frame_bounds(geometry: dict) -> tuple[int, int] | None:
    geometry_type = str(geometry.get("type") or "")
    if geometry_type not in VIDEO_GEOMETRY_TYPES:
        return None
    if "frame_index" in geometry:
        frame_index = int(geometry["frame_index"])
        return frame_index, frame_index

    frames = [int(row["frame_index"]) for row in geometry.get("keyframes") or []]
    for range_ in geometry.get("outside") or []:
        frames.extend((int(range_["from"]), int(range_["to"])))
    return (min(frames), max(frames)) if frames else None


async def segment_work_bounds_for_task(
    db: AsyncSession,
    *,
    task: Task,
    segment: VideoSegment,
    project: Project | None = None,
    video_metadata: VideoMetadata | None = None,
    segment_count: int | None = None,
) -> tuple[int, int]:
    project = project or await get_workspace_project(db, project_id=task.project_id)
    config = collaboration_config(project)
    if not config.enabled:
        return segment.start_frame, segment.end_frame

    if segment_count is None:
        segment_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(video_segment_row)
                    .where(video_segment_row.dataset_item_id == task.dataset_item_id)
                )
            ).scalar_one()
        )
    if video_metadata is None:
        video_metadata = (
            await AssetsTaskMediaSummaryReader(db).current_video_metadata(
                task.dataset_item_id
            )
            if task.dataset_item_id is not None
            else None
        )
        if task.dataset_item_id is not None and video_metadata is None:
            raise HTTPException(
                status_code=503,
                detail={"reason": "video_metadata_not_ready"},
                headers={"Retry-After": "3"},
            )
        frame_count = (video_metadata or {}).get("frame_count")
    else:
        frame_count = video_metadata.frame_count
    if frame_count is None or frame_count <= 0:
        raise HTTPException(
            status_code=503,
            detail={"reason": "video_metadata_not_ready"},
            headers={"Retry-After": "3"},
        )
    return segment_work_bounds(
        start_frame=segment.start_frame,
        end_frame=segment.end_frame,
        segment_index=segment.segment_index,
        segment_count=max(1, segment_count),
        frame_count=int(frame_count),
        overlap_frames=config.overlap_frames,
    )


async def validate_project_collaboration_update(
    db: AsyncSession,
    project: Project | None,
    requested: VideoCollaborationConfig,
    *,
    data_type: str,
    video_sampling_changed: bool = False,
    requested_video_sampling: dict | None = None,
    requested_tool_bindings: dict | None = None,
) -> None:
    if requested.enabled and data_type != "video":
        raise HTTPException(
            status_code=422,
            detail="video_collaboration 只能用于 video 项目",
        )
    if requested.overlap_frames >= max(1, settings.video_segment_size_frames):
        raise HTTPException(
            status_code=422,
            detail="overlap_frames 必须小于 VIDEO_SEGMENT_SIZE_FRAMES",
        )
    tool_bindings = requested_tool_bindings
    if tool_bindings is None and project is not None:
        tool_bindings = project.tool_bindings or {}
    invalid_units = sorted(set(tool_bindings or {}) - VIDEO_TOOL_UNITS)
    if requested.enabled and invalid_units:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "video_collaboration_invalid_tools",
                "tool_units": invalid_units,
            },
        )
    if project is None:
        return

    current = collaboration_config(project)
    collaboration_changed = requested != current
    if not collaboration_changed and not (video_sampling_changed and current.enabled):
        return

    annotation_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(annotation_row)
                .where(
                    annotation_row.project_id == project.id,
                    annotation_row.is_active.is_(True),
                    annotation_row.was_cancelled.is_(False),
                )
            )
        ).scalar_one()
    )
    if annotation_count:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "video_collaboration_config_frozen",
                "annotation_count": annotation_count,
            },
        )

    active_tracker_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(video_tracker_job_row)
                .join(task_row, task_row.id == video_tracker_job_row.task_id)
                .where(
                    task_row.project_id == project.id,
                    video_tracker_job_row.status.in_(
                        [
                            VideoTrackerJobStatus.QUEUED.value,
                            VideoTrackerJobStatus.RUNNING.value,
                        ]
                    ),
                )
            )
        ).scalar_one()
    )
    if active_tracker_count:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "video_collaboration_tracker_active",
                "active_tracker_count": active_tracker_count,
            },
        )

    if requested.enabled:
        sampling = requested_video_sampling
        if sampling is None:
            sampling = project.video_sampling or {}
        item_ids = list(
            (
                await db.execute(
                    select(DatasetItem.id)
                    .join(task_row, task_row.dataset_item_id == DatasetItem.id)
                    .where(task_row.project_id == project.id)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        summaries = await AssetsTaskMediaSummaryReader(db).current_video_metadata_many(
            tuple(item_ids)
        )
        if set(item_ids) - summaries.keys():
            raise HTTPException(
                status_code=503,
                detail={"reason": "video_media_summary_unavailable"},
            )
        for item_id in item_ids:
            metadata = summaries.get(item_id)
            if metadata is None:
                raise HTTPException(
                    status_code=503,
                    detail={"reason": "video_metadata_not_ready"},
                    headers={"Retry-After": "3"},
                )
            metadata = metadata or {}
            frame_count = max(0, int(metadata.get("frame_count") or 0))
            step = derive_step(metadata.get("fps"), sampling)
            for boundary in range(
                settings.video_segment_size_frames,
                frame_count,
                settings.video_segment_size_frames,
            ):
                left = requested.overlap_frames // 2
                right = requested.overlap_frames - left
                start = max(0, boundary - left)
                end = min(frame_count - 1, boundary + right - 1)
                first_sample = ((start + step - 1) // step) * step
                if first_sample > end:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "reason": "video_collaboration_sampling_empty",
                            "dataset_item_id": str(item_id),
                            "boundary_frame": boundary,
                        },
                    )


async def assert_video_annotation_write_scope(
    db: AsyncSession,
    *,
    task: Task,
    user: User | IdentityActorFacts,
    segment_id: uuid.UUID | None,
    geometry: dict | None,
) -> VideoSegment | None:
    project = await get_workspace_project(db, project_id=task.project_id)
    enabled = collaboration_enabled(project)
    if not enabled:
        if segment_id is not None:
            raise HTTPException(
                status_code=422,
                detail={"reason": "video_collaboration_disabled"},
            )
        return None

    if task.file_type != "video" or task.dataset_item_id is None:
        raise HTTPException(status_code=422, detail="协同标注只支持视频 Task")
    if segment_id is None:
        raise HTTPException(
            status_code=422,
            detail={"reason": "video_segment_required"},
        )
    segment = (
        await db.execute(
            select(video_segment_row).where(
                video_segment_row.id == segment_id,
                video_segment_row.dataset_item_id == task.dataset_item_id,
            )
        )
    ).scalar_one_or_none()
    if segment is None:
        raise HTTPException(status_code=404, detail="Video segment not found")
    if segment.status == "completed":
        raise HTTPException(
            status_code=409,
            detail={"reason": "video_segment_completed"},
        )

    now = datetime.now(timezone.utc)
    if (
        segment.assignee_id != user.id
        or segment.locked_by != user.id
        or segment.lock_expires_at is None
        or segment.lock_expires_at <= now
    ):
        raise HTTPException(
            status_code=409,
            detail={"reason": "video_segment_lease_required"},
        )

    if geometry is not None:
        bounds = geometry_frame_bounds(geometry)
        if bounds is None:
            raise HTTPException(
                status_code=422,
                detail={"reason": "video_geometry_required"},
            )
        work_start, work_end = await segment_work_bounds_for_task(
            db,
            task=task,
            segment=segment,
            project=project,
        )
        if bounds[0] < work_start or bounds[1] > work_end:
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": "video_geometry_outside_segment",
                    "frame_start": bounds[0],
                    "frame_end": bounds[1],
                    "work_start_frame": work_start,
                    "work_end_frame": work_end,
                },
            )
    return segment


async def heartbeat_task_lock_for_legacy_video(
    db: AsyncSession, task: Task, user_id: uuid.UUID
) -> None:
    project = await get_workspace_project(db, project_id=task.project_id)
    if collaboration_enabled(project):
        return
    from app.services.task_lock import TaskLockService

    await TaskLockService(db).heartbeat(task.id, user_id)


async def assert_task_lock_for_legacy_video(
    db: AsyncSession, task: Task, user_id: uuid.UUID
) -> None:
    project = await get_workspace_project(db, project_id=task.project_id)
    if collaboration_enabled(project):
        return
    from app.services.task_lock import TaskLockService

    await TaskLockService(db).assert_write_allowed(task.id, user_id)
