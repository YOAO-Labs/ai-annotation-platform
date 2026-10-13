from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from app.deps import (
    get_db,
    require_roles,
)
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.dataset import DatasetItem
from app.db.models.task_batch import TaskBatch
from app.services.storage import storage_service
from app.services.scheduler import batch_visibility_clause
from app.services.project_access import platform_role_is_manager
from app.services.project_aggregates import (
    DASHBOARD_PLATFORM_ROLES,
    REVIEWER_ROLE,
    project_scope_clause,
)
from app.db.enums import UserRole, TaskStatus
from app.schemas.dashboard import (
    ReviewerDashboardStats,
    ReviewTaskItem,
    ReviewingBatchItem,
    RecentReviewItem,
    ReviewerMiniStats,
)
from app.services.user_brief import resolve_briefs_with_project_role

router = APIRouter()


def _visible_project_clause(user: User):
    """Reviewer scope: reviewer memberships plus legitimately managed projects.

    A super administrator sees every project.  An employee who reviews in B but
    only annotates in A therefore sees B's review queue and not A's.  A missing
    reviewer membership fails closed (no legacy global-role fallback).
    """
    return project_scope_clause(user, Project.id, project_roles=(REVIEWER_ROLE,))


def _reviewable_claim_clause(user: User):
    """Actual actionable-review visibility plus the "do not steal a claim" rule.

    Project authority is applied separately by ``_visible_project_clause``.  A
    task is actionable only when its batch is in a reviewer-visible state (or it
    is a collaborative video task), an unbatched task is only visible to its
    assigned reviewer, and it is not already claimed by someone else.  The owner
    bypass requires an administrative platform role, so an anomalous employee
    owner does not gain management scope.
    """
    if user.role == UserRole.SUPER_ADMIN:
        return None
    manager_owner = and_(
        platform_role_is_manager(user.role), Project.owner_id == user.id
    )
    visible_batch = Task.batch_id.in_(
        select(TaskBatch.id)
        .where(batch_visibility_clause(user, project_role=REVIEWER_ROLE))
        .correlate(None)
    )
    collaborative_video = and_(
        Task.file_type == "video",
        Project.video_collaboration["enabled"].as_boolean().is_(True),
    )
    unbatched_assigned = and_(Task.batch_id.is_(None), Task.reviewer_id == user.id)
    return and_(
        or_(manager_owner, unbatched_assigned, visible_batch, collaborative_video),
        or_(
            manager_owner,
            Task.reviewer_claimed_at.is_(None),
            Task.reviewer_id == user.id,
        ),
    )


@router.get("/reviewer", response_model=ReviewerDashboardStats)
async def reviewer_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*DASHBOARD_PLATFORM_ROLES)),
):
    project_scope = _visible_project_clause(current_user)
    claim_scope = _reviewable_claim_clause(current_user)

    pending_result = await db.execute(
        select(func.count())
        .select_from(Task)
        .join(Project, Task.project_id == Project.id)
        .where(Task.status == TaskStatus.REVIEW)
        .where(project_scope if project_scope is not None else True)
        .where(claim_scope if claim_scope is not None else True)
    )
    pending_review_count = pending_result.scalar() or 0

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today_reviewed_result = await db.execute(
        select(func.count())
        .select_from(Task)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.reviewer_id == current_user.id,
            Task.reviewed_at.is_not(None),
            Task.reviewed_at >= today_start,
            project_scope if project_scope is not None else True,
        )
    )
    today_reviewed = today_reviewed_result.scalar() or 0

    total_completed_result = await db.execute(
        select(func.count())
        .select_from(Task)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.reviewer_id == current_user.id,
            Task.status == TaskStatus.COMPLETED,
            Task.reviewed_at.is_not(None),
            project_scope if project_scope is not None else True,
        )
    )
    total_completed = total_completed_result.scalar() or 0

    total_rejected = int(
        await db.scalar(
            select(func.count())
            .select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.reviewer_id == current_user.id,
                Task.reviewed_at.is_not(None),
                Task.status == "rejected",
                project_scope if project_scope is not None else True,
            )
        )
        or 0
    )
    total_all_reviewed = total_completed + total_rejected
    approval_rate = (
        (total_completed / total_all_reviewed * 100) if total_all_reviewed > 0 else 0.0
    )

    # v0.6.6 · 24h 滚动通过率：只统计当前审核员实际处理且在可见项目内的任务。
    cutoff_24h = now - timedelta(hours=24)
    rate_24h_result = await db.execute(
        select(
            func.count().filter(Task.status == TaskStatus.COMPLETED).label("approve_n"),
            func.count().filter(Task.status == "rejected").label("reject_n"),
        )
        .select_from(Task)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.reviewer_id == current_user.id,
            Task.reviewed_at.is_not(None),
            Task.reviewed_at >= cutoff_24h,
            project_scope if project_scope is not None else True,
        )
    )
    row = rate_24h_result.one()
    approve_n = row.approve_n or 0
    reject_n = row.reject_n or 0
    denom_24h = approve_n + reject_n
    approval_rate_24h = (approve_n / denom_24h * 100) if denom_24h > 0 else 0.0

    pending_tasks_result = await db.execute(
        select(Task, Project.name)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.status == TaskStatus.REVIEW,
            project_scope if project_scope is not None else True,
            claim_scope if claim_scope is not None else True,
        )
        .order_by(Task.updated_at.desc())
        .limit(50)
    )
    pending_tasks = [
        ReviewTaskItem(
            task_id=str(t.id),
            task_display_id=t.display_id,
            file_name=t.file_name,
            project_id=str(t.project_id),
            project_name=pname,
            total_annotations=t.total_annotations,
            total_predictions=t.total_predictions,
            updated_at=t.updated_at.isoformat() if t.updated_at else None,
            is_rework=(t.reopened_count or 0) > 0,
        )
        for t, pname in pending_tasks_result.all()
    ]

    # v0.7.0：批次级聚合 — 列出处于 reviewing 状态的批次（reviewer 跨批次审核）。
    # v0.7.1 B-18：扩展为「reviewing 批次 ∪ 任意 review_tasks > 0 的批次」，让单任务级提交质检
    # 也能在 ReviewPage 的批次树里看到，避免 reviewer 找不到入口。
    actionable_review = select(Task.id).where(
        Task.batch_id == TaskBatch.id,
        Task.status == TaskStatus.REVIEW,
        claim_scope if claim_scope is not None else True,
    )
    batch_rows = (
        await db.execute(
            select(TaskBatch, Project.name)
            .join(Project, TaskBatch.project_id == Project.id)
            .where(
                or_(
                    TaskBatch.status == "reviewing",
                    TaskBatch.review_tasks > 0,
                ),
                TaskBatch.status.in_(["active", "annotating", "reviewing"]),
                project_scope if project_scope is not None else True,
                actionable_review.exists(),
            )
            .order_by(Project.name, TaskBatch.updated_at.desc())
            .limit(100)
        )
    ).all()
    # v0.7.2 · 单值语义 — 一 batch 一标注员，直接 IN 查询 user
    project_user_map: dict = {}
    for b, _ in batch_rows:
        if b.annotator_id is not None:
            project_user_map.setdefault(b.project_id, set()).add(b.annotator_id)
    briefs_by_project: dict = {}
    for pid, uids in project_user_map.items():
        briefs_by_project[pid] = await resolve_briefs_with_project_role(db, pid, uids)

    # 批次封面缩略图：每个 batch 取最早一张「有缩略图」的任务作封面（DISTINCT ON）。
    # 缩略图口径与 tasks/_shared.py 一致：dataset 导入的任务缩略图存在 DatasetItem 上，
    # Task.thumbnail_path 为空，故需 LEFT JOIN + COALESCE。
    cover_map: dict = {}
    cover_batch_ids = [b.id for b, _ in batch_rows]
    if cover_batch_ids:
        eff_thumb = func.coalesce(DatasetItem.thumbnail_path, Task.thumbnail_path)
        eff_blur = func.coalesce(DatasetItem.blurhash, Task.blurhash)
        cover_rows = await db.execute(
            select(Task.batch_id, eff_thumb, eff_blur)
            .outerjoin(DatasetItem, DatasetItem.id == Task.dataset_item_id)
            .where(
                Task.batch_id.in_(cover_batch_ids),
                eff_thumb.is_not(None),
            )
            .order_by(Task.batch_id, Task.created_at.asc())
            .distinct(Task.batch_id)
        )
        cover_map = {row[0]: (row[1], row[2]) for row in cover_rows.all()}

    reviewing_batches = []
    for b, pname in batch_rows:
        per_proj = briefs_by_project.get(b.project_id, {})
        annotator = per_proj.get(str(b.annotator_id)) if b.annotator_id else None
        cover_path, cover_blurhash = cover_map.get(b.id, (None, None))
        thumbnail_url: str | None = None
        if cover_path:
            thumb_bucket = storage_service.bucket_for_cache_key(
                cover_path, default=storage_service.bucket
            )
            try:
                thumbnail_url = storage_service.generate_download_url(
                    cover_path, bucket=thumb_bucket
                )
            except Exception:
                thumbnail_url = None
        reviewing_batches.append(
            ReviewingBatchItem(
                batch_id=str(b.id),
                batch_display_id=b.display_id,
                batch_name=b.name,
                project_id=str(b.project_id),
                project_name=pname,
                total_tasks=b.total_tasks,
                review_tasks=b.review_tasks,
                completed_tasks=b.completed_tasks,
                thumbnail_url=thumbnail_url,
                cover_blurhash=cover_blurhash,
                annotator=annotator,
            )
        )

    # v0.8.4 · 效率看板 L2 扩展
    # 平均审核耗时（中位） = reviewed_at - reviewer_claimed_at（自己审过的任务）
    review_duration_row = (
        await db.execute(
            select(
                func.percentile_cont(0.5)
                .within_group(
                    (
                        func.extract(
                            "epoch", Task.reviewed_at - Task.reviewer_claimed_at
                        )
                        * 1000
                    ).asc()
                )
                .label("median_ms")
            )
            .select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.reviewer_id == current_user.id,
                Task.reviewer_claimed_at.isnot(None),
                Task.reviewed_at.isnot(None),
                project_scope if project_scope is not None else True,
            )
        )
    ).first()
    median_review_duration_ms = (
        int(review_duration_row.median_ms)
        if review_duration_row and review_duration_row.median_ms
        else None
    )

    # 二次返修率：自己 approve 的 task（task.reviewer_id == me 且 status==completed）
    # 后又被 reopen（reopened_count > 0）的比例
    reopen_after_row = (
        await db.execute(
            select(
                func.count()
                .filter(Task.status == TaskStatus.COMPLETED)
                .label("approved_n"),
                func.count()
                .filter(
                    Task.status == TaskStatus.COMPLETED,
                    Task.reopened_count > 0,
                )
                .label("reopened_n"),
            )
            .select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.reviewer_id == current_user.id,
                project_scope if project_scope is not None else True,
            )
        )
    ).first()
    approved_n = int(reopen_after_row.approved_n or 0) if reopen_after_row else 0
    reopen_after_approve_rate = (
        round((reopen_after_row.reopened_n or 0) / approved_n * 100, 1)
        if approved_n > 0
        else None
    )

    # 7 日审核数 sparkline
    daily_review_counts: list[int] = []
    for i in range(6, -1, -1):
        ds = today_start - timedelta(days=i)
        de = ds + timedelta(days=1)
        n = (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.reviewer_id == current_user.id,
                    Task.reviewed_at.isnot(None),
                    Task.reviewed_at >= ds,
                    Task.reviewed_at < de,
                    project_scope if project_scope is not None else True,
                )
                .join(Project, Task.project_id == Project.id)
            )
        ).scalar() or 0
        daily_review_counts.append(int(n))

    # 周环比
    week_start_r = today_start - timedelta(days=today_start.weekday())
    last_week_start_r = week_start_r - timedelta(days=7)
    this_week_n = (
        await db.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.reviewer_id == current_user.id,
                Task.reviewed_at.isnot(None),
                Task.reviewed_at >= week_start_r,
                project_scope if project_scope is not None else True,
            )
            .join(Project, Task.project_id == Project.id)
        )
    ).scalar() or 0
    last_week_n = (
        await db.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.reviewer_id == current_user.id,
                Task.reviewed_at.isnot(None),
                Task.reviewed_at >= last_week_start_r,
                Task.reviewed_at < week_start_r,
                project_scope if project_scope is not None else True,
            )
            .join(Project, Task.project_id == Project.id)
        )
    ).scalar() or 0
    weekly_compare_pct_r: float | None
    if last_week_n > 0:
        weekly_compare_pct_r = round((this_week_n - last_week_n) / last_week_n * 100, 1)
    elif this_week_n > 0:
        weekly_compare_pct_r = 100.0
    else:
        weekly_compare_pct_r = None

    return ReviewerDashboardStats(
        pending_review_count=pending_review_count,
        today_reviewed=today_reviewed,
        approval_rate=round(approval_rate, 1),
        approval_rate_24h=round(approval_rate_24h, 1),
        total_reviewed=total_completed,
        pending_tasks=pending_tasks,
        reviewing_batches=reviewing_batches,
        median_review_duration_ms=median_review_duration_ms,
        reopen_after_approve_rate=reopen_after_approve_rate,
        weekly_compare_pct=weekly_compare_pct_r,
        daily_review_counts=daily_review_counts,
    )


@router.get("/reviewer/today-mini", response_model=ReviewerMiniStats)
async def reviewer_today_mini(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*DASHBOARD_PLATFORM_ROLES)),
):
    """v0.8.7 F5.3 · ReviewWorkbench 右侧栏 mini 仪表轻量端点。

    - approved_today / rejected_today: 自己当日 approve/reject 的次数（基于 Task.reviewer_id）
    - avg_review_seconds: 自己当日审过的任务（reviewer_claimed_at → reviewed_at）平均耗时秒数
    20s 自动 refetch（前端 query staleTime）。
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # reject 流程：task.status 落回 in_progress，但 task.reject_reason 有值。
    # approve 流程：task.status = completed。
    # 当日审核耗时按 (reviewed_at - reviewer_claimed_at) 平均。
    counts = (
        await db.execute(
            select(
                func.count()
                .filter(Task.status == TaskStatus.COMPLETED)
                .label("approved"),
                func.count()
                .filter(
                    Task.status == TaskStatus.IN_PROGRESS,
                    Task.reject_reason.isnot(None),
                )
                .label("rejected"),
                func.avg(
                    func.extract("epoch", Task.reviewed_at - Task.reviewer_claimed_at)
                ).label("avg_seconds"),
            )
            .select_from(Task)
            .join(Project, Task.project_id == Project.id)
            .where(
                Task.reviewer_id == current_user.id,
                Task.reviewed_at.isnot(None),
                Task.reviewed_at >= today_start,
                _visible_project_clause(current_user)
                if _visible_project_clause(current_user) is not None
                else True,
            )
        )
    ).first()

    return ReviewerMiniStats(
        approved_today=int(counts.approved or 0) if counts else 0,
        rejected_today=int(counts.rejected or 0) if counts else 0,
        avg_review_seconds=(
            float(counts.avg_seconds)
            if counts and counts.avg_seconds is not None
            else None
        ),
    )


@router.get("/me/recent-reviews", response_model=list[RecentReviewItem])
async def my_recent_reviews(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*DASHBOARD_PLATFORM_ROLES)),
):
    """v0.6.6 · 当前 reviewer 最近审核过的任务（已 approve / reject 落定的）。

    依据 Task.reviewer_id + Task.reviewed_at（v0.6.5 状态机字段）。
    """
    result = await db.execute(
        select(Task, Project.name)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.reviewer_id == current_user.id,
            Task.reviewed_at.isnot(None),
            _visible_project_clause(current_user)
            if _visible_project_clause(current_user) is not None
            else True,
        )
        .order_by(Task.reviewed_at.desc())
        .limit(limit)
    )
    return [
        RecentReviewItem(
            task_id=str(t.id),
            task_display_id=t.display_id,
            file_name=t.file_name,
            project_id=str(t.project_id),
            project_name=pname,
            status=t.status,
            reviewed_at=t.reviewed_at.isoformat() if t.reviewed_at else None,
        )
        for t, pname in result.all()
    ]
