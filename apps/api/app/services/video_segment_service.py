from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.project_access import get_workspace_project
from app.db.models.dataset import VideoSegment
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.video_frame_service import VideoSegmentOut, VideoSegmentsResponse
from app.services.video_context import VideoContext
from app.services.video_collaboration import (
    collaboration_config,
    segment_work_bounds,
)
from app.services.annotation_evidence import (
    refresh_task_evidence,
    task_contributor_snapshot,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.labeling_core_read_relations import (
    lock_video_segment_for_core_read,
    video_segment_row,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(row: VideoSegment, now: datetime) -> bool:
    return bool(row.locked_by and row.lock_expires_at and row.lock_expires_at <= now)


def _normalize_lock(row: VideoSegment, now: datetime) -> None:
    if row.status == "completed":
        return
    if _is_expired(row, now):
        row.locked_by = None
        row.locked_at = None
        row.lock_expires_at = None
    if row.locked_by and row.lock_expires_at and row.lock_expires_at > now:
        row.status = "locked"
    elif row.assignee_id:
        row.status = "assigned"
    elif row.status != "completed":
        row.status = "open"


def _segment_count(ctx: VideoContext) -> int:
    frame_count = int(ctx.metadata.frame_count or 1)
    size = max(1, settings.video_segment_size_frames)
    return max(1, (max(1, frame_count) + size - 1) // size)


def _segment_bounds(ctx: VideoContext, segment_index: int) -> tuple[int, int]:
    frame_count = max(1, int(ctx.metadata.frame_count or 1))
    size = max(1, settings.video_segment_size_frames)
    start = segment_index * size
    end = min(frame_count - 1, start + size - 1)
    return start, end


async def ensure_segments(db: AsyncSession, ctx: VideoContext) -> list[VideoSegment]:
    rows = (
        (
            await db.execute(
                select(video_segment_row)
                .where(video_segment_row.dataset_item_id == ctx.dataset_item_id)
                .order_by(video_segment_row.segment_index.asc())
            )
        )
        .scalars()
        .all()
    )
    if rows:
        now = _now()
        for row in rows:
            _normalize_lock(row, now)
        await db.flush()
        return list(rows)

    # 并发安全: 两个请求可能同时发现 segments 为空, 都批量 insert 0..N, 撞
    # uq_video_segments_item_segment → IntegrityError。segment 是「要么全有要么全建」的
    # 全量创建, 故用一个 SAVEPOINT 包住整批 INSERT; 冲突时回滚整批, 再 select 拿别的请求
    # 建好的全量 segments (并按已存在分支的口径 normalize lock)。
    try:
        async with db.begin_nested():
            created = []
            for segment_index in range(_segment_count(ctx)):
                start, end = _segment_bounds(ctx, segment_index)
                row = VideoSegment(
                    dataset_item_id=ctx.dataset_item_id,
                    segment_index=segment_index,
                    start_frame=start,
                    end_frame=end,
                    status="open",
                )
                db.add(row)
                created.append(row)
            await db.flush()
        return created
    except IntegrityError:
        rows = (
            (
                await db.execute(
                    select(video_segment_row)
                    .where(video_segment_row.dataset_item_id == ctx.dataset_item_id)
                    .order_by(video_segment_row.segment_index.asc())
                )
            )
            .scalars()
            .all()
        )
        now = _now()
        for row in rows:
            _normalize_lock(row, now)
        await db.flush()
        return list(rows)


def segment_out(
    row: VideoSegment,
    *,
    frame_count: int | None = None,
    segment_count: int | None = None,
    overlap_frames: int = 0,
) -> VideoSegmentOut:
    work_start, work_end = segment_work_bounds(
        start_frame=row.start_frame,
        end_frame=row.end_frame,
        segment_index=row.segment_index,
        segment_count=segment_count or 1,
        frame_count=frame_count or row.end_frame + 1,
        overlap_frames=overlap_frames,
    )
    return VideoSegmentOut(
        id=row.id,
        segment_index=row.segment_index,
        start_frame=row.start_frame,
        end_frame=row.end_frame,
        work_start_frame=work_start,
        work_end_frame=work_end,
        status=row.status
        if row.status in {"open", "assigned", "locked", "completed"}
        else "open",
        assignee_id=row.assignee_id,
        locked_by=row.locked_by,
        locked_at=row.locked_at,
        lock_expires_at=row.lock_expires_at,
    )


async def _segment_out_for_context(
    db: AsyncSession, ctx: VideoContext, row: VideoSegment
) -> VideoSegmentOut:

    project = (
        await get_workspace_project(db, project_id=ctx.project_id)
        if ctx.project_id
        else None
    )
    config = collaboration_config(project)
    return segment_out(
        row,
        frame_count=max(1, int(ctx.metadata.frame_count or 1)),
        segment_count=_segment_count(ctx),
        overlap_frames=config.overlap_frames if config.enabled else 0,
    )


async def list_segments(db: AsyncSession, ctx: VideoContext) -> VideoSegmentsResponse:
    rows = await ensure_segments(db, ctx)

    project = (
        await get_workspace_project(db, project_id=ctx.project_id)
        if ctx.project_id
        else None
    )
    config = collaboration_config(project)
    await db.commit()
    return VideoSegmentsResponse(
        dataset_item_id=ctx.dataset_item_id,
        task_id=ctx.task_id,
        segment_size_frames=max(1, settings.video_segment_size_frames),
        collaboration_enabled=config.enabled,
        overlap_frames=config.overlap_frames if config.enabled else 0,
        segments=[
            segment_out(
                row,
                frame_count=max(1, int(ctx.metadata.frame_count or 1)),
                segment_count=len(rows),
                overlap_frames=config.overlap_frames if config.enabled else 0,
            )
            for row in rows
        ],
    )


async def _load_segment_for_update(
    db: AsyncSession, ctx: VideoContext, segment_id: uuid.UUID
) -> VideoSegment:
    row = await lock_video_segment_for_core_read(db, segment_id, ctx.dataset_item_id)
    if row is None:
        await ensure_segments(db, ctx)
        row = await lock_video_segment_for_core_read(
            db, segment_id, ctx.dataset_item_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Video segment not found")
    return row


def _assert_can_touch_lock(
    row: VideoSegment, user: User | IdentityActorFacts, privileged: bool
) -> None:
    if privileged or row.locked_by is None or row.locked_by == user.id:
        return
    raise HTTPException(
        status_code=403, detail="Video segment lock belongs to another user"
    )


def _assert_context_task_binding(ctx: VideoContext, task: Task | None) -> None:
    if task is None:
        task_id = None
    else:
        state = inspect(task)
        identity = state.identity
        task_id = state.dict.get("id", identity[0] if identity else None)
    if task_id != ctx.task_id or (task is not None and ctx.task_id is None):
        raise ValueError("video context task binding mismatch")


async def claim_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
) -> VideoSegmentOut:
    row = await _load_segment_for_update(db, ctx, segment_id)
    now = _now()
    _normalize_lock(row, now)
    if row.status == "completed":
        raise HTTPException(
            status_code=409, detail={"reason": "video_segment_completed"}
        )

    if row.assignee_id and row.assignee_id != user.id and not privileged:
        raise HTTPException(
            status_code=403, detail="Video segment is assigned to another user"
        )
    if row.locked_by and row.locked_by != user.id and not privileged:
        raise HTTPException(
            status_code=409, detail="Video segment is locked by another user"
        )

    row.assignee_id = row.assignee_id or user.id
    row.locked_by = user.id
    row.locked_at = now
    row.lock_expires_at = now + timedelta(
        seconds=settings.video_segment_lock_ttl_seconds
    )
    row.status = "locked"
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def heartbeat_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
) -> VideoSegmentOut:
    row = await _load_segment_for_update(db, ctx, segment_id)
    now = _now()
    _normalize_lock(row, now)
    if row.locked_by is None:
        raise HTTPException(status_code=409, detail="Video segment is not locked")
    _assert_can_touch_lock(row, user, privileged)
    row.lock_expires_at = now + timedelta(
        seconds=settings.video_segment_lock_ttl_seconds
    )
    row.status = "locked"
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def release_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
) -> VideoSegmentOut:
    row = await _load_segment_for_update(db, ctx, segment_id)
    now = _now()
    _normalize_lock(row, now)
    if row.status == "completed":
        return await _segment_out_for_context(db, ctx, row)
    if row.locked_by is not None:
        _assert_can_touch_lock(row, user, privileged)
    row.locked_by = None
    row.locked_at = None
    row.lock_expires_at = None
    row.status = "assigned" if row.assignee_id else "open"
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def submit_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
    task: Task | None = None,
) -> VideoSegmentOut:
    _assert_context_task_binding(ctx, task)
    if task is not None:
        from app.services.annotation_evidence import refresh_task_evidence

        # A2 · lock the task (and refresh its evidence) before the segment lock
        # so the entry into review freezes under the same task lock.
        await refresh_task_evidence(db, task)
    row = await _load_segment_for_update(db, ctx, segment_id)
    now = _now()
    _normalize_lock(row, now)
    if row.status == "completed":
        return await _segment_out_for_context(db, ctx, row)
    if not privileged:
        if row.assignee_id != user.id:
            raise HTTPException(
                status_code=403,
                detail={"reason": "video_segment_not_assigned_to_user"},
            )
        if (
            row.locked_by != user.id
            or not row.lock_expires_at
            or row.lock_expires_at <= now
        ):
            raise HTTPException(
                status_code=409,
                detail={"reason": "video_segment_lease_required"},
            )
    if task is not None:
        # A2 · every successful segment submitter is a contributor even when the
        # segment kept no surviving annotation. This uses the actual actor, not
        # the shared dataset-item segment assignee.
        from app.services.annotation_evidence import record_annotation_actor

        await record_annotation_actor(db, task, user.id)
    row.status = "completed"
    row.locked_by = None
    row.locked_at = None
    row.lock_expires_at = None
    if task is not None:
        remaining = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(video_segment_row)
                    .where(
                        video_segment_row.dataset_item_id == ctx.dataset_item_id,
                        video_segment_row.id != row.id,
                        video_segment_row.status != "completed",
                    )
                )
            ).scalar_one()
        )
        if remaining == 0:
            from app.services.labeling_tasks import LabelingTaskService

            labeling = LabelingTaskService(db)
            await labeling.complete_video_review(task, actor_id=user.id, now=now)
            # A2 · freeze the round evidence when the video task enters review.
            contributor_ids = await task_contributor_snapshot(db, task)
            await labeling.freeze_review_contributor_evidence(
                task,
                submitter_id=user.id,
                contributor_ids=contributor_ids,
            )
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def reopen_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    *,
    task: Task | None = None,
    actor_id: uuid.UUID | None = None,
) -> VideoSegmentOut:
    _assert_context_task_binding(ctx, task)
    if task is not None:
        await refresh_task_evidence(db, task)
    row = await _load_segment_for_update(db, ctx, segment_id)
    row.status = "assigned" if row.assignee_id else "open"
    row.locked_by = None
    row.locked_at = None
    row.lock_expires_at = None
    if task is not None:
        if actor_id is None:
            raise ValueError("Reopening a task video segment requires its actor")
        from app.services.labeling_tasks import LabelingTaskService

        labeling = LabelingTaskService(db)
        await labeling.reopen_video_segment(task, actor_id=actor_id)
        await labeling.clear_review_contributor_evidence(task, actor_id=actor_id)
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def unassign_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
) -> VideoSegmentOut:
    row = await _load_segment_for_update(db, ctx, segment_id)
    if row.status == "completed":
        raise HTTPException(
            status_code=409,
            detail={"reason": "video_segment_reopen_required"},
        )
    row.assignee_id = None
    row.status = "open"
    row.locked_by = None
    row.locked_at = None
    row.lock_expires_at = None
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)
