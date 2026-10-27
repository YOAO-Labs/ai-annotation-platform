from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.db.models.task import Task
from app.services.identity_contracts import IdentityActorFacts
from app.db.models.video_chapter import VideoChapter
from app.deps import get_current_user, get_db, require_active_task_actor
from app.schemas.task import (
    TaskVideoFrameTimetableResponse,
)
from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterList,
    VideoChapterOut,
    VideoChapterUpdate,
)
from app.schemas.video_frame_service import (
    VideoChunkOut,
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
from aap_workspace_contract.access import ProjectAccess
from app.application.task_workflow_errors import TaskWorkflowError
from app.application.video_chapters import VideoChapterWorkflow
from app.services.labeling_core_read_relations import task_row
from app.services.video_frame_service import (
    build_context_from_dataset_item,
    authorize_context,
    frame_timetable as build_frame_timetable,
    chunk_samples as build_chunk_samples,
    get_chunk as get_video_chunk_asset,
    get_frame as get_video_frame_asset,
    list_chunks as list_video_chunks,
    manifest_v2 as build_video_manifest_v2,
    prefetch_frames as prefetch_video_frames,
    retry_frames as retry_video_frames,
)
from app.services.video_segment_service import list_segments as list_video_segments

router = APIRouter(dependencies=[Depends(require_active_task_actor)])


async def _video_chapter_target_context(
    db: AsyncSession, dataset_item_id: uuid.UUID, user: IdentityActorFacts
) -> tuple[uuid.UUID, ProjectAccess]:
    task = await _visible_video_task_for_item(db, dataset_item_id, user)
    access = await task_access.resolve_task_access(db, task, user.id)
    return task.id, access


def _raise_task_workflow_error(error: TaskWorkflowError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


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
    db: AsyncSession, dataset_item_id: uuid.UUID, user: IdentityActorFacts
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


@router.get("/{dataset_item_id}/manifest", response_model=VideoManifestV2Response)
async def get_video_manifest(
    dataset_item_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
    return await build_video_manifest_v2(db, ctx, str(request.base_url))


@router.get("/{dataset_item_id}/segments", response_model=VideoSegmentsResponse)
async def get_video_segments(
    dataset_item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await build_context_from_dataset_item(
        db, dataset_item_id, task=task, actor_id=current_user.id
    )
    return await list_video_segments(db, ctx, task=task)


@router.get(
    "/{dataset_item_id}/frame-timetable",
    response_model=TaskVideoFrameTimetableResponse,
)
async def get_video_frame_timetable(
    dataset_item_id: uuid.UUID,
    request: Request,
    response: Response,
    from_frame: int | None = Query(default=None, ge=0, alias="from"),
    to_frame: int | None = Query(default=None, ge=0, alias="to"),
    expected_metadata_generation_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
    body = await build_frame_timetable(
        db,
        ctx,
        from_frame,
        to_frame,
        expected_metadata_generation_id=expected_metadata_generation_id,
    )
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["ETag"] = (
        f'"video-timetable:{body.metadata_generation_id}:{from_frame}:{to_frame}"'
    )
    return body


@router.get("/{dataset_item_id}/chunks", response_model=VideoChunksResponse)
async def get_video_chunks(
    dataset_item_id: uuid.UUID,
    request: Request,
    from_frame: int | None = Query(default=None, ge=0),
    to_frame: int | None = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
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
    request: Request,
    chunk_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
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
    request: Request,
    expected_output_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
) -> VideoChunkSamplesResponse:
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
    return await build_chunk_samples(
        db, ctx, chunk_id, expected_output_id=expected_output_id
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
    request: Request,
    frame_index: int,
    response: Response,
    format: Literal["webp", "jpeg"] = Query(default="webp"),
    w: int = Query(default=512, ge=1, le=4096),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
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
    request: Request,
    payload: VideoFramePrefetchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
    return await prefetch_video_frames(
        db, ctx, payload.frame_indices, payload.width, payload.format
    )


@router.post(
    "/{dataset_item_id}/frames:retry",
    response_model=VideoFramePrefetchResponse,
)
async def retry_video_frame_assets(
    dataset_item_id: uuid.UUID,
    request: Request,
    payload: VideoFrameRetryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    task = await _visible_video_task_for_item(db, dataset_item_id, current_user)
    ctx = await authorize_context(db, task, current_user, request)
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
