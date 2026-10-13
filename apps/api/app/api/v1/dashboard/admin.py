import csv
import io
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from app.deps import (
    assert_project_visible,
    get_db,
    require_roles,
)
from app.db.models.user import User
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.task_batch import TaskBatch
from app.db.models.task_event import TaskEvent
from app.db.enums import ProjectRole, UserRole
from app.services.project_aggregates import ANNOTATOR_ROLE, REVIEWER_ROLE
from app.schemas.dashboard import (
    AdminDashboardStats,
    RegistrationDayPoint,
    AdminPeopleList,
    AdminPersonItem,
    AdminPersonDetail,
    PredictionCostStats,
    BackendCostBreakdown,
)
from app.db.models.project_member import ProjectMember
from app.db.models.prediction import Prediction, PredictionMeta, FailedPrediction
from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend
from app.services.dashboard_stats import (
    _class_distribution,
    _first_pass_yield,
    _percentile_rank,
    _period_window,
    _reject_reason_breakdown,
)

router = APIRouter()


@router.get("/admin", response_model=AdminDashboardStats)
async def admin_dashboard(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    users_result = await db.execute(select(User).where(User.is_active.is_(True)))
    users = users_result.scalars().all()
    total_users = len(users)
    active_users = sum(1 for u in users if u.status == "online")

    role_distribution: dict[str, int] = {}
    for u in users:
        role_distribution[u.role] = role_distribution.get(u.role, 0) + 1

    projects_result = await db.execute(select(Project))
    projects = projects_result.scalars().all()
    total_projects = len(projects)
    projects_in_progress = sum(1 for p in projects if p.status == "in_progress")
    projects_completed = sum(1 for p in projects if p.status == "completed")
    projects_pending_review = sum(1 for p in projects if p.status == "pending_review")
    projects_archived = sum(1 for p in projects if p.status == "archived")

    total_tasks_result = await db.execute(select(func.count()).select_from(Task))
    total_tasks = total_tasks_result.scalar() or 0

    total_annotations_result = await db.execute(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.is_active.is_(True))
    )
    total_annotations = total_annotations_result.scalar() or 0

    ml_total = 0
    ml_connected = 0
    try:
        from app.db.models.ml_backend_registry import MLBackendRegistry as MLBackend

        ml_result = await db.execute(select(MLBackend))
        ml_backends = ml_result.scalars().all()
        ml_total = len(ml_backends)
        ml_connected = sum(1 for m in ml_backends if m.state == "connected")
    except Exception:
        pass

    # v0.9.5 · pre_annotated 批次计数（Sidebar 徽章 + AdminDashboard 卡片共用）
    from app.db.enums import BatchStatus

    pre_annotated_batches = (
        await db.execute(
            select(func.count())
            .select_from(TaskBatch)
            .where(TaskBatch.status == BatchStatus.PRE_ANNOTATED)
        )
    ).scalar() or 0

    # v0.8.1 · 过去 30 天注册来源（按日聚合 audit_logs.action='user.register'）
    cutoff_30d = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=29)
    reg_rows = (
        await db.execute(
            select(
                func.date_trunc("day", AuditLog.created_at).label("day"),
                func.count()
                .filter(AuditLog.detail_json["method"].astext == "open_registration")
                .label("open_count"),
                func.count()
                .filter(AuditLog.detail_json.has_key("invitation_id"))  # noqa: W601
                .label("invite_count"),
            )
            .where(
                AuditLog.action == "user.register",
                AuditLog.created_at >= cutoff_30d,
            )
            .group_by("day")
            .order_by("day")
        )
    ).all()
    by_day_map = {
        r.day.date().isoformat(): (int(r.open_count or 0), int(r.invite_count or 0))
        for r in reg_rows
    }
    registration_by_day: list[RegistrationDayPoint] = []
    for i in range(30):
        d = (cutoff_30d + timedelta(days=i)).date().isoformat()
        open_n, invite_n = by_day_map.get(d, (0, 0))
        registration_by_day.append(
            RegistrationDayPoint(date=d, open_count=open_n, invite_count=invite_n)
        )

    return AdminDashboardStats(
        total_users=total_users,
        active_users=active_users,
        total_projects=total_projects,
        projects_in_progress=projects_in_progress,
        projects_completed=projects_completed,
        projects_pending_review=projects_pending_review,
        projects_archived=projects_archived,
        total_tasks=total_tasks,
        total_annotations=total_annotations,
        ml_backends_total=ml_total,
        ml_backends_connected=ml_connected,
        role_distribution=role_distribution,
        registration_by_day=registration_by_day,
        pre_annotated_batches=int(pre_annotated_batches),
    )


async def _resolve_people_scope(
    db: AsyncSession, current_user: User, project: str | None
) -> uuid.UUID | None:
    """v0.12.6 (A3) · 解析成员绩效的项目范围 + 强制 RBAC。

    - super_admin：project 可选（给了就校验存在），返回 pid 或 None（全局）。
    - project_admin：project 必填且必须是其 **owner** 的项目（越权 / 不存在均返回
      404 隐藏存在性），缺失 project → 403。

    注意：project_admin 这里**不复用** `assert_project_visible`——后者在 owner 检查
    失败后会 fallback 到 `ProjectMember`，导致「身为他人项目 member 的 project_admin」
    也能读到该项目他人绩效。成员绩效语义严格限定为 owner，故自行校验 `owner_id`。

    返回用于聚合过滤的 project_id（None = 全局，仅 super_admin 可得）。
    """
    pid: uuid.UUID | None = None
    if project:
        try:
            pid = uuid.UUID(project)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail="invalid project id") from exc
    if current_user.role == UserRole.PROJECT_ADMIN:
        if pid is None:
            raise HTTPException(
                status_code=403, detail="project_admin 必须指定其管理的项目范围"
            )
        proj = await db.get(Project, pid)
        if proj is None or proj.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="项目不存在")
    elif pid is not None:
        # super_admin 指定项目：校验存在（对 super_admin 恒可见，无 member fallback 问题）。
        await assert_project_visible(pid, db, current_user)
    return pid


@router.get("/admin/people", response_model=AdminPeopleList)
async def admin_people_list(
    role: str | None = Query(None),
    project: str | None = Query(None),
    period: str = Query("7d"),
    sort: str = Query("throughput"),
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    """v0.8.4 · 全员效率卡片网格数据。

    role 过滤：annotator / reviewer / both（默认 both）
    period: today / 7d / 4w / 1m
    sort: throughput / quality / activity / weekly_compare

    v0.12.6 (A3)：project 给定时所有产能/质量/活跃聚合**按该项目切分**（非全局）；
    project_admin 必须指定其项目，super_admin 可全局或任意项目。
    """
    pid = await _resolve_people_scope(db, current_user, project)
    # 聚合级项目过滤片段（pid 为 None 时为空 → 全局，行为与改造前一致）。
    ann_proj = [Annotation.project_id == pid] if pid else []
    task_proj = [Task.project_id == pid] if pid else []
    event_proj = [TaskEvent.project_id == pid] if pid else []

    start, _end = _period_window(period)
    week_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_start = week_start - timedelta(days=week_start.weekday())
    last_week_start = week_start - timedelta(days=7)

    # 拉用户：标注 / 审核分组由 project_members.role 决定，平台账号角色不再
    # 参与分组（避免 employee 同时在不同项目承担不同职责时被错误归类）。
    # project 给定时只保留该项目内持有对应职责的成员。
    user_q = select(User).where(User.is_active.is_(True))
    if role in {"annotator", "reviewer"}:
        target_role = (
            ProjectRole.ANNOTATOR.value
            if role == "annotator"
            else ProjectRole.REVIEWER.value
        )
        membership_scope = select(ProjectMember.user_id).where(
            ProjectMember.role == target_role
        )
        if pid:
            membership_scope = membership_scope.where(ProjectMember.project_id == pid)
        user_q = user_q.where(User.id.in_(membership_scope))
    if q:
        like = f"%{q}%"
        user_q = user_q.where(or_(User.name.ilike(like), User.email.ilike(like)))
    users = (await db.execute(user_q)).scalars().all()

    if not users:
        return AdminPeopleList(items=[], total=0, period=period)

    user_ids = [u.id for u in users]

    # Per-user project roles for this scope (single query, no N+1): drives the
    # annotator/reviewer metric classification below.
    role_rows = (
        await db.execute(
            select(ProjectMember.user_id, ProjectMember.role).where(
                ProjectMember.user_id.in_(user_ids),
                *([ProjectMember.project_id == pid] if pid else []),
            )
        )
    ).all()
    user_roles_map: dict[uuid.UUID, set[str]] = {}
    for member_user_id, member_role in role_rows:
        user_roles_map.setdefault(member_user_id, set()).add(member_role)

    # 项目隶属计数
    pm_rows = (
        await db.execute(
            select(ProjectMember.user_id, func.count().label("n"))
            .where(ProjectMember.user_id.in_(user_ids))
            .group_by(ProjectMember.user_id)
        )
    ).all()
    pm_count_map = {r.user_id: int(r.n) for r in pm_rows}

    # 标注吞吐（period 内）
    ann_rows = (
        await db.execute(
            select(Annotation.user_id, func.count().label("n"))
            .where(
                Annotation.user_id.in_(user_ids),
                Annotation.is_active.is_(True),
                Annotation.created_at >= start,
                *ann_proj,
            )
            .group_by(Annotation.user_id)
        )
    ).all()
    ann_count_map = {r.user_id: int(r.n) for r in ann_rows}

    # 审核吞吐（reviewer）
    rev_rows = (
        await db.execute(
            select(Task.reviewer_id, func.count().label("n"))
            .where(
                Task.reviewer_id.in_(user_ids),
                Task.reviewed_at.isnot(None),
                Task.reviewed_at >= start,
                *task_proj,
            )
            .group_by(Task.reviewer_id)
        )
    ).all()
    rev_count_map = {r.reviewer_id: int(r.n) for r in rev_rows}

    # 上周对比（标注员）
    last_week_rows = (
        await db.execute(
            select(Annotation.user_id, func.count().label("n"))
            .where(
                Annotation.user_id.in_(user_ids),
                Annotation.is_active.is_(True),
                Annotation.created_at >= last_week_start,
                Annotation.created_at < week_start,
                *ann_proj,
            )
            .group_by(Annotation.user_id)
        )
    ).all()
    last_week_map = {r.user_id: int(r.n) for r in last_week_rows}

    this_week_rows = (
        await db.execute(
            select(Annotation.user_id, func.count().label("n"))
            .where(
                Annotation.user_id.in_(user_ids),
                Annotation.is_active.is_(True),
                Annotation.created_at >= week_start,
                *ann_proj,
            )
            .group_by(Annotation.user_id)
        )
    ).all()
    this_week_map = {r.user_id: int(r.n) for r in this_week_rows}

    # 退回率（标注员）
    reopen_rows = (
        await db.execute(
            select(
                Task.assignee_id,
                func.count().label("submitted_n"),
                func.count().filter(Task.reopened_count > 0).label("reopened_n"),
            )
            .where(
                Task.assignee_id.in_(user_ids),
                Task.submitted_at.isnot(None),
                *task_proj,
            )
            .group_by(Task.assignee_id)
        )
    ).all()
    reopen_map = {
        r.assignee_id: (int(r.submitted_n or 0), int(r.reopened_n or 0))
        for r in reopen_rows
    }

    # 7 日 sparkline（统一按 annotation 创建数；reviewer 用 reviewed_at）
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    daily_buckets: dict = {}  # user_id → [7 ints]
    for i in range(6, -1, -1):
        ds = today_start - timedelta(days=i)
        de = ds + timedelta(days=1)
        rows = (
            await db.execute(
                select(Annotation.user_id, func.count().label("n"))
                .where(
                    Annotation.user_id.in_(user_ids),
                    Annotation.is_active.is_(True),
                    Annotation.created_at >= ds,
                    Annotation.created_at < de,
                    *ann_proj,
                )
                .group_by(Annotation.user_id)
            )
        ).all()
        per_user = {r.user_id: int(r.n) for r in rows}
        for uid in user_ids:
            daily_buckets.setdefault(uid, []).append(per_user.get(uid, 0))

    # v0.8.4.1 hotfix · 7d active_minutes 团队分位（替换 activity_score=50 占位）
    seven_d_start = today_start - timedelta(days=6)
    active_rows = (
        await db.execute(
            select(
                TaskEvent.user_id,
                func.coalesce(func.sum(TaskEvent.duration_ms), 0).label("ms"),
            )
            .where(
                TaskEvent.user_id.in_(user_ids),
                TaskEvent.started_at >= seven_d_start,
                *event_proj,
            )
            .group_by(TaskEvent.user_id)
        )
    ).all()
    active_minutes_map = {r.user_id: int((r.ms or 0) // 60000) for r in active_rows}

    # 计算分位
    def _is_reviewer(u: User) -> bool:
        roles = user_roles_map.get(u.id, set())
        if role == "reviewer":
            # The request explicitly asks for reviewers; a mixed employee is
            # reported by review throughput so the filter and metric agree.
            return REVIEWER_ROLE in roles
        if role == "annotator":
            return False
        if roles:
            # Unfiltered "both" view: a reviewer membership classifies the row
            # as review work, while an account that also annotates stays in the
            # annotation cohort (the previous "reviewer and not annotator" rule).
            return REVIEWER_ROLE in roles and ANNOTATOR_ROLE not in roles
        # Manager accounts without a membership row keep their review-side
        # label.  This is metric labeling only; authority is never derived here.
        return u.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)

    throughputs = []
    quality_scores = []
    activity_minutes_list = []
    for u in users:
        if _is_reviewer(u):
            throughputs.append(rev_count_map.get(u.id, 0))
        else:
            throughputs.append(ann_count_map.get(u.id, 0))
        sub_n, reop_n = reopen_map.get(u.id, (0, 0))
        rejected_rate = (reop_n / sub_n * 100) if sub_n > 0 else 0.0
        quality_scores.append(100.0 - min(100.0, rejected_rate))
        activity_minutes_list.append(active_minutes_map.get(u.id, 0))

    items: list[AdminPersonItem] = []
    for idx, u in enumerate(users):
        is_reviewer = _is_reviewer(u)
        main_metric = throughputs[idx]
        main_label = (
            f"本周{period if period != '7d' else ''}审核数"
            if is_reviewer
            else f"本周{period if period != '7d' else ''}完成数"
        )

        sub_n, reop_n = reopen_map.get(u.id, (0, 0))
        rejected_rate: float | None = (
            round((reop_n / sub_n * 100), 1) if sub_n > 0 else None
        )

        last_n = last_week_map.get(u.id, 0)
        this_n = this_week_map.get(u.id, 0)
        if last_n > 0:
            wcp: float | None = round((this_n - last_n) / last_n * 100, 1)
        elif this_n > 0:
            wcp = 100.0
        else:
            wcp = None

        alerts: list[str] = []
        if rejected_rate is not None and rejected_rate > 15:
            alerts.append("high_rejected")
        if wcp is not None and wcp < -30:
            alerts.append("drop_30")

        items.append(
            AdminPersonItem(
                user_id=str(u.id),
                name=u.name,
                email=u.email,
                role=u.role,
                status=u.status,
                project_count=pm_count_map.get(u.id, 0),
                avatar_ref=u.avatar_ref,
                main_metric=main_metric,
                main_metric_label=main_label,
                weekly_compare_pct=wcp,
                throughput_score=_percentile_rank(throughputs, throughputs[idx]),
                quality_score=_percentile_rank(quality_scores, quality_scores[idx]),
                activity_score=_percentile_rank(
                    activity_minutes_list, activity_minutes_list[idx]
                ),
                sparkline_7d=daily_buckets.get(u.id, [0] * 7),
                rejected_rate=rejected_rate,
                alerts=alerts,
            )
        )

    # 排序
    if sort == "quality":
        items.sort(key=lambda it: it.quality_score, reverse=True)
    elif sort == "activity":
        items.sort(key=lambda it: it.activity_score, reverse=True)
    elif sort == "weekly_compare":
        items.sort(key=lambda it: it.weekly_compare_pct or -999, reverse=True)
    else:
        items.sort(key=lambda it: it.main_metric, reverse=True)

    # 项目过滤：指定项目时只返回该项目成员（聚合数字已在上面按 pid 切分）。
    if pid:
        allowed = (
            (
                await db.execute(
                    select(ProjectMember.user_id).where(ProjectMember.project_id == pid)
                )
            )
            .scalars()
            .all()
        )
        allowed_set = {str(x) for x in allowed}
        items = [it for it in items if it.user_id in allowed_set]

    return AdminPeopleList(items=items, total=len(items), period=period)


# v0.12.5 · 成员绩效 CSV 导出(A2)。
# 注意:必须定义在 /admin/people/{user_id} 之前,否则 "export" 会被当作 user_id 命中详情端点。
_PEOPLE_EXPORT_COLS = [
    "user_id",
    "name",
    "email",
    "role",
    "status",
    "project_count",
    "main_metric",
    "main_metric_label",
    "weekly_compare_pct",
    "throughput_score",
    "quality_score",
    "activity_score",
    "rejected_rate",
]


@router.get("/admin/people/export")
async def admin_people_export(
    role: str | None = Query(None),
    project: str | None = Query(None),
    period: str = Query("7d"),
    sort: str = Query("throughput"),
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    """v0.12.5 · 成员绩效 CSV 导出。复用 admin_people_list 聚合,零重复;Excel UTF-8 BOM 防中文乱码。

    v0.12.6 (A3)：放行 project_admin（委托 admin_people_list 强制其项目范围）。
    """
    data = await admin_people_list(
        role=role,
        project=project,
        period=period,
        sort=sort,
        q=q,
        db=db,
        current_user=current_user,
    )

    # 名实相符:admin_people_list 已把全表加载进内存(百行级),
    # CSV 也是一次拼好,直接 Response,不假装流式。
    buf = io.StringIO()
    buf.write("﻿")  # Excel UTF-8 BOM
    writer = csv.DictWriter(buf, fieldnames=_PEOPLE_EXPORT_COLS, extrasaction="ignore")
    writer.writeheader()
    for it in data.items:
        writer.writerow(it.model_dump(mode="json"))

    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=people_performance_{period}.csv"
        },
    )


@router.get("/admin/people/{user_id}", response_model=AdminPersonDetail)
async def admin_person_detail(
    user_id: str,
    period: str = Query("4w"),
    project: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    """v0.8.4 · 个人详情：4 周趋势、耗时直方图、项目分布、timeline。

    v0.12.6 (A3)：project 给定时产能/质量/耗时/归因聚合**按该项目切分**；
    project_admin 必须指定其项目，super_admin 可全局或任意项目。timeline（审计活动流）
    不含可靠 project 维度，保持全局。
    """
    pid = await _resolve_people_scope(db, current_user, project)
    ann_proj = [Annotation.project_id == pid] if pid else []
    task_proj = [Task.project_id == pid] if pid else []
    event_proj = [TaskEvent.project_id == pid] if pid else []

    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid user_id")

    user = await db.get(User, uid)
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="user not found")

    # v0.12.6 (A3) 安全收口:project_admin 只能查其项目内的成员详情。否则可借任意
    # user_id + 自有 project 枚举他人存在性 + 读取全局 timeline(IDOR)。非成员 → 404。
    if current_user.role == UserRole.PROJECT_ADMIN and pid is not None:
        membership = (
            await db.execute(
                select(ProjectMember).where(
                    ProjectMember.project_id == pid,
                    ProjectMember.user_id == uid,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=404, detail="user not found")

    start, _end = _period_window(period)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 总产出
    throughput = (
        await db.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.user_id == uid,
                Annotation.is_active.is_(True),
                Annotation.created_at >= start,
                *ann_proj,
            )
        )
    ).scalar() or 0

    # 4 周趋势（每周一点）
    trend_throughput: list[int] = []
    trend_quality: list[int] = []
    for w in range(3, -1, -1):
        ws = today_start - timedelta(days=today_start.weekday()) - timedelta(weeks=w)
        we = ws + timedelta(weeks=1)
        n = (
            await db.execute(
                select(func.count())
                .select_from(Annotation)
                .where(
                    Annotation.user_id == uid,
                    Annotation.is_active.is_(True),
                    Annotation.created_at >= ws,
                    Annotation.created_at < we,
                    *ann_proj,
                )
            )
        ).scalar() or 0
        trend_throughput.append(int(n))
        # 质量：1 - 当周 reopen 率
        reopen_row = (
            await db.execute(
                select(
                    func.count().label("sn"),
                    func.count().filter(Task.reopened_count > 0).label("rn"),
                ).where(
                    Task.assignee_id == uid,
                    Task.submitted_at.isnot(None),
                    Task.submitted_at >= ws,
                    Task.submitted_at < we,
                    *task_proj,
                )
            )
        ).first()
        sn = int(reopen_row.sn or 0) if reopen_row else 0
        rn = int(reopen_row.rn or 0) if reopen_row else 0
        q = 100 if sn == 0 else max(0, 100 - int(rn / sn * 100))
        trend_quality.append(q)

    # 项目分布
    proj_rows = (
        await db.execute(
            select(
                Annotation.project_id,
                Project.name,
                func.count().label("n"),
            )
            .join(Project, Annotation.project_id == Project.id)
            .where(
                Annotation.user_id == uid,
                Annotation.is_active.is_(True),
                Annotation.created_at >= start,
                *ann_proj,
            )
            .group_by(Annotation.project_id, Project.name)
            .order_by(func.count().desc())
        )
    ).all()
    project_distribution = [
        {"project_id": str(r.project_id), "project_name": r.name, "count": int(r.n)}
        for r in proj_rows
    ]

    # 耗时直方图：从 task_events 拉本人的 annotate kind（TaskEvent 已在模块级导入）
    duration_rows = (
        (
            await db.execute(
                select(TaskEvent.duration_ms).where(
                    TaskEvent.user_id == uid,
                    TaskEvent.kind == "annotate",
                    TaskEvent.started_at >= start,
                    *event_proj,
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
        # 10 桶 [0..peak]
        if peak > 0:
            step = max(1, peak // 10)
            buckets = [0] * 10
            for d in durations:
                idx = min(9, d // step)
                buckets[idx] += 1
            for i, c in enumerate(buckets):
                duration_histogram.append(
                    {"upper_ms": int((i + 1) * step), "count": int(c)}
                )
        p50 = int(durations_sorted[len(durations_sorted) // 2])
        p95_idx = max(0, int(len(durations_sorted) * 0.95) - 1)
        p95 = int(durations_sorted[p95_idx])

    # timeline：最近 50 条 audit_logs
    timeline_rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.actor_id == uid)
                .where(
                    AuditLog.action.in_(
                        [
                            "task.submit",
                            "task.approve",
                            "task.reject",
                            "task.reopen",
                            "task.create_annotation",
                        ]
                    )
                )
                .order_by(AuditLog.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    timeline = []
    for a in timeline_rows:
        target_id = (
            a.target_id
            if a.target_id and a.target_type == "task"
            else (a.detail_json or {}).get("task_id")
        )
        timeline.append(
            {
                "at": a.created_at.isoformat() if a.created_at else "",
                "action": a.action,
                "task_id": str(target_id) if target_id else None,
                "task_display_id": (a.detail_json or {}).get("task_display_id"),
                "detail": (a.detail_json or {}).get("reason"),
            }
        )

    # 综合分（throughput + quality / 2，活跃暂用 50）
    quality_score = trend_quality[-1] if trend_quality else 50
    composite = int(round((min(100, throughput) + quality_score + 50) / 3))

    proj_count = (
        await db.execute(
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.user_id == uid)
        )
    ).scalar() or 0

    # 周环比
    last_week_start = (
        today_start - timedelta(days=today_start.weekday()) - timedelta(weeks=1)
    )
    week_start_dt = last_week_start + timedelta(weeks=1)
    last_n = (
        await db.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.user_id == uid,
                Annotation.is_active.is_(True),
                Annotation.created_at >= last_week_start,
                Annotation.created_at < week_start_dt,
                *ann_proj,
            )
        )
    ).scalar() or 0
    this_n = (
        await db.execute(
            select(func.count())
            .select_from(Annotation)
            .where(
                Annotation.user_id == uid,
                Annotation.is_active.is_(True),
                Annotation.created_at >= week_start_dt,
                *ann_proj,
            )
        )
    ).scalar() or 0
    if last_n > 0:
        wcp = round((this_n - last_n) / last_n * 100, 1)
    elif this_n > 0:
        wcp = 100.0
    else:
        wcp = None

    return AdminPersonDetail(
        user_id=str(user.id),
        name=user.name,
        email=user.email,
        role=user.role,
        project_count=int(proj_count),
        throughput=int(throughput),
        quality_score=int(quality_score),
        active_minutes=None,
        composite_score=int(composite),
        weekly_compare_pct=wcp,
        trend_throughput=trend_throughput,
        trend_quality=trend_quality,
        project_distribution=project_distribution,
        duration_histogram=duration_histogram,
        p50_duration_ms=p50,
        p95_duration_ms=p95,
        timeline=timeline,
        reject_reason_breakdown=await _reject_reason_breakdown(
            db, uid, start, project_id=pid
        ),
        class_distribution=await _class_distribution(db, uid, start, project_id=pid),
        first_pass_yield=await _first_pass_yield(db, uid, start, project_id=pid),
    )


# ── v0.8.6 F4 · 预测成本卡片 ─────────────────────────────────────────


_RANGE_DAYS = {"7d": 7, "30d": 30}


@router.get("/admin/prediction-cost-stats", response_model=PredictionCostStats)
async def prediction_cost_stats(
    range: str = Query("30d", pattern="^(7d|30d)$"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """v0.8.6 F4 · 过去 N 天预测调用数 / 平均耗时 / 失败率 / 总成本。

    数据来源：predictions × prediction_metas × failed_predictions × ml_backends。
    异常时降级返回零，避免 Dashboard 因聚合失败黑屏。
    """
    days = _RANGE_DAYS.get(range, 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        # 主聚合：调用数 / avg / p50 / p95 / p99 / total_cost / total_tokens
        # v0.8.7 F2 · 加 PERCENTILE_CONT(p50/p95/p99) WITHIN GROUP；postgres 原生支持。
        main = (
            await db.execute(
                select(
                    func.count(Prediction.id).label("total"),
                    func.avg(PredictionMeta.inference_time_ms).label("avg_ms"),
                    func.percentile_cont(0.5)
                    .within_group(PredictionMeta.inference_time_ms.asc())
                    .label("p50_ms"),
                    func.percentile_cont(0.95)
                    .within_group(PredictionMeta.inference_time_ms.asc())
                    .label("p95_ms"),
                    func.percentile_cont(0.99)
                    .within_group(PredictionMeta.inference_time_ms.asc())
                    .label("p99_ms"),
                    func.coalesce(func.sum(PredictionMeta.total_cost), 0.0).label(
                        "total_cost"
                    ),
                    func.coalesce(func.sum(PredictionMeta.total_tokens), 0).label(
                        "total_tokens"
                    ),
                )
                .select_from(Prediction)
                .outerjoin(
                    PredictionMeta, PredictionMeta.prediction_id == Prediction.id
                )
                .where(Prediction.created_at >= cutoff)
            )
        ).one()

        # 失败数
        failed_count = (
            await db.execute(
                select(func.count(FailedPrediction.id)).where(
                    FailedPrediction.created_at >= cutoff
                )
            )
        ).scalar() or 0

        # by_backend
        rows = (
            await db.execute(
                select(
                    Prediction.ml_backend_id,
                    MLBackend.name,
                    func.count(Prediction.id),
                    func.coalesce(func.sum(PredictionMeta.total_cost), 0.0),
                    func.avg(PredictionMeta.inference_time_ms),
                )
                .select_from(Prediction)
                .outerjoin(
                    PredictionMeta, PredictionMeta.prediction_id == Prediction.id
                )
                .outerjoin(MLBackend, MLBackend.id == Prediction.ml_backend_id)
                .where(Prediction.created_at >= cutoff)
                .group_by(Prediction.ml_backend_id, MLBackend.name)
            )
        ).all()

        # 失败按 backend
        failed_by_backend_rows = (
            await db.execute(
                select(FailedPrediction.ml_backend_id, func.count(FailedPrediction.id))
                .where(FailedPrediction.created_at >= cutoff)
                .group_by(FailedPrediction.ml_backend_id)
            )
        ).all()
        failed_by_backend = {bid: int(c) for bid, c in failed_by_backend_rows}

        by_backend = [
            BackendCostBreakdown(
                backend_id=bid,
                backend_name=name,
                predictions=int(cnt),
                failures=failed_by_backend.get(bid, 0),
                total_cost=float(cost or 0.0),
                avg_inference_time_ms=(float(avg_ms) if avg_ms is not None else None),
            )
            for bid, name, cnt, cost, avg_ms in rows
        ]

        total = int(main.total or 0)
        denom = total + failed_count
        failure_rate = (failed_count / denom) if denom else 0.0

        return PredictionCostStats(
            range=range,
            total_predictions=total,
            failed_predictions=failed_count,
            failure_rate=round(failure_rate, 4),
            avg_inference_time_ms=(
                float(main.avg_ms) if main.avg_ms is not None else None
            ),
            p50_inference_time_ms=(
                float(main.p50_ms) if main.p50_ms is not None else None
            ),
            p95_inference_time_ms=(
                float(main.p95_ms) if main.p95_ms is not None else None
            ),
            p99_inference_time_ms=(
                float(main.p99_ms) if main.p99_ms is not None else None
            ),
            total_cost=float(main.total_cost or 0.0),
            total_tokens=int(main.total_tokens or 0),
            by_backend=by_backend,
        )
    except Exception:
        # 数据量异常或 schema 漂移时降级，避免 Dashboard 黑屏
        return PredictionCostStats(range=range)
