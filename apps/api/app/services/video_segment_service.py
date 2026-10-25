from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from aap_labeling.video_control_contract import (
    VIDEO_CONTROL_PROTOCOL,
    ClaimVideoSegment,
    HeartbeatVideoSegment,
    PrepareVideoSegments,
    ReleaseVideoSegment,
    ReopenVideoSegment,
    SubmitVideoSegment,
    UnassignVideoSegment,
)

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
    get_core_row,
    lock_video_segment_for_core_read,
    task_row,
    video_segment_row,
)
from app.services.video_tracking.control import (
    VideoControlRejected,
    video_segment_lifecycle_command,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _segment_count(ctx: VideoContext) -> int:
    frame_count = int(ctx.metadata.frame_count or 1)
    size = max(1, settings.video_segment_size_frames)
    return max(1, (max(1, frame_count) + size - 1) // size)


async def _task_scope(
    db: AsyncSession, ctx: VideoContext, task: Task | None
) -> Task | None:
    if task is None and ctx.task_id is not None:
        task = await get_core_row(db, task_row, ctx.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
    _assert_context_task_binding(ctx, task)
    return task


def _task_scope_fields(ctx: VideoContext, task: Task | None) -> dict[str, object]:
    if task is None:
        return {
            "task_id": None,
            "project_id": None,
            "expected_batch_id": None,
            "expected_task_status": None,
            "expected_review_round_id": None,
        }
    return {
        "task_id": ctx.task_id,
        "project_id": task.project_id,
        "expected_batch_id": task.batch_id,
        "expected_task_status": task.status,
        "expected_review_round_id": task.review_round_id,
    }


def _raise_control_error(exc: VideoControlRejected) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _segment_from_owner(
    db: AsyncSession, value: dict[str, object]
) -> VideoSegment:
    segment_id = value["id"]
    cached = db.sync_session.identity_map.get(
        identity_key(VideoSegment, ident=segment_id)
    )
    if cached is not None:
        for field, field_value in value.items():
            set_committed_value(cached, field, field_value)
        return cached
    segment = VideoSegment(**value)
    make_transient_to_detached(segment)
    return await db.merge(segment, load=False)


async def ensure_segments(
    db: AsyncSession, ctx: VideoContext, *, task: Task | None = None
) -> list[VideoSegment]:
    task = await _task_scope(db, ctx, task)
    if ctx.actor_id is None:
        raise HTTPException(status_code=401, detail="Video actor is required")
    request = PrepareVideoSegments(
        schema=VIDEO_CONTROL_PROTOCOL,
        operation="prepare_segments",
        task_id=ctx.task_id,
        project_id=task.project_id if task is not None else None,
        actor_id=ctx.actor_id,
        dataset_item_id=ctx.dataset_item_id,
        frame_count=max(1, int(ctx.metadata.frame_count or 1)),
        segment_size_frames=max(1, settings.video_segment_size_frames),
        now=_now(),
    )
    try:
        result = await video_segment_lifecycle_command(db, request)
    except VideoControlRejected as exc:
        _raise_control_error(exc)
    rows = [await _segment_from_owner(db, value) for value in result["segments"]]
    await db.flush()
    return rows


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


async def list_segments(
    db: AsyncSession, ctx: VideoContext, *, task: Task | None = None
) -> VideoSegmentsResponse:
    rows = await ensure_segments(db, ctx, task=task)

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


def _assert_context_task_binding(ctx: VideoContext, task: Task | None) -> None:
    if task is None:
        task_id = None
    else:
        state = inspect(task)
        identity = state.identity
        task_id = state.dict.get("id", identity[0] if identity else None)
    if task_id != ctx.task_id or (task is not None and ctx.task_id is None):
        raise ValueError("video context task binding mismatch")


async def _load_segment_for_update(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    *,
    task: Task | None = None,
) -> VideoSegment:
    row = await lock_video_segment_for_core_read(db, segment_id, ctx.dataset_item_id)
    if row is None:
        await ensure_segments(db, ctx, task=task)
        row = await lock_video_segment_for_core_read(
            db, segment_id, ctx.dataset_item_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Video segment not found")
    return row


async def _mutate_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    task: Task | None,
    request_type: type[
        ClaimVideoSegment
        | HeartbeatVideoSegment
        | ReleaseVideoSegment
        | SubmitVideoSegment
        | ReopenVideoSegment
        | UnassignVideoSegment
    ],
    operation: str,
    now: datetime | None = None,
    lock_ttl_seconds: int | None = None,
    privileged: bool | None = None,
) -> tuple[VideoSegment, bool]:
    task = await _task_scope(db, ctx, task)
    payload: dict[str, object] = {
        "schema": VIDEO_CONTROL_PROTOCOL,
        "operation": operation,
        **_task_scope_fields(ctx, task),
        "actor_id": actor_id,
        "dataset_item_id": ctx.dataset_item_id,
        "segment_id": segment_id,
    }
    if now is not None:
        payload["now"] = now
    if lock_ttl_seconds is not None:
        payload["lock_ttl_seconds"] = lock_ttl_seconds
    if privileged is not None:
        payload["privileged"] = privileged
    request = request_type(**payload)
    try:
        result = await video_segment_lifecycle_command(db, request)
    except VideoControlRejected as exc:
        _raise_control_error(exc)
    if result["segment"] is None:
        await ensure_segments(db, ctx, task=task)
        try:
            result = await video_segment_lifecycle_command(db, request)
        except VideoControlRejected as exc:
            _raise_control_error(exc)
    if result["segment"] is None:
        raise HTTPException(status_code=404, detail="Video segment not found")
    row = await _segment_from_owner(db, result["segment"])
    return row, result["changed"]


async def claim_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
    task: Task | None = None,
) -> VideoSegmentOut:
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    now = _now()
    row, _ = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=user.id,
        task=task,
        request_type=ClaimVideoSegment,
        operation="claim_segment",
        now=now,
        lock_ttl_seconds=settings.video_segment_lock_ttl_seconds,
        privileged=privileged,
    )
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def heartbeat_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
    task: Task | None = None,
) -> VideoSegmentOut:
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    now = _now()
    row, _ = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=user.id,
        task=task,
        request_type=HeartbeatVideoSegment,
        operation="heartbeat_segment",
        now=now,
        lock_ttl_seconds=settings.video_segment_lock_ttl_seconds,
        privileged=privileged,
    )
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)


async def release_segment(
    db: AsyncSession,
    ctx: VideoContext,
    segment_id: uuid.UUID,
    user: User | IdentityActorFacts,
    *,
    privileged: bool,
    task: Task | None = None,
) -> VideoSegmentOut:
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    row, _ = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=user.id,
        task=task,
        request_type=ReleaseVideoSegment,
        operation="release_segment",
        now=_now(),
        privileged=privileged,
    )
    if row.status != "completed":
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
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    now = _now()
    row, changed = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=user.id,
        task=task,
        request_type=SubmitVideoSegment,
        operation="submit_segment",
        now=now,
        privileged=privileged,
    )
    if not changed:
        return await _segment_out_for_context(db, ctx, row)
    if task is not None:
        # A2 · every successful segment submitter is a contributor even when the
        # segment kept no surviving annotation. This uses the actual actor, not
        # the shared dataset-item segment assignee.
        from app.services.annotation_evidence import record_annotation_actor

        await record_annotation_actor(db, task, user.id)
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
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    if actor_id is None:
        actor_id = ctx.actor_id
    if actor_id is None:
        raise ValueError("Reopening a video segment requires its actor")
    row, _ = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=actor_id,
        task=task,
        request_type=ReopenVideoSegment,
        operation="reopen_segment",
    )
    if task is not None:
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
    *,
    task: Task | None = None,
    actor_id: uuid.UUID | None = None,
) -> VideoSegmentOut:
    if actor_id is None:
        actor_id = ctx.actor_id
    if actor_id is None:
        raise ValueError("Unassigning a video segment requires its actor")
    await _load_segment_for_update(db, ctx, segment_id, task=task)
    row, _ = await _mutate_segment(
        db,
        ctx,
        segment_id,
        actor_id=actor_id,
        task=task,
        request_type=UnassignVideoSegment,
        operation="unassign_segment",
    )
    await db.flush()
    return await _segment_out_for_context(db, ctx, row)
