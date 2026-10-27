import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date, or_, and_
from sqlalchemy.orm import aliased
from app.deps import (
    get_current_user,
    get_db,
    require_roles,
)
from app.db.models.project_member import ProjectMember
from app.db.models.dataset import DatasetItem
from aap_assets_media.contract import ReadSourceRequest
from app.services.labeling_discussion_relations import task_event_facts
from app.db.enums import UserRole, TaskStatus, ProjectRole
from app.schemas.dashboard import (
    AnnotatorDashboardStats,
    MyBatchItem,
    MyPerformance,
    OnboardingProjectSummary,
)
from app.services.scheduler import task_visibility_clause
from app.services.project_aggregates import (
    ANNOTATOR_ROLE,
    DASHBOARD_PLATFORM_ROLES,
    project_scope_clause,
)
from app.services.workspace_read_relations import project_search_scope
from app.services.user_brief import resolve_briefs_with_project_role
from app.services.video_poster_references import video_poster_urls
from app.services.dashboard_stats import (
    _class_distribution,
    _first_pass_yield,
    _period_window,
    _reject_reason_breakdown,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_read_relations import account_profiles
from app.services.workspace_read_relations import project_member_facts

from app.services.labeling_core_read_relations import (
    annotation_row,
    batch_row,
    task_row,
)

router = APIRouter()


@router.get(
    "/annotator/projects/{project_id}/onboarding",
    response_model=OnboardingProjectSummary,
)
async def annotator_project_onboarding(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(
        require_roles(*DASHBOARD_PLATFORM_ROLES)
    ),
):
    """Return durable checklist signals for the current user and project.

    Opening is based on task events, annotation completion is based on active
    annotations owned by the user, and review completion is based on the
    assignee's task review fields.  Client-side remembered task state is only a
    supplemental signal for the currently open browser tab.
    """
    from app.services.project_access import resolve_project_access_by_id

    project, access = await resolve_project_access_by_id(
        db, actor_id=current_user.id, project_id=project_id
    )
    assignment_scope = True
    if not access.is_manager:
        # Pass the resolved project role: the task-visibility predicate fails
        # closed without one, so omitting it would hide every assigned task.
        assignment_scope = or_(
            task_row.batch_id.in_(
                select(batch_row.id)
                .where(
                    task_visibility_clause(
                        current_user.id,
                        project_role=access.project_role,
                        task_model=task_row,
                        batch_model=batch_row,
                    )
                )
                .correlate_except(batch_row)
            ),
            and_(
                task_row.file_type == "video",
                bool((project.video_collaboration or {}).get("enabled")),
            ),
        )

    assigned_task_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(task_row)
                .where(
                    task_row.project_id == project_id,
                    task_row.assignee_id == current_user.id,
                    task_row.status != "uploading",
                    assignment_scope,
                )
            )
        ).scalar()
        or 0
    )
    opened_task_count = int(
        (
            await db.execute(
                select(func.count(func.distinct(task_event_facts.c.task_id)))
                .select_from(task_event_facts)
                .join(task_row, task_row.id == task_event_facts.c.task_id)
                .where(
                    task_row.project_id == project_id,
                    task_event_facts.c.user_id == current_user.id,
                    task_event_facts.c.project_id == project_id,
                    task_event_facts.c.kind == "annotate",
                )
            )
        ).scalar()
        or 0
    )
    saved_annotation_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(annotation_row)
                .join(task_row, task_row.id == annotation_row.task_id)
                .where(
                    task_row.project_id == project_id,
                    annotation_row.user_id == current_user.id,
                    annotation_row.is_active.is_(True),
                    annotation_row.was_cancelled.is_(False),
                )
            )
        ).scalar()
        or 0
    )
    reviewed_task_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(task_row)
                .where(
                    task_row.project_id == project_id,
                    task_row.assignee_id == current_user.id,
                    task_row.reviewed_at.is_not(None),
                    task_row.status.in_([TaskStatus.COMPLETED, "rejected"]),
                )
            )
        ).scalar()
        or 0
    )
    reviewed_task = await db.scalar(
        select(task_row)
        .where(
            task_row.project_id == project_id,
            task_row.assignee_id == current_user.id,
            task_row.reviewed_at.is_not(None),
            task_row.status.in_([TaskStatus.COMPLETED, "rejected"]),
        )
        .order_by(task_row.reviewed_at.desc(), task_row.id.desc())
        .limit(1)
    )
    return OnboardingProjectSummary(
        project_id=project_id,
        assigned_task_count=assigned_task_count,
        opened_task_count=opened_task_count,
        saved_annotation_count=saved_annotation_count,
        reviewed_task_count=reviewed_task_count,
        reviewed_task_id=reviewed_task.id if reviewed_task else None,
        reviewed_task_display_id=reviewed_task.display_id if reviewed_task else None,
        reviewed_task_status=reviewed_task.status if reviewed_task else None,
        reviewed_task_reason=(
            reviewed_task.reject_reason
            if reviewed_task and reviewed_task.status == "rejected"
            else None
        ),
    )


@router.get("/annotator", response_model=AnnotatorDashboardStats)
async def annotator_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(
        require_roles(*DASHBOARD_PLATFORM_ROLES)
    ),
):
    # Current annotation work follows the *project* annotator role.  An
    # employee who is only a reviewer elsewhere must not see that project's
    # task queue; a manager keeps the existing owner/super-admin override.
    # Historical personal metrics below stay account-scoped.
    annotator_work_scope = project_scope_clause(
        current_user, task_row.project_id, project_roles=(ANNOTATOR_ROLE,)
    )
    assigned_filters = [
        task_row.assignee_id == current_user.id,
        task_row.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
    ]
    rejected_filters = [
        task_row.assignee_id == current_user.id,
        task_row.status == "rejected",
    ]
    if annotator_work_scope is not None:
        assigned_filters.append(annotator_work_scope)
        rejected_filters.append(annotator_work_scope)

    assigned_result = await db.execute(
        select(func.count()).select_from(task_row).where(*assigned_filters)
    )
    assigned_tasks = assigned_result.scalar() or 0

    rejected_tasks_result = await db.execute(
        select(func.count()).select_from(task_row).where(*rejected_filters)
    )
    rejected_tasks_count = rejected_tasks_result.scalar() or 0

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    today_completed_result = await db.execute(
        select(func.count())
        .select_from(annotation_row)
        .where(
            annotation_row.user_id == current_user.id,
            annotation_row.is_active.is_(True),
            annotation_row.created_at >= today_start,
        )
    )
    today_completed = today_completed_result.scalar() or 0

    weekly_completed_result = await db.execute(
        select(func.count())
        .select_from(annotation_row)
        .where(
            annotation_row.user_id == current_user.id,
            annotation_row.is_active.is_(True),
            annotation_row.created_at >= week_start,
        )
    )
    weekly_completed = weekly_completed_result.scalar() or 0

    total_completed_result = await db.execute(
        select(func.count())
        .select_from(annotation_row)
        .where(
            annotation_row.user_id == current_user.id,
            annotation_row.is_active.is_(True),
        )
    )
    total_completed = total_completed_result.scalar() or 0

    ai_derived_result = await db.execute(
        select(func.count())
        .select_from(annotation_row)
        .where(
            annotation_row.user_id == current_user.id,
            annotation_row.is_active.is_(True),
            annotation_row.parent_prediction_id.isnot(None),
        )
    )
    ai_derived = ai_derived_result.scalar() or 0
    personal_accuracy = (
        ((total_completed - ai_derived) / total_completed * 100)
        if total_completed > 0
        else 100.0
    )

    daily_counts = []
    for i in range(6, -1, -1):
        day_start = today_start - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        day_result = await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == current_user.id,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= day_start,
                annotation_row.created_at < day_end,
            )
        )
        daily_counts.append(day_result.scalar() or 0)

    # v0.8.4 · 效率看板 L2 字段：基于 task_row.assigned_at / submitted_at / reopened_count
    # 中位单题耗时（仅本人 + assigned_at IS NOT NULL + submitted_at 在过去 30d）
    cutoff_30d = now - timedelta(days=30)
    duration_rows = (
        await db.execute(
            select(
                func.percentile_cont(0.5)
                .within_group(
                    (
                        func.extract(
                            "epoch", task_row.submitted_at - task_row.assigned_at
                        )
                        * 1000
                    ).asc()
                )
                .label("median_ms")
            ).where(
                task_row.assignee_id == current_user.id,
                task_row.assigned_at.isnot(None),
                task_row.submitted_at.isnot(None),
                task_row.submitted_at >= cutoff_30d,
            )
        )
    ).first()
    median_duration_ms = (
        int(duration_rows.median_ms)
        if duration_rows and duration_rows.median_ms
        else None
    )

    # 退回率 / 重审次数：仅本人，submitted_at 不为空（已提交过的任务）
    reopen_row = (
        await db.execute(
            select(
                func.count().label("submitted_n"),
                func.count().filter(task_row.reopened_count > 0).label("reopened_n"),
                func.coalesce(func.avg(task_row.reopened_count), 0.0).label(
                    "reopen_avg"
                ),
            ).where(
                task_row.assignee_id == current_user.id,
                task_row.submitted_at.isnot(None),
            )
        )
    ).first()
    submitted_n = int(reopen_row.submitted_n or 0) if reopen_row else 0
    rejected_rate = (
        round((reopen_row.reopened_n or 0) / submitted_n * 100, 1)
        if submitted_n > 0
        else None
    )
    reopened_avg = (
        round(float(reopen_row.reopen_avg), 2)
        if reopen_row and reopen_row.reopen_avg is not None
        else None
    )

    # 周环比：本周完成 vs 上周完成
    last_week_start = week_start - timedelta(days=7)
    last_week_n = (
        await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == current_user.id,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= last_week_start,
                annotation_row.created_at < week_start,
            )
        )
    ).scalar() or 0
    weekly_compare_pct: float | None
    if last_week_n > 0:
        weekly_compare_pct = round(
            (weekly_completed - last_week_n) / last_week_n * 100, 1
        )
    elif weekly_completed > 0:
        weekly_compare_pct = 100.0  # 上周 0 → 本周有量 → +100%
    else:
        weekly_compare_pct = None

    # 周目标：ProjectMember.weekly_target → Identity weekly_target_default → 200
    weekly_target = getattr(current_user, "weekly_target_default", None) or 200

    # v0.8.4.1 hotfix · 接通 task_events 真实数据（与 0.8.3 心跳基座并行开发遗留）
    # active_minutes_today: 当日累计 duration_ms / 60000
    active_ms_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(task_event_facts.c.duration_ms), 0).label("ms")
            ).where(
                task_event_facts.c.user_id == current_user.id,
                task_event_facts.c.started_at >= today_start,
            )
        )
    ).first()
    active_minutes_today = int((active_ms_row.ms if active_ms_row else 0) // 60000)

    # v0.8.5 · 24-bar 当日专注时段：按 EXTRACT(hour) 聚合 duration_ms → 分钟
    hour_rows = (
        await db.execute(
            select(
                func.extract("hour", task_event_facts.c.started_at).label("hour"),
                func.coalesce(func.sum(task_event_facts.c.duration_ms), 0).label("ms"),
            )
            .where(
                task_event_facts.c.user_id == current_user.id,
                task_event_facts.c.started_at >= today_start,
                task_event_facts.c.started_at < today_start + timedelta(days=1),
            )
            .group_by(func.extract("hour", task_event_facts.c.started_at))
        )
    ).all()
    hour_map = {int(r.hour): int(r.ms // 60000) for r in hour_rows}
    hour_buckets = [hour_map.get(h, 0) for h in range(24)]

    # streak_days: 从今天倒推 distinct UTC 日期连续计数（30 天上限）
    streak_cutoff = today_start - timedelta(days=29)
    day_expr = cast(func.timezone("UTC", task_event_facts.c.started_at), Date)
    day_rows = (
        await db.execute(
            select(day_expr.label("d"))
            .where(
                task_event_facts.c.user_id == current_user.id,
                task_event_facts.c.started_at >= streak_cutoff,
            )
            .distinct()
        )
    ).all()
    day_set = {r.d for r in day_rows}
    streak_days = 0
    cursor = today_start.date()
    while cursor in day_set:
        streak_days += 1
        cursor -= timedelta(days=1)

    return AnnotatorDashboardStats(
        assigned_tasks=assigned_tasks,
        today_completed=today_completed,
        weekly_completed=weekly_completed,
        total_completed=total_completed,
        personal_accuracy=round(personal_accuracy, 1),
        daily_counts=daily_counts,
        median_duration_ms=median_duration_ms,
        rejected_rate=rejected_rate,
        reopened_avg=reopened_avg,
        weekly_compare_pct=weekly_compare_pct,
        weekly_target=weekly_target,
        active_minutes_today=active_minutes_today,
        streak_days=streak_days,
        hour_buckets=hour_buckets,
        rejected_tasks_count=rejected_tasks_count,
    )


@router.get("/annotator/batches", response_model=list[MyBatchItem])
async def my_batches(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(
        require_roles(*DASHBOARD_PLATFORM_ROLES)
    ),
):
    """v0.7.1 B-17 · 标注员视角的「我的批次」：仅返回当前用户被分派、且处于
    active / annotating / rejected / reviewing 的批次。让标注员从 dashboard
    一眼看到自己手里的批次进度，并直接「提交质检」/ 查看 reviewer 留言。

    super_admin 看到所有同状态批次（便于演示 / 调试）；其他角色按 annotator_id 过滤（v0.7.2 单值）。"""
    visible_statuses = ["active", "annotating", "rejected", "reviewing"]

    visible_projects = project_search_scope(current_user)
    q = (
        select(
            batch_row,
            visible_projects.c.project_name,
            visible_projects.c.project_id,
        )
        .join(
            visible_projects,
            visible_projects.c.project_id == batch_row.project_id,
        )
        .where(batch_row.status.in_(visible_statuses))
    )
    if current_user.role != UserRole.SUPER_ADMIN:
        q = q.where(batch_row.annotator_id == current_user.id)
        # "My batches" is annotation work: restrict to projects where the
        # account still holds an annotator role (managers may also annotate in
        # a project they own).
        batch_scope = project_scope_clause(
            current_user, batch_row.project_id, project_roles=(ANNOTATOR_ROLE,)
        )
        if batch_scope is not None:
            q = q.where(batch_scope)
    q = (
        q.order_by(visible_projects.c.project_name, batch_row.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    rows = (await db.execute(q)).all()

    # B-20 · 标注员视角的进度分三档（in_progress / review / completed）：旧字段
    # completed_tasks 仅记 reviewer 已通过的任务，标注员看不到自己已动工的进展。
    # 这里一次 GROUP BY 拉齐每个 batch 的 in_progress 数量。
    batch_ids = [b.id for b, _, _ in rows]
    in_progress_map: dict = {}
    if batch_ids:
        ip_rows = await db.execute(
            select(task_row.batch_id, func.count())
            .where(
                task_row.batch_id.in_(batch_ids),
                task_row.status == "in_progress",
            )
            .group_by(task_row.batch_id)
        )
        in_progress_map = {row[0]: row[1] for row in ip_rows.all()}

    # 批次封面缩略图：每个 batch 取最早一张「有缩略图」的任务作封面（DISTINCT ON）。
    # 缩略图口径与 tasks/_shared.py 一致：dataset 导入的任务缩略图存在 DatasetItem 上，
    # task_row.thumbnail_path 为空，故需 LEFT JOIN + COALESCE，否则数据集任务永远拿不到封面。
    cover_map: dict = {}
    video_cover_requests: list[ReadSourceRequest] = []
    if batch_ids:
        from app.services.directory_policy import legacy_media_column

        eff_thumb = legacy_media_column(
            func.coalesce(DatasetItem.thumbnail_path, task_row.thumbnail_path).label(
                "thumbnail_path"
            )
        )
        eff_blur = legacy_media_column(
            func.coalesce(DatasetItem.blurhash, task_row.blurhash).label("blurhash")
        )
        cover_rows = await db.execute(
            select(
                task_row.batch_id,
                task_row.id,
                task_row.dataset_item_id,
                task_row.file_type,
                DatasetItem.file_type,
                eff_thumb,
                eff_blur,
            )
            .outerjoin(DatasetItem, DatasetItem.id == task_row.dataset_item_id)
            .where(
                task_row.batch_id.in_(batch_ids),
                eff_thumb.is_not(None),
            )
            .order_by(task_row.batch_id, task_row.created_at.asc())
            .distinct(task_row.batch_id)
        )
        for (
            batch_id,
            task_id,
            item_id,
            task_type,
            item_type,
            path,
            blurhash,
        ) in cover_rows.all():
            cover_map[batch_id] = (
                task_id,
                item_id,
                task_type,
                item_type,
                path,
                blurhash,
            )
            if item_id is not None and (task_type == "video" or item_type == "video"):
                video_cover_requests.append(
                    ReadSourceRequest(
                        actor_id=current_user.id,
                        dataset_item_id=item_id,
                        task_id=task_id,
                    )
                )
    video_posters = await video_poster_urls(db, video_cover_requests)
    video_posters_by_task = {
        request.task_id: poster_url
        for request, poster_url in zip(video_cover_requests, video_posters, strict=True)
    }

    # v0.7.2 · 单值语义 — 一 batch 一审核员
    project_user_map: dict = {}
    for b, _, _ in rows:
        if b.reviewer_id is not None:
            project_user_map.setdefault(b.project_id, set()).add(b.reviewer_id)
    briefs_by_project: dict = {}
    for pid, uids in project_user_map.items():
        briefs_by_project[pid] = await resolve_briefs_with_project_role(db, pid, uids)

    items = []
    for b, pname, pid in rows:
        per_proj = briefs_by_project.get(b.project_id, {})
        reviewer = per_proj.get(str(b.reviewer_id)) if b.reviewer_id else None
        in_progress_tasks = in_progress_map.get(b.id, 0)
        cover = cover_map.get(b.id)
        thumbnail_url: str | None = None
        cover_blurhash: str | None = None
        cover_task_id = None
        if cover is not None:
            task_id, item_id, task_type, item_type, cover_path, cover_blurhash = cover
            cover_task_id = task_id
        else:
            task_id = item_id = None
            task_type = item_type = None
            cover_path = None
        if item_id is not None and (task_type == "video" or item_type == "video"):
            thumbnail_url = video_posters_by_task.get(task_id)
            cover_blurhash = None
        elif cover_path:
            from app.services import image_pyramid_objects, thumbnail_outputs

            thumbnail_url = await image_pyramid_objects.thumbnail_url(
                db, "task", cover_task_id, cover_path
            )
            if thumbnail_url is None:
                thumbnail_url = await thumbnail_outputs.thumbnail_url(
                    db, "task", cover_task_id, cover_path
                )
        items.append(
            MyBatchItem(
                batch_id=str(b.id),
                batch_display_id=b.display_id,
                batch_name=b.name,
                project_id=str(pid),
                project_name=pname,
                status=b.status,
                total_tasks=b.total_tasks,
                completed_tasks=b.completed_tasks,
                review_tasks=b.review_tasks,
                in_progress_tasks=in_progress_tasks,
                approved_tasks=b.approved_tasks,
                rejected_tasks=b.rejected_tasks,
                progress_pct=round(
                    (b.completed_tasks / b.total_tasks * 100) if b.total_tasks else 0.0,
                    1,
                ),
                review_feedback=b.review_feedback,
                reviewed_at=b.reviewed_at.isoformat() if b.reviewed_at else None,
                thumbnail_url=thumbnail_url,
                cover_blurhash=cover_blurhash,
                reviewer=reviewer,
            )
        )
    return items


# ─── v0.8.4 · 管理员人员看板 ───────────────────────────────────────────────────


@router.get("/me/performance", response_model=MyPerformance)
async def my_performance(
    period: str = Query("4w"),
    db: AsyncSession = Depends(get_db),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    """v0.12.3 · 标注员自助绩效（取经合集 §4.1 个人页）。

    任意已认证用户看**自己**的 4 周趋势 + 耗时直方图，并叠加团队（annotator 群体）
    每周平均产出作对标基线。强制 self，不接受他人 user_id。
    """
    uid = current_user.id
    start, _end = _period_window(period)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 本期总产出
    throughput = (
        await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == uid,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= start,
            )
        )
    ).scalar() or 0

    # 团队 annotator 群体（活跃），用于每周均线分母。
    # 项目职责来自 project_members，账户必须是当前活跃账号；legacy 全局
    # annotator 账号角色不再参与统计。
    annotator_membership = aliased(
        ProjectMember, project_member_facts(), adapt_on_names=True
    )
    team_ids = (
        (
            await db.execute(
                select(annotator_membership.user_id)
                .join(
                    account_profiles,
                    account_profiles.c.id == annotator_membership.user_id,
                )
                .where(
                    account_profiles.c.is_active.is_(True),
                    annotator_membership.role == ProjectRole.ANNOTATOR.value,
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    team_n = len(team_ids) or 1

    # 4 周趋势（每周一点）：自身 throughput / quality + 团队均线
    trend_throughput: list[int] = []
    trend_quality: list[int] = []
    team_trend_throughput: list[float] = []
    for w in range(3, -1, -1):
        ws = today_start - timedelta(days=today_start.weekday()) - timedelta(weeks=w)
        we = ws + timedelta(weeks=1)
        n = (
            await db.execute(
                select(func.count())
                .select_from(annotation_row)
                .where(
                    annotation_row.user_id == uid,
                    annotation_row.is_active.is_(True),
                    annotation_row.created_at >= ws,
                    annotation_row.created_at < we,
                )
            )
        ).scalar() or 0
        trend_throughput.append(int(n))
        # 质量：1 - 当周 reopen 率
        reopen_row = (
            await db.execute(
                select(
                    func.count().label("sn"),
                    func.count().filter(task_row.reopened_count > 0).label("rn"),
                ).where(
                    task_row.assignee_id == uid,
                    task_row.submitted_at.isnot(None),
                    task_row.submitted_at >= ws,
                    task_row.submitted_at < we,
                )
            )
        ).first()
        sn = int(reopen_row.sn or 0) if reopen_row else 0
        rn = int(reopen_row.rn or 0) if reopen_row else 0
        trend_quality.append(100 if sn == 0 else max(0, 100 - int(rn / sn * 100)))
        # 团队均线：当周全 annotator 总产出 / annotator 数
        team_total = (
            await db.execute(
                select(func.count())
                .select_from(annotation_row)
                .where(
                    annotation_row.user_id.in_(team_ids),
                    annotation_row.is_active.is_(True),
                    annotation_row.created_at >= ws,
                    annotation_row.created_at < we,
                )
            )
        ).scalar() or 0
        team_trend_throughput.append(round(int(team_total) / team_n, 1))

    # 耗时直方图（10 桶）+ p50 / p95，从 task_events 拉本人 annotate
    duration_rows = (
        (
            await db.execute(
                select(task_event_facts.c.duration_ms).where(
                    task_event_facts.c.user_id == uid,
                    task_event_facts.c.kind == "annotate",
                    task_event_facts.c.started_at >= start,
                )
            )
        )
        .scalars()
        .all()
    )
    durations = [int(d) for d in duration_rows if d is not None]
    duration_histogram: list[dict] = []
    p50: int | None = None
    p95: int | None = None
    if durations:
        durations_sorted = sorted(durations)
        peak = durations_sorted[-1]
        if peak > 0:
            step = max(1, peak // 10)
            buckets = [0] * 10
            for d in durations:
                buckets[min(9, d // step)] += 1
            for i, c in enumerate(buckets):
                duration_histogram.append(
                    {"upper_ms": int((i + 1) * step), "count": int(c)}
                )
        p50 = int(durations_sorted[len(durations_sorted) // 2])
        p95_idx = max(0, int(len(durations_sorted) * 0.95) - 1)
        p95 = int(durations_sorted[p95_idx])

    # 周环比
    last_week_start = (
        today_start - timedelta(days=today_start.weekday()) - timedelta(weeks=1)
    )
    week_start_dt = last_week_start + timedelta(weeks=1)
    last_n = (
        await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == uid,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= last_week_start,
                annotation_row.created_at < week_start_dt,
            )
        )
    ).scalar() or 0
    this_n = (
        await db.execute(
            select(func.count())
            .select_from(annotation_row)
            .where(
                annotation_row.user_id == uid,
                annotation_row.is_active.is_(True),
                annotation_row.created_at >= week_start_dt,
            )
        )
    ).scalar() or 0
    weekly_compare_pct: float | None = (
        round((this_n - last_n) / last_n * 100, 1) if last_n else None
    )

    return MyPerformance(
        user_id=str(uid),
        name=current_user.name,
        period=period,
        throughput=int(throughput),
        quality_score=trend_quality[-1] if trend_quality else 100,
        weekly_compare_pct=weekly_compare_pct,
        trend_throughput=trend_throughput,
        trend_quality=trend_quality,
        team_trend_throughput=team_trend_throughput,
        duration_histogram=duration_histogram,
        p50_duration_ms=p50,
        p95_duration_ms=p95,
        reject_reason_breakdown=await _reject_reason_breakdown(db, uid, start),
        class_distribution=await _class_distribution(db, uid, start),
        first_pass_yield=await _first_pass_yield(db, uid, start),
    )
