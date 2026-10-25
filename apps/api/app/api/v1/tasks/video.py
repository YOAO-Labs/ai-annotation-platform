import time
import uuid
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aap_assets_media.contract import MediaFailure, TimetableRequest

from app.services.quality_job_dispatch import QualityJobDispatcher
from app.deps import (
    get_quality_job_dispatcher,
    get_video_tracker_job_dispatcher,
    get_db,
    get_current_user,
    require_scopes,
)
from app.db.models.user import User
from app.db.models.task import Task
from app.db.models.video_track_quality import (
    VideoTrackQualityIssue,
    VideoTrackQualityRun,
)
from app.schemas.annotation import AnnotationOut
from app.schemas.task import (
    TaskPointCloudManifestResponse,
    TaskVideoFrameTimetableResponse,
    TaskVideoManifestResponse,
    VideoFrameTimetableEntry,
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
from app.services.project_access import (
    get_workspace_project,
    is_privileged_for_project,
)
from app.services.storage import storage_service
from app.application.point_cloud_manifest import (
    PointCloudManifestFileUnavailable,
    build_point_cloud_manifest,
)
from app.services.assets_point_cloud_manifest import (
    SqlAlchemyPointCloudManifestAssetsReader,
)
from app.services.labeling_point_cloud_manifest import (
    SqlAlchemyTaskDatasetItemLinksReader,
)
from app.services.video_frame_service import (
    AssetsVideoContextReader,
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
from app.services.labeling_spatial_quality import (
    quality_row,
    quality_rows,
    spatial_quality_data,
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
from app.application.task_workflow_errors import TaskWorkflowError
from app.services.annotation_evidence import (
    task_contributor_snapshot,
)
from app.services.assets_media import media_gateway, media_http_error
from app.services.video_tracking.dispatch import VideoTrackerJobDispatcher
from app.observability.metrics import observe_mask_ai_phase, record_mask_ai_operation


from app.services import task_access
from app.api.v1.tasks._shared import (
    _load_task_or_404,
    require_task_annotation_write,
    require_task_review_write,
    VIDEO_MANIFEST_URL_EXPIRES_IN,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import (
    lock_task_for_core_read,
    video_segment_row,
)

router = APIRouter()


@router.get(
    "/{task_id}/video/tracker-jobs/reviewable",
    response_model=list[VideoTrackerJobOut],
)
async def get_reviewable_video_tracker_jobs(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """恢复当前任务仍待接受/丢弃的服务端候选。"""
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """恢复当前任务仍在运行 (queued/running) 的追踪任务, 供刷新后重连 WebSocket。"""
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")
    return await list_active_tracker_jobs(db, task=task, user=current_user)


@router.get("/{task_id}/video/manifest", response_model=TaskVideoManifestResponse)
async def get_video_manifest(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")

    if not task.dataset_item_id:
        raise HTTPException(status_code=503, detail="Video metadata not ready")
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    metadata = ctx.metadata
    if not metadata.fps or not metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")
    reference = ctx.source_reference
    if reference is None:
        raise HTTPException(status_code=503, detail="Video source unavailable")

    return TaskVideoManifestResponse(
        task_id=task.id,
        dataset_item_id=task.dataset_item_id,
        video_url=reference.video_url,
        poster_url=reference.poster_url,
        metadata=metadata,
        expires_in=reference.expires_in,
    )


@router.get(
    "/{task_id}/point-cloud/manifest",
    response_model=TaskPointCloudManifestResponse,
)
async def get_point_cloud_manifest(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)

    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    if project is None or project.data_type != "lidar":
        raise HTTPException(status_code=409, detail="Task is not a point-cloud task")

    def _presign(key: str) -> str:
        return storage_service.generate_download_url(
            key,
            expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
            bucket=storage_service.datasets_bucket,
        )

    try:
        return await build_point_cloud_manifest(
            task.id,
            task.file_path,
            link_reader=SqlAlchemyTaskDatasetItemLinksReader(db),
            assets_reader=SqlAlchemyPointCloudManifestAssetsReader(db),
            presign=_presign,
            expires_in=VIDEO_MANIFEST_URL_EXPIRES_IN,
        )
    except PointCloudManifestFileUnavailable as exc:
        raise HTTPException(
            status_code=404, detail="Point cloud file not available"
        ) from exc


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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if task.file_type != "video":
        raise HTTPException(status_code=400, detail="Task is not a video task")
    response.headers["Cache-Control"] = "private, max-age=3600"

    if not task.dataset_item_id:
        raise HTTPException(status_code=503, detail="Video metadata not ready")
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    metadata = ctx.metadata
    if not metadata.fps or not metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")

    try:
        async with media_gateway(db) as gateway:
            frames = await gateway.read_all_timetable(
                TimetableRequest(
                    actor_id=current_user.id,
                    dataset_item_id=task.dataset_item_id,
                    task_id=task.id,
                    from_frame=from_frame,
                    to_frame=to_frame,
                )
            )
    except MediaFailure as exc:
        raise media_http_error(exc) from None
    has_timetable = bool(metadata.frame_timetable_frame_count) or bool(frames)

    body = TaskVideoFrameTimetableResponse(
        task_id=task.id,
        fps=metadata.fps,
        frame_count=metadata.frame_count,
        source="ffprobe" if has_timetable else "estimated",
        frames=[
            VideoFrameTimetableEntry(
                frame_index=frame.frame_index,
                pts_ms=frame.pts_ms,
                is_keyframe=frame.is_keyframe,
                pict_type=frame.pict_type,
                byte_offset=frame.byte_offset,
            )
            for frame in frames
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    return await build_video_manifest_v2(db, ctx, str(request.base_url), task=task)


@router.get("/{task_id}/video/segments", response_model=VideoSegmentsResponse)
async def get_video_segments(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    return await list_video_segments(db, ctx, task=task)


@router.post(
    "/{task_id}/video/segments/{segment_id}:claim",
    response_model=VideoSegmentOut,
)
async def claim_video_segment(
    task_id: uuid.UUID,
    segment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await claim_segment(
        db, ctx, segment_id, current_user, privileged=privileged, task=task
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_CLAIM,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.dataset_item_id)},
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await heartbeat_segment(
        db, ctx, segment_id, current_user, privileged=privileged, task=task
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_HEARTBEAT,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.dataset_item_id)},
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    privileged = bool(project and is_privileged_for_project(current_user, project))
    body = await release_segment(
        db, ctx, segment_id, current_user, privileged=privileged, task=task
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_SEGMENT_RELEASE,
        target_type="video_segment",
        target_id=segment_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "dataset_item_id": str(ctx.dataset_item_id)},
    )
    await db.commit()
    return body


async def _segment_privileged(
    db: AsyncSession, task: Task, user: User | IdentityActorFacts
) -> bool:
    project = await get_workspace_project(db, actor_id=None, project_id=task.project_id)
    return bool(
        project
        and (is_privileged_for_project(user, project) or task.reviewer_id == user.id)
    )


def _raise_quality_error(exc: VideoTrackQualityError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _raise_task_workflow_error(exc: TaskWorkflowError) -> None:
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    was_in_review = task.status == "review"
    try:
        body = await submit_segment(
            db,
            ctx,
            segment_id,
            current_user,
            privileged=await _segment_privileged(db, task, current_user),
            task=task,
        )
    except TaskWorkflowError as exc:
        _raise_task_workflow_error(exc)
    neighbors = list(
        (
            await db.execute(
                select(video_segment_row).where(
                    video_segment_row.dataset_item_id == ctx.dataset_item_id,
                    video_segment_row.segment_index.in_(
                        [body.segment_index - 1, body.segment_index + 1]
                    ),
                    video_segment_row.status == "completed",
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
                video_metadata=ctx.metadata,
            )
        except VideoTrackQualityError as exc:
            _raise_quality_error(exc)
        if created and job is not None:
            dispatches.append((run.id, job.id))
    if task.status == "review":
        if not was_in_review:
            contributor_ids = await task_contributor_snapshot(
                # Segments belong to dataset items shared by projects.
                # Only this task's authors and actual submitter establish
                # project contribution; global segment assignees do not.
                db,
                task,
                extra_user_ids=(current_user.id,),
            )
            from app.services.labeling_tasks import LabelingTaskService

            try:
                await LabelingTaskService(db).capture_first_review_contributor_snapshot(
                    task, actor_id=current_user.id, contributor_ids=contributor_ids
                )
            except TaskWorkflowError as exc:
                _raise_task_workflow_error(exc)
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
            await dispatch_quality_run(
                db, run_id=run_id, async_job_id=job_id, dispatcher=quality_dispatcher
            )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    try:
        body = await reopen_segment(
            db, ctx, segment_id, task=task, actor_id=current_user.id
        )
    except TaskWorkflowError as exc:
        _raise_task_workflow_error(exc)
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    body = await unassign_segment(
        db, ctx, segment_id, task=task, actor_id=current_user.id
    )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    data = await spatial_quality_data(db, "list_video_runs", task_id=task.id)
    runs = quality_rows(VideoTrackQualityRun, data.get("rows"))
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
    quality_dispatcher: QualityJobDispatcher = Depends(get_quality_job_dispatcher),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
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
            await dispatch_quality_run(
                db, run_id=run.id, async_job_id=job.id, dispatcher=quality_dispatcher
            )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    data = await spatial_quality_data(db, "get_video_run", run_id=run_id)
    run = quality_row(VideoTrackQualityRun, data["row"]) if data["row"] else None
    if run is None or run.task_id != task.id:
        raise HTTPException(status_code=404, detail="Track quality run not found")
    await refresh_staleness(db, run)
    data = await spatial_quality_data(db, "list_video_issues", run_id=run.id)
    issues = quality_rows(VideoTrackQualityIssue, data.get("rows"))
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_review_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    if not await _segment_privileged(db, task, current_user):
        raise HTTPException(status_code=403, detail="reviewer privilege required")
    data = await spatial_quality_data(db, "lock_video_run", run_id=run_id)
    run = quality_row(VideoTrackQualityRun, data["row"]) if data["row"] else None
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    return await list_video_chunks(db, ctx, from_frame, to_frame)


@router.get(
    "/{task_id}/video/chunks/{chunk_id}",
    response_model=VideoChunkOut,
    responses={
        202: {
            "model": VideoChunkOut,
            "description": "Asset is not ready; inspect status and retry_after before another request.",
            "headers": {"Retry-After": {"schema": {"type": "string"}}},
        }
    },
)
async def get_video_chunk(
    task_id: uuid.UUID,
    chunk_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    body = await get_video_chunk_asset(db, ctx, chunk_id)
    if body.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = str(body.retry_after or 3)
    return body


@router.get(
    "/{task_id}/video/frames/{frame_index}",
    response_model=VideoFrameOut,
    responses={
        202: {
            "model": VideoFrameOut,
            "description": "Asset is not ready; inspect status and retry_after before another request.",
            "headers": {"Retry-After": {"schema": {"type": "string"}}},
        }
    },
)
async def get_video_frame(
    task_id: uuid.UUID,
    frame_index: int,
    response: Response,
    format: Literal["webp", "jpeg"] = Query(default="webp"),
    w: int = Query(default=512, ge=1, le=4096),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    return await prefetch_video_frames(
        db,
        ctx,
        payload.frame_indices,
        payload.width,
        payload.format,
    )


@router.post(
    "/{task_id}/video/frames:retry",
    response_model=VideoFramePrefetchResponse,
)
async def retry_video_frame_assets(
    task_id: uuid.UUID,
    payload: VideoFrameRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id)
    ctx = await AssetsVideoContextReader(db).from_task_summary(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    return await retry_video_frames(
        db,
        ctx,
        payload.frame_indices,
        payload.width,
        payload.format,
        force=payload.force,
        task_annotation_retry=True,
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await lock_task_for_core_read(db, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    raw_if_match = request.headers.get("If-Match", "").strip()
    if not raw_if_match:
        raise HTTPException(status_code=428, detail={"reason": "if_match_required"})
    try:
        expected_version = int(raw_if_match.removeprefix('W/"').removesuffix('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid If-Match format") from exc
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
):
    task = await lock_task_for_core_read(db, task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    raw_if_match = request.headers.get("If-Match", "").strip()
    if not raw_if_match:
        raise HTTPException(status_code=428, detail={"reason": "if_match_required"})
    try:
        expected_version = int(raw_if_match.removeprefix('W/"').removesuffix('"'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid If-Match format") from exc
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
    tracker_dispatcher: VideoTrackerJobDispatcher = Depends(
        get_video_tracker_job_dispatcher
    ),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
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
        enqueued = await enqueue_tracker_job(
            db,
            body.id,
            dispatcher=tracker_dispatcher,
            fail_closed=True,
        )
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
    tracker_dispatcher: VideoTrackerJobDispatcher = Depends(
        get_video_tracker_job_dispatcher
    ),
):
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    body = await create_tracker_job(
        db,
        task=task,
        ctx=ctx,
        annotation_id=annotation_id,
        payload=payload,
        user=current_user,
        dispatcher=tracker_dispatcher,
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
            "task_id": str(task_id),
            "annotation_id": str(annotation_id),
            "dataset_item_id": str(ctx.dataset_item_id),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
    access: ProjectAccess = Depends(require_task_annotation_write),
    tracker_dispatcher: VideoTrackerJobDispatcher = Depends(
        get_video_tracker_job_dispatcher
    ),
):
    """v0.22.1 · B · 任务级追踪 (画布级入口): source_annotation_id 可选——给出即延展该轨迹,
    缺省则为无源检测 (文本/种子), 新建轨迹类别由 target_class_name 指定。旧的
    /tracks/{annotation_id}:propagate 仍保留 (延展快捷路径)。"""
    task = await _load_task_or_404(db, task_id)
    await task_access.assert_task_visible(db, task, current_user.id, access=access)
    task_access.assert_task_editable(task, current_user.id, access=access)
    ctx = await AssetsVideoContextReader(db).from_task(
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
        file_type=task.file_type,
        dataset_item_id=task.dataset_item_id,
    )
    body = await create_tracker_job(
        db,
        task=task,
        ctx=ctx,
        annotation_id=payload.source_annotation_id,
        payload=payload,
        user=current_user,
        dispatcher=tracker_dispatcher,
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
            "task_id": str(task_id),
            "annotation_id": str(payload.source_annotation_id)
            if payload.source_annotation_id
            else None,
            # v0.22.2 · M · 多选批量: 记录批量延展的源轨迹 ids (多源时 annotation_id 为 None)。
            "source_annotation_ids": [str(a) for a in payload.source_annotation_ids]
            if payload.source_annotation_ids
            else None,
            "dataset_item_id": str(ctx.dataset_item_id),
            "segment_id": str(body.segment_id) if body.segment_id else None,
            "from_frame": body.from_frame,
            "to_frame": body.to_frame,
            "model_key": body.model_key,
            "direction": body.direction,
        },
    )
    await db.commit()
    return body
