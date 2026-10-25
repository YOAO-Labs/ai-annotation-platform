from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from aap_assets_media.contract import MediaFailure, SamplesRequest, TimetableRequest

from app.services import task_access
from aap_workspace_contract.access import ProjectAccess
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_chapter import VideoChapter
from app.deps import (
    get_current_user,
    get_db,
    require_active_task_actor,
)
from app.schemas.task import (
    TaskVideoFrameTimetableResponse,
    VideoFrameTimetableEntry,
)
from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterList,
    VideoChapterOut,
    VideoChapterUpdate,
)
from app.schemas.video_frame_service import (
    VideoChunkOut,
    VideoChunkSampleOut,
    VideoChunkSamplesResponse,
    VideoChunksResponse,
    VideoFrameOut,
    VideoFramePrefetchRequest,
    VideoFramePrefetchResponse,
    VideoFrameRetryRequest,
    VideoManifestV2Response,
    VideoSegmentsResponse,
)
from app.application.task_workflow_errors import TaskWorkflowError
from app.application.video_chapters import VideoChapterWorkflow
from app.services.audit import AuditAction, AuditService
from app.services.assets_media import media_gateway, media_http_error
from app.services.video_frame_service import (
    AssetsVideoContextReader,
    get_chunk as get_video_chunk_asset,
    get_frame as get_video_frame_asset,
    list_chunks as list_video_chunks,
    manifest_v2 as build_video_manifest_v2,
    prefetch_frames as prefetch_video_frames,
    retry_frames as retry_video_frames,
)
from app.services.video_segment_service import list_segments as list_video_segments
from app.services.identity_contracts import IdentityActorFacts

from app.services.labeling_core_read_relations import task_row

router = APIRouter(dependencies=[Depends(require_active_task_actor)])


def _chapter_out(row: VideoChapter) -> VideoChapterOut:
    return VideoChapterOut.model_validate(
        {
            "id": row.id,
            "dataset_item_id": row.dataset_item_id,
            "start_frame": row.start_frame,
            "end_frame": row.end_frame,
            "title": row.title,
            "color": row.color,
            "metadata": dict(row.chapter_metadata or {}),
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


async def _visible_video_task_for_item(
    db: AsyncSession, dataset_item_id: uuid.UUID, user: User | IdentityActorFacts
) -> Task:
    tasks = (
        await db.execute(
            select(task_row).where(
                task_row.dataset_item_id == dataset_item_id,
                task_row.file_type == "video",
            )
        )
    ).scalars()
    for task in tasks:
        try:
            await task_access.assert_task_visible(db, task, user.id)
            return task
        except HTTPException:
            continue
    raise HTTPException(status_code=404, detail="Video not found")


async def _video_chapter_target_context(
    db: AsyncSession, dataset_item_id: uuid.UUID, user: User | IdentityActorFacts
) -> tuple[uuid.UUID, ProjectAccess]:
    task = await _visible_video_task_for_item(db, dataset_item_id, user)
    access = await task_access.resolve_task_access(db, task, user.id)
    return task.id, access


def _raise_task_workflow_error(error: TaskWorkflowError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


@router.get("/{dataset_item_id}/manifest", response_model=VideoManifestV2Response)
async def get_video_manifest(
    dataset_item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    return await build_video_manifest_v2(db, ctx, str(request.base_url), task=task)


@router.get("/{dataset_item_id}/segments", response_model=VideoSegmentsResponse)
async def get_video_segments(
    dataset_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    return await list_video_segments(db, ctx, task=task)


@router.get(
    "/{dataset_item_id}/frame-timetable",
    response_model=TaskVideoFrameTimetableResponse,
)
async def get_video_frame_timetable(
    dataset_item_id: uuid.UUID,
    response: Response,
    from_frame: int | None = Query(default=None, ge=0, alias="from"),
    to_frame: int | None = Query(default=None, ge=0, alias="to"),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    if not ctx.metadata.fps or not ctx.metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")

    try:
        async with media_gateway(db) as gateway:
            frames = await gateway.read_all_timetable(
                TimetableRequest(
                    actor_id=current_user.id,
                    dataset_item_id=dataset_item_id,
                    task_id=task.id,
                    from_frame=from_frame,
                    to_frame=to_frame,
                )
            )
    except MediaFailure as exc:
        raise media_http_error(exc) from None
    has_timetable = bool(ctx.metadata.frame_timetable_frame_count) or bool(frames)
    body = TaskVideoFrameTimetableResponse(
        task_id=task.id,
        fps=ctx.metadata.fps,
        frame_count=ctx.metadata.frame_count,
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
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["ETag"] = (
        f'"video-timetable:{dataset_item_id}:{ctx.metadata.frame_count}:'
        f'{len(body.frames)}:{from_frame or 0}:{to_frame or ""}"'
    )
    return body


@router.get("/{dataset_item_id}/chunks", response_model=VideoChunksResponse)
async def get_video_chunks(
    dataset_item_id: uuid.UUID,
    from_frame: int | None = Query(default=None, ge=0),
    to_frame: int | None = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    return await list_video_chunks(db, ctx, from_frame, to_frame)


@router.get(
    "/{dataset_item_id}/chunks/{chunk_id}",
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
    dataset_item_id: uuid.UUID,
    chunk_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    body = await get_video_chunk_asset(db, ctx, chunk_id)
    if body.status == "pending":
        response.status_code = 202
        response.headers["Retry-After"] = str(body.retry_after or 3)
    return body


@router.get(
    "/{dataset_item_id}/chunks/{chunk_id}/samples",
    response_model=VideoChunkSamplesResponse,
)
async def get_video_chunk_samples(
    dataset_item_id: uuid.UUID,
    chunk_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> VideoChunkSamplesResponse:
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    try:
        async with media_gateway(db) as gateway:
            document = await gateway.read_samples_document(
                SamplesRequest(
                    actor_id=current_user.id,
                    dataset_item_id=dataset_item_id,
                    task_id=task.id,
                    chunk_id=chunk_id,
                )
            )
    except MediaFailure as exc:
        raise media_http_error(exc) from None
    if not document.samples:
        raise HTTPException(status_code=404, detail="samples_not_available")
    return VideoChunkSamplesResponse(
        dataset_item_id=dataset_item_id,
        chunk_id=chunk_id,
        codec_string=document.codec_string,
        description=document.description,
        width=document.width,
        height=document.height,
        samples=[
            VideoChunkSampleOut.model_validate(sample.model_dump())
            for sample in document.samples
        ],
    )


@router.get(
    "/{dataset_item_id}/frames/{frame_index}",
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
    dataset_item_id: uuid.UUID,
    frame_index: int,
    response: Response,
    format: Literal["webp", "jpeg"] = Query(default="webp"),
    w: int = Query(default=512, ge=1, le=4096),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
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
    "/{dataset_item_id}/frames:prefetch",
    response_model=VideoFramePrefetchResponse,
)
async def prefetch_video_frame_assets(
    dataset_item_id: uuid.UUID,
    payload: VideoFramePrefetchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    return await prefetch_video_frames(
        db,
        ctx,
        payload.frame_indices,
        payload.width,
        payload.format,
    )


@router.post(
    "/{dataset_item_id}/frames:retry",
    response_model=VideoFramePrefetchResponse,
)
async def retry_video_frame_assets(
    dataset_item_id: uuid.UUID,
    payload: VideoFrameRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await AssetsVideoContextReader(db).from_dataset_item(
        dataset_item_id=dataset_item_id,
        actor_id=current_user.id,
        task_id=task.id,
        project_id=task.project_id,
    )
    return await retry_video_frames(
        db,
        ctx,
        payload.frame_indices,
        payload.width,
        payload.format,
        force=payload.force,
    )


@router.get("/{dataset_item_id}/chapters", response_model=VideoChapterList)
async def list_video_chapters(
    dataset_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    await _visible_video_task_for_item(db, dataset_item_id, current_user)
    rows = (
        (
            await db.execute(
                select(VideoChapter)
                .where(VideoChapter.dataset_item_id == dataset_item_id)
                .order_by(VideoChapter.start_frame.asc(), VideoChapter.end_frame.asc())
            )
        )
        .scalars()
        .all()
    )
    return VideoChapterList(chapters=[_chapter_out(row) for row in rows])


@router.post(
    "/{dataset_item_id}/chapters",
    response_model=VideoChapterOut,
    status_code=201,
)
async def create_video_chapter(
    dataset_item_id: uuid.UUID,
    payload: VideoChapterCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task_id, expected_access = await _video_chapter_target_context(
        db, dataset_item_id, current_user
    )
    try:
        chapter = await VideoChapterWorkflow(db).create(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=current_user.id,
            expected_access=expected_access,
            payload=payload,
        )
    except TaskWorkflowError as exc:
        _raise_task_workflow_error(exc)
    body = _chapter_out(chapter)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_CHAPTER_CREATE,
        target_type="video_chapter",
        target_id=chapter.id,
        request=request,
        status_code=201,
        detail={
            "dataset_item_id": str(dataset_item_id),
            "start_frame": chapter.start_frame,
            "end_frame": chapter.end_frame,
            "title": chapter.title,
        },
    )
    await db.commit()
    return body


@router.patch(
    "/{dataset_item_id}/chapters/{chapter_id}",
    response_model=VideoChapterOut,
)
async def update_video_chapter(
    dataset_item_id: uuid.UUID,
    chapter_id: uuid.UUID,
    payload: VideoChapterUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task_id, expected_access = await _video_chapter_target_context(
        db, dataset_item_id, current_user
    )
    try:
        chapter, changes = await VideoChapterWorkflow(db).update(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=current_user.id,
            expected_access=expected_access,
            chapter_id=chapter_id,
            payload=payload,
        )
    except TaskWorkflowError as exc:
        _raise_task_workflow_error(exc)
    # onupdate=func.now() 令 updated_at 在 flush 后被 expire; 若不 refresh, _chapter_out 读取它
    # 会触发异步上下文外的惰性加载 (MissingGreenlet → 500)。显式 refresh 在 greenlet 内取回新值。
    await db.refresh(chapter)
    body = _chapter_out(chapter)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_CHAPTER_UPDATE,
        target_type="video_chapter",
        target_id=chapter.id,
        request=request,
        status_code=200,
        detail={
            "dataset_item_id": str(dataset_item_id),
            "changes": changes,
        },
    )
    await db.commit()
    return body


@router.delete(
    "/{dataset_item_id}/chapters/{chapter_id}",
    status_code=204,
)
async def delete_video_chapter(
    dataset_item_id: uuid.UUID,
    chapter_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task_id, expected_access = await _video_chapter_target_context(
        db, dataset_item_id, current_user
    )
    try:
        snapshot = await VideoChapterWorkflow(db).delete(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=current_user.id,
            expected_access=expected_access,
            chapter_id=chapter_id,
        )
    except TaskWorkflowError as exc:
        _raise_task_workflow_error(exc)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_CHAPTER_DELETE,
        target_type="video_chapter",
        target_id=chapter_id,
        request=request,
        status_code=204,
        detail={"dataset_item_id": str(dataset_item_id), **snapshot},
    )
    await db.commit()
    return Response(status_code=204)
