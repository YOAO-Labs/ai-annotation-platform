from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks import _assert_task_visible
from app.api.v1.tasks._shared import (
    _assert_review_adjustment_evidence,
    _resolve_task_access,
    assert_annotation_write_allowed,
)
from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.db.models.video_tracker_job import (
    VideoTrackerJob,
    VideoTrackerJobStatus,
)
from app.deps import (
    get_current_user,
    get_db,
    require_roles,
    require_scopes,
    require_active_task_actor,
)
from app.schemas.video_tracker_job import (
    TrackerJobStatus,
    VideoTrackerDecisionRequest,
    VideoTrackerJobOut,
)
from app.services.audit import AuditAction, AuditService
from app.services.project_access import ProjectAccess
from app.services.raster_mask_storage import load_coco_rle
from app.services.video_tracking.jobs import (
    accept_tracker_job,
    cancel_tracker_job,
    decide_tracker_job,
    discard_tracker_job,
    get_tracker_job,
    tracker_job_out,
    tracker_job_preview,
)
from app.observability.metrics import observe_mask_ai_phase, record_mask_ai_operation

router = APIRouter(dependencies=[Depends(require_active_task_actor)])


class VideoTrackerJobListItem(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str | None = None
    project_display_id: str | None = None
    dataset_item_id: uuid.UUID
    annotation_id: uuid.UUID | None = None
    segment_id: uuid.UUID | None = None
    created_by: uuid.UUID | None = None
    status: TrackerJobStatus
    model_key: str
    direction: str
    from_frame: int
    to_frame: int
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class VideoTrackerJobCounts(BaseModel):
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    # v0.21.28 · 候选/接受流状态计数。
    pending_review: int = 0
    partially_reviewed: int = 0
    accepted: int = 0
    discarded: int = 0


class VideoTrackerJobsResponse(BaseModel):
    items: list[VideoTrackerJobListItem]
    next_cursor: str | None = None
    counts: VideoTrackerJobCounts


class VideoTrackerPreviewResult(BaseModel):
    """候选预览的逐帧结果。形状与 video_tracker_runner._serialize_results 一致
    (TrackerFrameResult → dict)。必需/默认刻意对齐 _deserialize_results 的容错读取
    (frame_index 必需, 其余可缺省), 避免 typed 响应对部分 staged 行过严而 500。"""

    frame_index: int
    geometry: dict | None = None
    confidence: float | None = None
    outside: bool = False
    instance_id: str | None = None
    primary: bool = False
    candidate_key: str
    geometry_digest: str
    source_annotation_id: uuid.UUID | None = None
    target_annotation_id: uuid.UUID | None = None
    manual_protected: bool = False


class VideoTrackerJobPreview(BaseModel):
    """v0.21.28 · 候选预览: job 暂存的逐帧结果, 供前端在接受前渲染候选叠加。"""

    job_id: uuid.UUID
    status: TrackerJobStatus
    annotation_id: uuid.UUID | None = None
    job_kind: Literal["tracking", "correction"] = "tracking"
    correction_frame: int | None = None
    direction: Literal["forward", "backward", "bidirectional"] | None = None
    from_frame: int
    to_frame: int
    fallback_reason: str | None = None
    seed_mode: Literal["native_mask", "bbox"] | None = None
    protect_manual: bool = False
    results: list[VideoTrackerPreviewResult] = Field(default_factory=list)
    grid_step: int = 1
    output_geometry: str = "bbox"
    job_revision: int = 1
    expected_source_versions: dict[uuid.UUID, int] = Field(default_factory=dict)
    candidate_total: int = 0
    candidate_pending: int = 0
    candidate_accepted: int = 0
    candidate_rejected: int = 0


def _encode_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    payload = {"c": created_at.isoformat(), "i": str(job_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(payload["c"]), uuid.UUID(payload["i"])
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}")


async def _load_visible_job_task(
    db: AsyncSession, job_id: uuid.UUID, user: User
) -> tuple[Task, VideoTrackerJobOut]:
    task, row = await _load_visible_job_task_row(db, job_id, user)
    return task, tracker_job_out(row)


async def _load_visible_job_task_row(
    db: AsyncSession, job_id: uuid.UUID, user: User
) -> tuple[Task, VideoTrackerJob]:
    row = await get_tracker_job(db, job_id)
    task = await db.get(Task, row.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    await _assert_task_visible(db, task, user)
    return task, row


async def _lock_visible_job_task_row(
    db: AsyncSession,
    job_id: uuid.UUID,
    user: User,
    *,
    require_annotation_write: bool = True,
) -> tuple[Task, VideoTrackerJob, ProjectAccess]:
    """Lock and refresh the job's task at the mutation boundary.

    Account-first, then the membership ``FOR SHARE``, then the refreshed task row
    ``NOWAIT``: a busy resource returns a retryable 409 instead of blocking, and
    the route preflight no longer relies on an unlocked ``db.get`` snapshot.

    ``require_annotation_write=False`` (cancellation) skips the annotation-write
    and review-evidence guards: cancelling obsolete compute only needs active
    project visibility plus job ownership, which the caller checks separately.
    A member whose project role changed can still cancel their own job even
    when they can no longer apply annotation results.
    """

    from app.services.task_lock import assert_task_user_active

    row = await get_tracker_job(db, job_id)
    if not await assert_task_user_active(db, user.id):
        raise HTTPException(
            status_code=401,
            detail="账号已停用，请联系管理员",
            headers={"WWW-Authenticate": "Bearer"},
        )
    task = await db.get(Task, row.task_id, populate_existing=True)
    if task is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    access = await _resolve_task_access(db, task, user, lock_membership=True)
    task = await _lock_task_nowait(db, row.task_id)
    if require_annotation_write:
        assert_annotation_write_allowed(task, access)
        await _assert_review_adjustment_evidence(db, task, user, access)
    await _assert_task_visible(db, task, user, access=access)
    return task, row, access


async def _lock_task_nowait(db: AsyncSession, task_id: uuid.UUID) -> Task:
    try:
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail={"reason": "task_busy"}
            ) from exc
        raise
    if task is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    return task


async def _assert_can_cancel(
    db: AsyncSession,
    task: Task,
    body: VideoTrackerJob | VideoTrackerJobOut,
    user: User,
    access: ProjectAccess,
) -> None:
    if body.created_by == user.id or access.is_manager:
        return
    raise HTTPException(
        status_code=403, detail="Video tracker job belongs to another user"
    )


async def _assert_can_decide(
    db: AsyncSession,
    task: Task,
    body: VideoTrackerJob,
    user: User,
    access: ProjectAccess,
) -> None:
    if task.status == "completed":
        raise HTTPException(
            status_code=409,
            detail={"reason": "task_locked", "status": task.status},
        )
    if access.is_manager:
        return
    if task.status == "review":
        if task.reviewer_id == user.id and task.reviewer_claimed_at is not None:
            return
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "task_review_not_claimed_by_user",
                "reviewer_id": str(task.reviewer_id) if task.reviewer_id else None,
            },
        )
    if body.created_by == user.id:
        return
    raise HTTPException(
        status_code=403, detail="Video tracker job belongs to another user"
    )


@router.get("", response_model=VideoTrackerJobsResponse)
async def list_video_tracker_jobs(
    project_id: uuid.UUID | None = Query(default=None),
    status: VideoTrackerJobStatus | None = Query(default=None),
    model_key: str | None = Query(default=None, max_length=80),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(
        require_roles(UserRole.PROJECT_ADMIN, UserRole.SUPER_ADMIN)
    ),
) -> VideoTrackerJobsResponse:
    """列 video_tracker_jobs 时间线 (created_at DESC + cursor 分页) + 按 status 聚合计数.

    project_id 过滤需 join Task (VideoTrackerJob 无 project_id 列)。
    counts 在当前 project_id/model_key 过滤下 (忽略 status/cursor) 分组统计。
    """

    # 当前过滤条件 (project/model_key), 不含 status/cursor — counts 与列表共用.
    base_conds = []
    if model_key is not None:
        base_conds.append(VideoTrackerJob.model_key == model_key)
    needs_task_join = project_id is not None
    if project_id is not None:
        base_conds.append(Task.project_id == project_id)
    if current_admin.role == UserRole.PROJECT_ADMIN:
        # 项目管理员的全局列表与计数只能覆盖自己拥有的项目；显式传入他人项目 id
        # 也只会得到空交集，不能借列表或 counts 枚举跨项目任务。
        base_conds.append(
            Task.project_id.in_(
                select(Project.id).where(Project.owner_id == current_admin.id)
            )
        )
        needs_task_join = True

    # 聚合计数: select status, count(*) group by status (带 project/model_key 过滤).
    counts_stmt = select(VideoTrackerJob.status, func.count()).group_by(
        VideoTrackerJob.status
    )
    if needs_task_join:
        counts_stmt = counts_stmt.join(Task, VideoTrackerJob.task_id == Task.id)
    if base_conds:
        counts_stmt = counts_stmt.where(and_(*base_conds))
    cres = await db.execute(counts_stmt)
    counts = VideoTrackerJobCounts()
    for st, cnt in cres.all():
        if hasattr(counts, st):
            setattr(counts, st, cnt)

    # 列表查询: base 过滤 + status + cursor 复合分页.
    list_conds = list(base_conds)
    if status is not None:
        list_conds.append(VideoTrackerJob.status == status.value)
    if cursor:
        cursor_created, cursor_id = _decode_cursor(cursor)
        list_conds.append(
            or_(
                VideoTrackerJob.created_at < cursor_created,
                and_(
                    VideoTrackerJob.created_at == cursor_created,
                    VideoTrackerJob.id < cursor_id,
                ),
            )
        )

    stmt = select(VideoTrackerJob, Task.project_id).join(
        Task, VideoTrackerJob.task_id == Task.id
    )
    if list_conds:
        stmt = stmt.where(and_(*list_conds))
    stmt = stmt.order_by(
        VideoTrackerJob.created_at.desc(), VideoTrackerJob.id.desc()
    ).limit(limit + 1)

    res = await db.execute(stmt)
    rows = res.all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    if not rows:
        return VideoTrackerJobsResponse(items=[], next_cursor=None, counts=counts)

    project_ids = list({pid for _, pid in rows})
    pres = await db.execute(select(Project).where(Project.id.in_(project_ids)))
    projects_by_id = {p.id: p for p in pres.scalars().all()}

    items: list[VideoTrackerJobListItem] = []
    for job, pid in rows:
        proj = projects_by_id.get(pid)
        items.append(
            VideoTrackerJobListItem(
                id=job.id,
                task_id=job.task_id,
                project_id=pid,
                project_name=proj.name if proj else None,
                project_display_id=getattr(proj, "display_id", None) if proj else None,
                dataset_item_id=job.dataset_item_id,
                annotation_id=job.annotation_id,
                segment_id=job.segment_id,
                created_by=job.created_by,
                status=job.status,
                model_key=job.model_key,
                direction=job.direction,
                from_frame=job.from_frame,
                to_frame=job.to_frame,
                error_message=job.error_message,
                started_at=job.started_at,
                completed_at=job.completed_at,
                created_at=job.created_at,
            )
        )

    last_job = rows[-1][0]
    next_cursor = _encode_cursor(last_job.created_at, last_job.id) if has_more else None

    return VideoTrackerJobsResponse(items=items, next_cursor=next_cursor, counts=counts)


@router.get("/{job_id}", response_model=VideoTrackerJobOut)
async def get_video_tracker_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _, body = await _load_visible_job_task(db, job_id, current_user)
    return body


@router.delete(
    "/{job_id}",
    response_model=VideoTrackerJobOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def cancel_video_tracker_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    task, row, access = await _lock_visible_job_task_row(
        db, job_id, current_user, require_annotation_write=False
    )
    await _assert_can_cancel(db, task, row, current_user, access)
    job_kind = row.job_kind
    status_before = row.status
    commit_started = time.monotonic()
    try:
        body = await cancel_tracker_job(db, job_id)
    except HTTPException as exc:
        if job_kind == "correction":
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
                decision="cancel",
                outcome=outcome,
            )
        raise
    if status_before != VideoTrackerJobStatus.CANCELLED.value:
        await AuditService.log(
            db,
            actor=current_user,
            action=(
                AuditAction.VIDEO_CORRECTION_JOB_CANCEL
                if job_kind == "correction"
                else AuditAction.VIDEO_TRACKER_JOB_CANCEL
            ),
            target_type="video_tracker_job",
            target_id=job_id,
            request=request,
            status_code=200,
            detail={
                "task_id": str(task.id),
                "status_before": status_before,
                "status": body.status,
                "job_kind": job_kind,
                "manual_keyframe_preserved": job_kind == "correction",
                "staged_discarded": job_kind == "correction",
            },
        )
    try:
        await db.commit()
    except Exception:
        if job_kind == "correction":
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
                decision="cancel",
                outcome="error",
            )
        raise
    if job_kind == "correction":
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
            decision="cancel",
            outcome="success",
        )
    return body


@router.post(
    "/{job_id}/accept",
    response_model=VideoTrackerJobOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def accept_video_tracker_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.21.28 · 接受候选: 把 job 暂存结果落库 (主实例回填源轨迹 + 新轨迹建), status=ACCEPTED。"""
    task, row, access = await _lock_visible_job_task_row(db, job_id, current_user)
    await _assert_can_cancel(db, task, row, current_user, access)
    if row.job_kind == "correction":
        raise HTTPException(
            status_code=409,
            detail={"reason": "correction_requires_local_decision"},
        )
    body = await accept_tracker_job(
        db,
        job_id,
        actor_id=current_user.id,
        privileged=access.is_manager,
    )
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_TRACKER_JOB_ACCEPT,
        target_type="video_tracker_job",
        target_id=job_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "status": body.status},
    )
    await db.commit()
    return body


@router.post(
    "/{job_id}/discard",
    response_model=VideoTrackerJobOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def discard_video_tracker_job(
    job_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.21.28 · 丢弃候选: status=DISCARDED, 清 staged_result, annotation 零改动。"""
    task, row, access = await _lock_visible_job_task_row(db, job_id, current_user)
    await _assert_can_cancel(db, task, row, current_user, access)
    if row.job_kind == "correction":
        raise HTTPException(
            status_code=409,
            detail={"reason": "correction_requires_local_decision"},
        )
    body = await discard_tracker_job(db, job_id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.VIDEO_TRACKER_JOB_DISCARD,
        target_type="video_tracker_job",
        target_id=job_id,
        request=request,
        status_code=200,
        detail={"task_id": str(task.id), "status": body.status},
    )
    await db.commit()
    return body


@router.post(
    "/{job_id}/decisions",
    response_model=VideoTrackerJobOut,
    dependencies=[Depends(require_scopes("annotations:write"))],
)
async def decide_video_tracker_candidates(
    job_id: uuid.UUID,
    body: VideoTrackerDecisionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept or reject an instance/window or QC issue region selector."""

    task, visible, access = await _lock_visible_job_task_row(db, job_id, current_user)
    await _assert_can_decide(db, task, visible, current_user, access)
    staged = visible.staged_result or {}
    output_geometry = str(staged.get("output_geometry") or "unknown")
    observe_decision = visible.job_kind == "correction" or output_geometry == "mask"
    commit_started = time.monotonic()
    try:
        result = await decide_tracker_job(
            db,
            job_id,
            body,
            actor_id=current_user.id,
            privileged=access.is_manager,
        )
    except HTTPException as exc:
        if observe_decision:
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
                output_geometry=output_geometry,
                decision=body.decision,
                outcome=outcome,
            )
        raise
    last_decision = ((result.prompt or {}).get("review_state") or {}).get(
        "last_decision"
    ) or {}
    correction = (result.prompt or {}).get("correction") or {}
    if not result.review_replayed:
        await AuditService.log(
            db,
            actor=current_user,
            action=AuditAction.VIDEO_TRACKER_JOB_DECISION,
            target_type="video_tracker_job",
            target_id=job_id,
            request=request,
            status_code=200,
            detail={
                "task_id": str(task.id),
                "status": result.status,
                "job_kind": result.job_kind,
                "track_id": result.track_id_snapshot,
                "correction_frame": result.correction_frame,
                "source_version": correction.get("source_version"),
                "result_versions": (result.prompt or {}).get(
                    "expected_source_versions"
                ),
                "seed_mode": correction.get("seed_mode"),
                "fallback_reason": correction.get("fallback_reason"),
                **last_decision,
            },
        )
    # The review mutation and its mandatory audit row share this transaction.
    try:
        await db.commit()
    except Exception:
        if observe_decision:
            observe_mask_ai_phase(
                operation="correction",
                phase="commit",
                outcome="error",
                duration_seconds=time.monotonic() - commit_started,
            )
            record_mask_ai_operation(
                operation="correction",
                prompt_family="correction_frame",
                output_geometry=output_geometry,
                candidate_count=int(last_decision.get("candidate_count") or 0),
                decision=body.decision,
                fallback_reason=correction.get("fallback_reason"),
                outcome="error",
            )
        raise
    if observe_decision:
        observe_mask_ai_phase(
            operation="correction",
            phase="commit",
            outcome="success",
            duration_seconds=time.monotonic() - commit_started,
        )
        record_mask_ai_operation(
            operation="correction",
            prompt_family="correction_frame",
            output_geometry=output_geometry,
            candidate_count=int(last_decision.get("candidate_count") or 0),
            decision=body.decision,
            fallback_reason=correction.get("fallback_reason"),
            outcome="success",
        )
    return result


@router.get("/{job_id}/preview", response_model=VideoTrackerJobPreview)
async def get_video_tracker_job_preview(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """v0.21.28 · 候选预览: 返回 job 暂存的逐帧结果 (接受前渲染候选叠加用)。无 staged → 空。"""
    row = await get_tracker_job(db, job_id)
    task = await db.get(Task, row.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    await _assert_task_visible(db, task, current_user)
    return VideoTrackerJobPreview.model_validate(await tracker_job_preview(db, row))


@router.get(
    "/{job_id}/mask-content/{sha256}",
    dependencies=[Depends(require_scopes("annotations:read"))],
)
async def get_video_tracker_mask_content(
    job_id: uuid.UUID,
    sha256: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    row = await get_tracker_job(db, job_id)
    task = await db.get(Task, row.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Video tracker job not found")
    await _assert_task_visible(db, task, current_user)
    reference = next(
        (
            (result.get("geometry") or {}).get("mask")
            for result in ((row.staged_result or {}).get("results") or [])
            if ((result.get("geometry") or {}).get("mask") or {}).get("sha256")
            == sha256
        ),
        None,
    )
    if reference is None:
        raise HTTPException(status_code=404, detail="mask candidate not found")
    try:
        payload = await load_coco_rle(reference)
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail=f"mask object is invalid: {exc}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"reason": "mask_storage_unavailable", "retryable": True},
        ) from exc
    return JSONResponse(
        payload,
        headers={"Cache-Control": "private, max-age=300", "ETag": f'"{sha256}"'},
    )
