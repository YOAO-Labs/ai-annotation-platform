from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks import _assert_task_visible
from app.db.models.dataset import VideoChunk, VideoFrameIndex
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_chapter import VideoChapter
from app.deps import get_current_user, get_db, require_active_task_actor
from app.schemas.task import (
    TaskVideoFrameTimetableResponse,
    VideoFrameTimetableEntry,
)
from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterList,
    VideoChapterOut,
    VideoChapterUpdate,
    merge_chapter_metadata,
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
from app.services.audit import AuditAction, AuditService
from app.services.project_access import resolve_project_access
from app.services.video_frame_service import (
    build_context_from_dataset_item,
    get_chunk as get_video_chunk_asset,
    get_frame as get_video_frame_asset,
    list_chunks as list_video_chunks,
    manifest_v2 as build_video_manifest_v2,
    prefetch_frames as prefetch_video_frames,
    retry_frames as retry_video_frames,
)
from app.services.video_segment_service import list_segments as list_video_segments

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


async def _assert_chapter_editor(db: AsyncSession, task: Task, user: User) -> None:
    project = await db.get(Project, task.project_id)
    if project is not None:
        access = await resolve_project_access(db, user=user, project=project)
        if access.is_manager:
            return
    raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可管理章节")


async def _visible_video_task_for_item(
    db: AsyncSession, dataset_item_id: uuid.UUID, user: User
) -> Task:
    tasks = (
        await db.execute(
            select(Task).where(
                Task.dataset_item_id == dataset_item_id,
                Task.file_type == "video",
            )
        )
    ).scalars()
    for task in tasks:
        try:
            await _assert_task_visible(db, task, user)
            return task
        except HTTPException:
            continue
    raise HTTPException(status_code=404, detail="Video not found")


@router.get("/{dataset_item_id}/manifest", response_model=VideoManifestV2Response)
async def get_video_manifest(
    dataset_item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
    return await build_video_manifest_v2(db, ctx, str(request.base_url))


@router.get("/{dataset_item_id}/segments", response_model=VideoSegmentsResponse)
async def get_video_segments(
    dataset_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
    return await list_video_segments(db, ctx)


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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
    if not ctx.metadata.fps or not ctx.metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")

    has_timetable = (
        await db.execute(
            select(func.count(VideoFrameIndex.id)).where(
                VideoFrameIndex.dataset_item_id == dataset_item_id
            )
        )
    ).scalar_one() > 0
    stmt = select(VideoFrameIndex).where(
        VideoFrameIndex.dataset_item_id == dataset_item_id
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
        fps=ctx.metadata.fps,
        frame_count=ctx.metadata.frame_count,
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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
    return await list_video_chunks(db, ctx, from_frame, to_frame)


@router.get("/{dataset_item_id}/chunks/{chunk_id}", response_model=VideoChunkOut)
async def get_video_chunk(
    dataset_item_id: uuid.UUID,
    chunk_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
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
    current_user: User = Depends(get_current_user),
) -> VideoChunkSamplesResponse:
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    await build_context_from_dataset_item(db, dataset_item_id, task=task)
    row = await db.scalar(
        select(VideoChunk).where(
            VideoChunk.dataset_item_id == dataset_item_id,
            VideoChunk.chunk_id == chunk_id,
        )
    )
    if row is None or not row.diagnostics or "samples" not in row.diagnostics:
        raise HTTPException(status_code=404, detail="samples_not_available")
    d = row.diagnostics
    return VideoChunkSamplesResponse(
        dataset_item_id=dataset_item_id,
        chunk_id=chunk_id,
        codec_string=d.get("codec_string", "avc1.4d001e"),
        description=d.get("description"),
        width=d.get("width", 0),
        height=d.get("height", 0),
        samples=[VideoChunkSampleOut(**s) for s in d["samples"]],
    )


@router.get("/{dataset_item_id}/frames/{frame_index}", response_model=VideoFrameOut)
async def get_video_frame(
    dataset_item_id: uuid.UUID,
    frame_index: int,
    response: Response,
    format: Literal["webp", "jpeg"] = Query(default="webp"),
    w: int = Query(default=512, ge=1, le=4096),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
    return await prefetch_video_frames(
        db, ctx, payload.frame_indices, payload.width, payload.format
    )


@router.post(
    "/{dataset_item_id}/frames:retry",
    response_model=VideoFramePrefetchResponse,
)
async def retry_video_frame_assets(
    dataset_item_id: uuid.UUID,
    payload: VideoFrameRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(db, dataset_item_id, task=task)
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
    current_user: User = Depends(get_current_user),
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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    await _assert_chapter_editor(db, task, current_user)
    chapter = VideoChapter(
        dataset_item_id=dataset_item_id,
        start_frame=payload.start_frame,
        end_frame=payload.end_frame,
        title=payload.title,
        color=payload.color,
        # frame_step / source 落进 chapter_metadata 约定键 (不新增数据库列)。
        chapter_metadata=merge_chapter_metadata(
            payload.metadata,
            frame_step=payload.frame_step,
            source=payload.source,
        ),
        created_by=current_user.id,
    )
    db.add(chapter)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="章节起止帧与现有章节重复",
        )
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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    await _assert_chapter_editor(db, task, current_user)
    chapter = await db.get(VideoChapter, chapter_id)
    if chapter is None or chapter.dataset_item_id != dataset_item_id:
        raise HTTPException(status_code=404, detail="章节不存在")

    changes: dict[str, object] = {}
    if payload.start_frame is not None:
        chapter.start_frame = payload.start_frame
        changes["start_frame"] = payload.start_frame
    if payload.end_frame is not None:
        chapter.end_frame = payload.end_frame
        changes["end_frame"] = payload.end_frame
    if chapter.end_frame < chapter.start_frame:
        raise HTTPException(status_code=422, detail="end_frame 必须 >= start_frame")
    if payload.title is not None:
        chapter.title = payload.title
        changes["title"] = payload.title
    if payload.color is not None:
        chapter.color = payload.color
        changes["color"] = payload.color
    # metadata / frame_step / source 都写进 chapter_metadata: 给出 metadata 时以它为基底,
    # 否则在现有值上叠加; frame_step / source 显式给出时覆盖对应键。
    if (
        payload.metadata is not None
        or payload.frame_step is not None
        or payload.source is not None
    ):
        base = (
            payload.metadata
            if payload.metadata is not None
            else chapter.chapter_metadata
        )
        chapter.chapter_metadata = merge_chapter_metadata(
            base,
            frame_step=payload.frame_step,
            source=payload.source,
        )
        changes["metadata"] = True

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="章节起止帧与现有章节重复",
        )
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
    current_user: User = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    await _assert_chapter_editor(db, task, current_user)
    chapter = await db.get(VideoChapter, chapter_id)
    if chapter is None or chapter.dataset_item_id != dataset_item_id:
        raise HTTPException(status_code=404, detail="章节不存在")
    snapshot = {
        "start_frame": chapter.start_frame,
        "end_frame": chapter.end_frame,
        "title": chapter.title,
    }
    await db.delete(chapter)
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
