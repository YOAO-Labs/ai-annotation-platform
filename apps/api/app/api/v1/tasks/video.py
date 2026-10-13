import time
import uuid
from typing import Literal
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    get_current_user,
    require_scopes,
)
from app.db.models.user import User
from app.db.models.dataset import Dataset, DatasetItem, VideoFrameIndex
from app.db.models.task import Task
from app.schemas.annotation import AnnotationOut
from app.schemas.task import (
    PointCloudCameraOut,
    TaskPointCloudManifestResponse,
    TaskVideoFrameTimetableResponse,
    TaskVideoManifestResponse,
    VideoFrameTimetableEntry,
    VideoMetadata,
)
from app.schemas.video_frame_service import (
    VideoChunkOut,
    VideoChunksResponse,
    VideoFrameOut,
    VideoFramePrefetchRequest,
    VideoFramePrefetchResponse,
    VideoFrameRetryRequest,
    VideoManifestV2Response,
    VideoSegmentOut,
    VideoSegmentsResponse,
)
from app.schemas.video_track_quality import (
    VideoTrackQualityAcceptRequest,
    VideoTrackQualityRunOut,
    VideoTrackQualityRunRequest,
)
from app.schemas.video_tracker_job import (
    VideoMaskCorrectionRequest,
    VideoMaskKeyframeOperationRequest,
    VideoMaskKeyframeSaveRequest,
    VideoTrackerJobOut,
    VideoTrackerPropagateRequest,
)
from app.services.audit import AuditAction, AuditService
from app.services.task_dataset_link import get_linked_items
from app.services.project_access import is_privileged_for_project
from app.services.storage import storage_service
from app.services.video_frame_service import (
    build_context_from_task,
    get_chunk as get_video_chunk_asset,
    get_frame as get_video_frame_asset,
    list_chunks as list_video_chunks,
    manifest_v2 as build_video_manifest_v2,
    prefetch_frames as prefetch_video_frames,
    retry_frames as retry_video_frames,
)
from app.services.video_segment_service import (
    claim_segment,
    heartbeat_segment,
    list_segments as list_video_segments,
    release_segment,
    reopen_segment,
    submit_segment,
    unassign_segment,
)
from app.services.video_track_quality import (
    VideoTrackQualityError,
    accept_quality_run,
    create_quality_run,
    dispatch_quality_run,
    mark_boundary_runs_stale,
    refresh_staleness,
)
from app.services.video_tracking.jobs import (
    create_video_mask_correction_job,
    create_tracker_job,
    enqueue_tracker_job,
    list_active_tracker_jobs,
    list_reviewable_tracker_jobs,
    operate_video_mask_keyframe,
    save_video_mask_keyframe,
)
from app.services.video_collaboration import heartbeat_task_lock_for_legacy_video
from app.observability.metrics import observe_mask_ai_phase, record_mask_ai_operation


from app.api.v1.tasks._shared import (
    _assert_task_editable,
    _load_task_or_404,
    _assert_task_visible,
    _attach_dimensions,
    _capture_first_review_contributor_snapshot,
    _task_contributor_snapshot,
    require_task_annotation_write,
    require_task_review_write,
    VIDEO_MANIFEST_URL_EXPIRES_IN,
    logger,
)
from app.services.project_access import ProjectAccess

router = APIRouter()


@router.get(
    "/{task_id}/video/tracker-jobs/reviewable",
    response_model=list[VideoTrackerJobOut],
)
async def get_reviewable_video_tracker_jobs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """恢复当前任务仍待接受/丢弃的服务端候选。"""
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")
    return await list_reviewable_tracker_jobs(db, task=task, user=current_user)


@router.get(
    "/{task_id}/video/tracker-jobs/active",
    response_model=list[VideoTrackerJobOut],
)
async def get_active_video_tracker_jobs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """恢复当前任务仍在运行 (queued/running) 的追踪任务, 供刷新后重连 WebSocket。"""
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")
    return await list_active_tracker_jobs(db, task=task, user=current_user)


@router.get("/{task_id}/video/manifest", response_model=TaskVideoManifestResponse)
async def get_video_manifest(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")

    bucket = (
        storage_service.datasets_bucket
        if task.dataset_item_id
        else storage_service.bucket
    )
    try:
        _, _, thumb, _, video_metadata = await _attach_dimensions(db, task)
    except Exception as exc:
        logger.exception("Failed to load video metadata task_id=%s", task.id)
        raise HTTPException(
            status_code=503, detail="Video metadata unavailable"
        ) from exc

    metadata = VideoMetadata.model_validate(video_metadata or {})
    if not metadata.fps or not metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")

    video_path = metadata.playback_path or task.file_path
    # v0.10.17 · playback/* 走 media-cache 桶,原始源文件走 datasets 桶
    video_bucket = storage_service.bucket_for_cache_key(video_path, default=bucket)
    try:
        video_url = storage_service.generate_download_url(
            video_path,
            expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
            bucket=video_bucket,
        )
    except ClientError as exc:
        code = (exc.response.get("Error") or {}).get("Code")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(
                status_code=404, detail="Video file not available"
            ) from exc
        logger.exception(
            "Failed to generate video manifest URL task_id=%s bucket=%s key=%s",
            task.id,
            video_bucket,
            video_path,
        )
        raise HTTPException(
            status_code=503, detail="Video storage unavailable"
        ) from exc
    except BotoCoreError as exc:
        logger.exception(
            "Failed to generate video manifest URL task_id=%s bucket=%s key=%s",
            task.id,
            video_bucket,
            video_path,
        )
        raise HTTPException(
            status_code=503, detail="Video storage unavailable"
        ) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected video manifest URL error task_id=%s bucket=%s key=%s",
            task.id,
            video_bucket,
            video_path,
        )
        raise HTTPException(
            status_code=503, detail="Video storage unavailable"
        ) from exc

    poster_path = metadata.poster_frame_path or thumb
    poster_url: str | None = None
    poster_bucket: str | None = None
    if poster_path:
        # poster 可能是 thumbnails/* 或 videos/*/frames/* (media-cache) 或源 key (datasets)
        poster_bucket = storage_service.bucket_for_cache_key(
            poster_path, default=bucket
        )
        try:
            poster_url = storage_service.generate_download_url(
                poster_path,
                expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
                bucket=poster_bucket,
            )
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code")
            if code not in {"NoSuchKey", "404", "NotFound"}:
                logger.exception(
                    "Failed to generate video poster URL task_id=%s bucket=%s key=%s",
                    task.id,
                    poster_bucket,
                    poster_path,
                )
        except BotoCoreError:
            logger.exception(
                "Failed to generate video poster URL task_id=%s bucket=%s key=%s",
                task.id,
                poster_bucket,
                poster_path,
            )
        except Exception:
            logger.exception(
                "Unexpected video poster URL error task_id=%s bucket=%s key=%s",
                task.id,
                poster_bucket,
                poster_path,
            )

    return TaskVideoManifestResponse(
        task_id=task.id,
        dataset_item_id=task.dataset_item_id,
        video_url=video_url,
        poster_url=poster_url,
        metadata=metadata,
        expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
    )


@router.get(
    "/{task_id}/point-cloud/manifest",
    response_model=TaskPointCloudManifestResponse,
)
async def get_point_cloud_manifest(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.db.models.project import Project
    from app.services.sensor_calibration import (
        SensorCalibrationError,
        resolve_calibration_state,
    )

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)

    project = await db.get(Project, task.project_id)
    if project is None or project.data_type != "lidar":
        raise HTTPException(status_code=409, detail="Task is not a point-cloud task")

    links = await get_linked_items(db, task_id)

    def _presign(key: str) -> str:
        return storage_service.generate_download_url(
            key,
            expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
            bucket=storage_service.datasets_bucket,
        )

    # 一次性取出所有关联 DatasetItem，避免逐 link 往返 DB
    item_ids = [link.dataset_item_id for link in links]
    items_by_id: dict[uuid.UUID, DatasetItem] = {}
    if item_ids:
        rows = (
            (await db.execute(select(DatasetItem).where(DatasetItem.id.in_(item_ids))))
            .scalars()
            .all()
        )
        items_by_id = {item.id: item for item in rows}

    # 主点云 URL：优先 primary_lidar link，否则回退 task.file_path
    point_cloud_url: str | None = None
    primary_link = next((link for link in links if link.role == "primary_lidar"), None)
    if primary_link is not None:
        primary_item = items_by_id.get(primary_link.dataset_item_id)
        if primary_item is not None:
            point_cloud_url = _presign(primary_item.file_path)
    if point_cloud_url is None:
        if not task.file_path:
            raise HTTPException(
                status_code=404, detail="Point cloud file not available"
            )
        point_cloud_url = _presign(task.file_path)

    cameras: list[PointCloudCameraOut] = []
    for link in links:
        if not link.role.startswith("camera_"):
            continue
        item = items_by_id.get(link.dataset_item_id)
        if item is None:
            continue
        calibration = None
        calibration_revision = None
        calibration_digest = None
        try:
            state = await resolve_calibration_state(db, item)
            calibration = state.calibration
            calibration_revision = state.revision
            calibration_digest = state.digest
        except SensorCalibrationError:
            # 无标定相机仍可展示原图；投影与人工成员写入按显式空值降级。
            logger.warning(
                "task %s %s: stored calibration unavailable, returning null",
                task_id,
                link.role,
            )
        cameras.append(
            PointCloudCameraOut(
                name=link.role[len("camera_") :],
                role=link.role,
                dataset_item_id=item.id,
                image_url=_presign(item.file_path),
                width=item.width,
                height=item.height,
                calibration=calibration,
                calibration_revision=calibration_revision,
                calibration_digest=calibration_digest,
            )
        )

    cameras.sort(key=lambda c: c.name)

    # v0.13.11 · 取主点云所在 Dataset 的 axis_convention,前端用它把 PCD positions 与各相机
    # extrinsic 旋转归一化到 ISO 8855。无 primary_lidar item / 无 metadata key → None,
    # 前端按 "iso_8855" (= identity) 处理,保持向后兼容。
    axis_convention = None
    if primary_link is not None:
        primary_item = items_by_id.get(primary_link.dataset_item_id)
        if primary_item is not None:
            dataset = await db.get(Dataset, primary_item.dataset_id)
            if dataset is not None:
                axis_convention = (dataset.metadata_ or {}).get("axis_convention")

    # v0.14.0 · scene 字段透出:仅当 primary_lidar item 有 scene_id 时挂上,
    # 前端用做跨帧导航的合法 backing(本期不消费 UX,仅显示在调试面板)。
    scene_id_out: uuid.UUID | None = None
    scene_name_out: str | None = None
    frame_index_out: int | None = None
    scene_total_frames_out: int | None = None
    ego_pose_out = None
    if primary_link is not None:
        primary_item = items_by_id.get(primary_link.dataset_item_id)
        if primary_item is not None and primary_item.scene_id is not None:
            from app.db.models.dataset import Scene
            from app.services import scene_pose as scene_pose_svc

            scene = await db.get(Scene, primary_item.scene_id)
            if scene is not None:
                scene_id_out = scene.id
                scene_name_out = scene.name
                frame_index_out = primary_item.frame_index
                total_row = await db.execute(
                    select(func.count(func.distinct(DatasetItem.frame_index)))
                    .where(DatasetItem.scene_id == scene.id)
                    .where(DatasetItem.frame_index.is_not(None))
                )
                scene_total_frames_out = total_row.scalar() or 0
                # v0.15.0 · 本帧 ego pose 透出;无位姿行(历史/非 nuScenes) → None
                if primary_item.frame_index is not None:
                    ego_pose_out = await scene_pose_svc.get_frame_pose(
                        db,
                        scene_id=scene.id,
                        frame_index=primary_item.frame_index,
                    )

    return TaskPointCloudManifestResponse(
        task_id=task.id,
        point_cloud_url=point_cloud_url,
        cameras=cameras,
        expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
        axis_convention=axis_convention,
        scene_id=scene_id_out,
        scene_name=scene_name_out,
        frame_index=frame_index_out,
        scene_total_frames=scene_total_frames_out,
        ego_pose=ego_pose_out,
    )


@router.get(
    "/{task_id}/video/frame-timetable",
    response_model=TaskVideoFrameTimetableResponse,
)
async def get_video_frame_timetable(
    task_id: uuid.UUID,
    response: Response,
    from_frame: int | None = Query(default=None, ge=0, alias="from"),
    to_frame: int | None = Query(default=None, ge=0, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")
    response.headers["Cache-Control"] = "private, max-age=3600"

    _, _, _, _, video_metadata = await _attach_dimensions(db, task)
    metadata = VideoMetadata.model_validate(video_metadata or {})
    if not metadata.fps or not metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")

    if not task.dataset_item_id:
        return TaskVideoFrameTimetableResponse(
            task_id=task.id,
            fps=metadata.fps,
            frame_count=metadata.frame_count,
            source="estimated",
            frames=[],
        )

    has_timetable = (
        await db.execute(
            select(func.count(VideoFrameIndex.id)).where(
                VideoFrameIndex.dataset_item_id == task.dataset_item_id
            )
        )
    ).scalar_one() > 0
    stmt = select(VideoFrameIndex).where(
        VideoFrameIndex.dataset_item_id == task.dataset_item_id
    )
    if from_frame is not None:
        stmt = stmt.where(VideoFrameIndex.frame_index >= from_frame)
    if to_frame is not None:
        stmt = stmt.where(VideoFrameIndex.frame_index <= to_frame)
    rows = (
        (await db.execute(stmt.order_by(VideoFrameIndex.frame_index.asc())))
        .scalars()
        .all()
    )

    body = TaskVideoFrameTimetableResponse(
        task_id=task.id,
        fps=metadata.fps,
        frame_count=metadata.frame_count,
        source="ffprobe" if has_timetable else "estimated",
        frames=[
            VideoFrameTimetableEntry(
                frame_index=row.frame_index,
                pts_ms=row.pts_ms,
                is_keyframe=row.is_keyframe,
                pict_type=row.pict_type,
                byte_offset=row.byte_offset,
            )
            for row in rows
        ],
    )
    response.headers["ETag"] = (
        f'"video-timetable:{task.dataset_item_id}:{metadata.frame_count}:'
        f'{len(body.frames)}:{from_frame or 0}:{to_frame or ""}"'
    )
    return body


@router.get("/{task_id}/video/manifest-v2", response_model=VideoManifestV2Response)
async def get_video_manifest_v2(
    task_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    return await build_video_manifest_v2(db, ctx, str(request.base_url))


@router.get("/{task_id}/video/segments", response_model=VideoSegmentsResponse)
async def get_video_segments(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    return await list_video_segments(db, ctx)


@router.post(
    "/{task_id}/video/segments/{segment_id}:claim",
    response_model=VideoSegmentOut,
)
async def claim_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await claim_segment(db, ctx, segment_id, current_user, privileged=privileged)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_CLAIM,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.item.id)},
    )
    await db.commit()
    return body


@router.post(
    "/{task_id}/video/segments/{segment_id}:heartbeat",
    response_model=VideoSegmentOut,
)
async def heartbeat_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await heartbeat_segment(
        db, ctx, segment_id, current_user, privileged=privileged
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_HEARTBEAT,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.item.id)},
    )
    await db.commit()
    return body


@router.post(
    "/{task_id}/video/segments/{segment_id}:release",
    response_model=VideoSegmentOut,
)
async def release_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await release_segment(
        db, ctx, segment_id, current_user, privileged=privileged
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_RELEASE,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.item.id)},
    )
    await db.commit()
    return body


async def _segment_privileged(db: AsyncSession, task: Task, user: User) -> bool:
    from app.db.models.project import Project

    project = await db.get(Project, task.project_id)
    return bool(
        project
        and (is_privileged_for_project(user, project) or task.reviewer_id == user.id)
    )


def _raise_quality_error(exc: VideoTrackQualityError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post(
    "/{task_id}/video/segments/{segment_id}:submit",
    response_model=VideoSegmentOut,
)
async def submit_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    was_in_review = task.status == "review"
    body = await submit_segment(
        db,
        ctx,
        segment_id,
        current_user,
        privileged=await _segment_privileged(db, task, current_user),
    )
    from app.db.models.dataset import VideoSegment

    neighbors = list(
        (
            await db.execute(
                select(VideoSegment).where(
                    VideoSegment.dataset_item_id == ctx.item.id,
                    VideoSegment.segment_index.in_(
                        [body.segment_index - 1, body.segment_index + 1]
                    ),
                    VideoSegment.status == "completed",
                )
            )
        )
        .scalars()
        .all()
    )
    dispatches: list[tuple[uuid.UUID, uuid.UUID]] = []
    for neighbor in neighbors:
        left_id, right_id = (
            (neighbor.id, segment_id)
            if neighbor.segment_index < body.segment_index
            else (segment_id, neighbor.id)
        )
        try:
            run, job, created = await create_quality_run(
                db,
                task=task,
                left_segment_id=left_id,
                right_segment_id=right_id,
                actor_id=current_user.id,
            )
        except VideoTrackQualityError as exc:
            _raise_quality_error(exc)
        if created and job is not None:
            dispatches.append((run.id, job.id))
    if task.status == "review":
        if not was_in_review:
            contributor_ids = await _task_contributor_snapshot(
                # Segments belong to dataset items shared by projects.
                # Only this task's authors and actual submitter establish
                # project contribution; global segment assignees do not.
                db,
                task,
                extra_user_ids=(current_user.id,),
            )
            _capture_first_review_contributor_snapshot(task, contributor_ids)
            await AuditService.log(
                db,
                actor=current_user,
                action=AuditAction.TASK_SUBMIT,
                target_type="task",
                target_id=str(task.id),
                request=request,
                status_code=200,
                detail={
                    "project_id": str(task.project_id),
                    "assignee_id": str(task.assignee_id) if task.assignee_id else None,
                    "contributor_ids": contributor_ids,
                    "review_round_id": str(task.review_round_id)
                    if task.review_round_id
                    else None,
                    "result": "submitted",
                    "submission_source": "video_segment",
                    "segment_id": str(segment_id),
                },
            )
        from app.services.batch import BatchService

        batch_service = BatchService(db)
        await batch_service.check_auto_transitions(task.batch_id)
        if task.batch_id:
            await batch_service.recalculate_counters(task.batch_id)
    await db.commit()
    for run_id, job_id in dispatches:
        try:
            await dispatch_quality_run(db, run_id=run_id, async_job_id=job_id)
        except VideoTrackQualityError as exc:
            _raise_quality_error(exc)
    return body


@router.post(
    "/{task_id}/video/segments/{segment_id}:reopen",
    response_model=VideoSegmentOut,
)
async def reopen_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    ctx = await build_context_from_task(db, task)
    body = await reopen_segment(db, ctx, segment_id)
    await mark_boundary_runs_stale(db, segment_id)
    await db.commit()
    return body


@router.post(
    "/{task_id}/video/segments/{segment_id}:unassign",
    response_model=VideoSegmentOut,
)
async def unassign_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    ctx = await build_context_from_task(db, task)
    body = await unassign_segment(db, ctx, segment_id)
    await mark_boundary_runs_stale(db, segment_id)
    await db.commit()
    return body


@router.get(
    "/{task_id}/video/track-quality",
    response_model=list[VideoTrackQualityRunOut],
)
async def list_video_track_quality(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.db.models.video_track_quality import VideoTrackQualityRun

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    runs = list(
        (
            await db.execute(
                select(VideoTrackQualityRun)
                .where(VideoTrackQualityRun.task_id == task.id)
                .order_by(VideoTrackQualityRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        await refresh_staleness(db, run)
    await db.commit()
    return runs


@router.post(
    "/{task_id}/video/track-quality/run",
    response_model=VideoTrackQualityRunOut,
)
async def run_video_track_quality_now(
    task_id: uuid.UUID,
    body: VideoTrackQualityRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    try:
        run, job, created = await create_quality_run(
            db,
            task=task,
            left_segment_id=body.left_segment_id,
            right_segment_id=body.right_segment_id,
            actor_id=current_user.id,
        )
    except VideoTrackQualityError as exc:
        _raise_quality_error(exc)
    await db.commit()
    if created and job is not None:
        try:
            await dispatch_quality_run(db, run_id=run.id, async_job_id=job.id)
        except VideoTrackQualityError as exc:
            _raise_quality_error(exc)
    return run


@router.get(
    "/{task_id}/video/track-quality/{run_id}",
    response_model=VideoTrackQualityRunOut,
)
async def get_video_track_quality(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.db.models.video_track_quality import (
        VideoTrackQualityIssue,
        VideoTrackQualityRun,
    )

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    run = await db.get(VideoTrackQualityRun, run_id)
    if run is None or run.task_id != task.id:
        raise HTTPException(status_code=404, detail="Track quality run not found")
    await refresh_staleness(db, run)
    issues = list(
        (
            await db.execute(
                select(VideoTrackQualityIssue)
                .where(VideoTrackQualityIssue.run_id == run.id)
                .order_by(
                    VideoTrackQualityIssue.frame_start,
                    VideoTrackQualityIssue.id,
                )
            )
        )
        .scalars()
        .all()
    )
    from app.services.video_track_quality import metrics_with_current_chapters

    metrics = await metrics_with_current_chapters(db, task=task, run=run, issues=issues)
    await db.commit()
    return VideoTrackQualityRunOut.model_validate(run).model_copy(
        update={"issues": issues, "metrics": metrics}
    )


@router.post(
    "/{task_id}/video/track-quality/{run_id}:accept",
    response_model=VideoTrackQualityRunOut,
)
async def accept_video_track_quality(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    body: VideoTrackQualityAcceptRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    from app.db.models.video_track_quality import VideoTrackQualityRun

    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    run = (
        await db.execute(
            select(VideoTrackQualityRun)
            .where(VideoTrackQualityRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None or run.task_id != task.id:
        raise HTTPException(status_code=404, detail="Track quality run not found")
    try:
        await accept_quality_run(
            db,
            run=run,
            actor_id=current_user.id,
            input_digest=body.input_digest,
            decisions=[pair.model_dump(mode="json") for pair in body.pairs],
        )
    except VideoTrackQualityError as exc:
        _raise_quality_error(exc)
    await db.commit()
    return run


@router.get("/{task_id}/video/chunks", response_model=VideoChunksResponse)
async def get_video_chunks(
    task_id: uuid.UUID,
    from_frame: int | None = Query(default=None, ge=0),
    to_frame: int | None = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    return await list_video_chunks(db, ctx, from_frame, to_frame)


@router.get("/{task_id}/video/chunks/{chunk_id}", response_model=VideoChunkOut)
async def get_video_chunk(
    task_id: uuid.UUID,
    chunk_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    body = await get_video_chunk_asset(db, ctx, chunk_id)
    if body.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = str(body.retry_after or 3)
    return body


@router.get("/{task_id}/video/frames/{frame_index}", response_model=VideoFrameOut)
async def get_video_frame(
    task_id: uuid.UUID,
    frame_index: int,
    response: Response,
    format: Literal["webp", "jpeg"] = Query(default="webp"),
    w: int = Query(default=512, ge=1, le=4096),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    body = await get_video_frame_asset(db, ctx, frame_index, w, format)
    if body.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = str(body.retry_after or 3)
    response.headers["Cache-Control"] = (
        "private, max-age=3600" if body.status == "ready" else "no-store"
    )
    return body


@router.post(
    "/{task_id}/video/frames:prefetch",
    response_model=VideoFramePrefetchResponse,
)
async def prefetch_video_frame_assets(
    task_id: uuid.UUID,
    payload: VideoFramePrefetchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    return await prefetch_video_frames(
        db, ctx, payload.frame_indices, payload.width, payload.format
    )


@router.post(
    "/{task_id}/video/frames:retry",
    response_model=VideoFramePrefetchResponse,
)
async def retry_video_frame_assets(
    task_id: uuid.UUID,
    payload: VideoFrameRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user)
    ctx = await build_context_from_task(db, task)
    return await retry_video_frames(
        db,
        ctx,
        payload.frame_indices,
        payload.width,
        payload.format,
        force=payload.force,
    )


@router.put(
    "/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}",
    response_model=AnnotationOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def save_video_mask_correction_keyframe(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    frame_index: int,
    payload: VideoMaskKeyframeSaveRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    raw_if_match = request.headers.get("If-Match", "").strip()
    if not raw_if_match:
        raise HTTPException(status_code=428, detail={"reason": "if_match_required"})
    try:
        expected_version = int(raw_if_match.removeprefix('W/"').removesuffix('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid If-Match format") from exc
    ctx = await build_context_from_task(db, task)
    annotation, audit_detail = await save_video_mask_keyframe(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        frame_index=frame_index,
        payload=payload,
        expected_version=expected_version,
        user=current_user,
    )
    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_MASK_KEYFRAME_CORRECT,
        target_type="annotation",
        target_id=annotation.id,
        request=request,
        status_code=200,
        detail=audit_detail,
    )
    await db.commit()
    await db.refresh(annotation)
    response.headers["ETag"] = f'W/"{annotation.version}"'
    return AnnotationOut.model_validate(annotation)


@router.patch(
    "/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}",
    response_model=AnnotationOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def operate_video_mask_correction_keyframe(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    frame_index: int,
    payload: VideoMaskKeyframeOperationRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = (
        await db.execute(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    raw_if_match = request.headers.get("If-Match", "").strip()
    if not raw_if_match:
        raise HTTPException(status_code=428, detail={"reason": "if_match_required"})
    try:
        expected_version = int(raw_if_match.removeprefix('W/"').removesuffix('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid If-Match format") from exc
    ctx = await build_context_from_task(db, task)
    annotation, audit_detail = await operate_video_mask_keyframe(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        frame_index=frame_index,
        payload=payload,
        expected_version=expected_version,
        user=current_user,
    )
    await heartbeat_task_lock_for_legacy_video(db, task, current_user.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_MASK_KEYFRAME_OPERATE,
        target_type="annotation",
        target_id=annotation.id,
        request=request,
        status_code=200,
        detail=audit_detail,
    )
    await db.commit()
    await db.refresh(annotation)
    response.headers["ETag"] = f'W/"{annotation.version}"'
    if audit_detail["resolved_keyframe_frame"] is not None:
        response.headers["X-Resolved-Keyframe-Frame"] = str(
            audit_detail["resolved_keyframe_frame"]
        )
    response.headers["X-Restored-Held"] = (
        "true" if audit_detail["restored_held"] else "false"
    )
    return AnnotationOut.model_validate(annotation)


@router.post(
    "/{task_id}/video/tracks/{annotation_id}/correction-jobs",
    response_model=VideoTrackerJobOut,
    status_code=202,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def create_video_mask_correction(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    payload: VideoMaskCorrectionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    ctx = await build_context_from_task(db, task)
    commit_started = time.monotonic()
    try:
        body = await create_video_mask_correction_job(
            db,
            task=task,
            ctx=ctx,
            annotation_id=annotation_id,
            payload=payload,
            user=current_user,
        )
    except HTTPException as exc:
        reason = exc.detail.get("reason") if isinstance(exc.detail, dict) else None
        outcome = "conflict" if exc.status_code == 409 else "error"
        observe_mask_ai_phase(
            operation="correction",
            phase="commit",
            outcome=outcome,
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation="correction",
            prompt_family="correction_frame",
            output_geometry="mask",
            fallback_reason=(
                "mask_prompt_unsupported"
                if reason == "mask_prompt_unsupported"
                else None
            ),
            outcome=outcome,
        )
        raise
    correction = (body.prompt or {}).get("correction") or {}
    routing = correction.get("routing") or {}
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_CORRECTION_JOB_CREATE,
        target_type="video_tracker_job",
        target_id=body.id,
        request=request,
        status_code=202,
        detail={
            "task_id": str(task.id),
            "annotation_id": str(annotation_id),
            "track_id": body.track_id_snapshot,
            "correction_frame": body.correction_frame,
            "from_frame": body.from_frame,
            "to_frame": body.to_frame,
            "direction": body.direction,
            "model_key": body.model_key,
            "model_id": routing.get("model_id"),
            "requested_backend_id": routing.get("requested_backend_id"),
            "backend_pool_id": routing.get("backend_pool_id"),
            "source_version": correction.get("source_version"),
            "corrected_digest": correction.get("corrected_digest"),
            "segment": correction.get("segment"),
            "seed_mode": correction.get("seed_mode"),
            "fallback_reason": correction.get("fallback_reason"),
        },
    )
    try:
        await db.commit()
        enqueued = await enqueue_tracker_job(db, body.id, fail_closed=True)
    except HTTPException as exc:
        outcome = "conflict" if exc.status_code == 409 else "error"
        observe_mask_ai_phase(
            operation="correction",
            phase="commit",
            outcome=outcome,
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation="correction",
            prompt_family="correction_frame",
            output_geometry="mask",
            fallback_reason=correction.get("fallback_reason"),
            outcome=outcome,
        )
        raise
    except Exception:
        observe_mask_ai_phase(
            operation="correction",
            phase="commit",
            outcome="error",
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation="correction",
            prompt_family="correction_frame",
            output_geometry="mask",
            fallback_reason=correction.get("fallback_reason"),
            outcome="error",
        )
        raise
    observe_mask_ai_phase(
        operation="correction",
        phase="commit",
        outcome="success",
        duration_seconds=time.monotonic() - commit_started,
    )
    record_mask_ai_operation(
        operation="correction",
        prompt_family="correction_frame",
        output_geometry="mask",
        fallback_reason=correction.get("fallback_reason"),
        outcome="success",
    )
    return enqueued


@router.post(
    "/{task_id}/video/tracks/{annotation_id}:propagate",
    response_model=VideoTrackerJobOut,
    status_code=202,
)
async def propagate_video_track(
    task_id: uuid.UUID,
    annotation_id: uuid.UUID,
    payload: VideoTrackerPropagateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    ctx = await build_context_from_task(db, task)
    body = await create_tracker_job(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        payload=payload,
        user=current_user,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_TRACKER_JOB_CREATE,
        target_type="video_tracker_job",
        target_id=body.id,
        request=request,
        status_code=202,
        detail={
            "task_id": str(task.id),
            "annotation_id": str(annotation_id),
            "dataset_item_id": str(ctx.item.id),
            "segment_id": str(body.segment_id) if body.segment_id else None,
            "from_frame": body.from_frame,
            "to_frame": body.to_frame,
            "model_key": body.model_key,
            "direction": body.direction,
        },
    )
    await db.commit()
    return body


@router.post(
    "/{task_id}/video:track",
    response_model=VideoTrackerJobOut,
    status_code=202,
)
async def track_video(
    task_id: uuid.UUID,
    payload: VideoTrackerPropagateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    """v0.22.1 · B · 任务级追踪 (画布级入口): source_annotation_id 可选——给出即延展该轨迹,
    缺省则为无源检测 (文本/种子), 新建轨迹类别由 target_class_name 指定。旧的
    /tracks/{annotation_id}:propagate 仍保留 (延展快捷路径)。"""
    task = await _load_task_or_404(db, task_id)
    await _assert_task_visible(db, task, current_user, access=access)
    _assert_task_editable(task, current_user, access=access)
    ctx = await build_context_from_task(db, task)
    body = await create_tracker_job(
        db,
        task=task,
        ctx=ctx,
        annotation_id=payload.source_annotation_id,
        payload=payload,
        user=current_user,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_TRACKER_JOB_CREATE,
        target_type="video_tracker_job",
        target_id=body.id,
        request=request,
        status_code=202,
        detail={
            "task_id": str(task.id),
            "annotation_id": str(payload.source_annotation_id)
            if payload.source_annotation_id
            else None,
            # v0.22.2 · M · 多选批量: 记录批量延展的源轨迹 ids (多源时 annotation_id 为 None)。
            "source_annotation_ids": [str(a) for a in payload.source_annotation_ids]
            if payload.source_annotation_ids
            else None,
            "dataset_item_id": str(ctx.item.id),
            "segment_id": str(body.segment_id) if body.segment_id else None,
            "from_frame": body.from_frame,
            "to_frame": body.to_frame,
            "model_key": body.model_key,
            "direction": body.direction,
        },
    )
    await db.commit()
    return body
