from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.user import UserBrief


class OnboardingProjectSummary(BaseModel):
    """Server-observed progress for one employee's project checklist."""

    project_id: UUID
    assigned_task_count: int = 0
    opened_task_count: int = 0
    saved_annotation_count: int = 0
    reviewed_task_count: int = 0
    reviewed_task_id: UUID | None = None
    reviewed_task_display_id: str | None = None
    reviewed_task_status: Literal["completed", "rejected"] | None = None
    reviewed_task_reason: str | None = None


class RegistrationDayPoint(BaseModel):
    """v0.8.1 · 注册来源按日聚合：邀请 vs 开放注册。"""

    date: str  # YYYY-MM-DD
    invite_count: int
    open_count: int


class AdminDashboardStats(BaseModel):
    total_users: int
    active_users: int
    total_projects: int
    projects_in_progress: int
    projects_completed: int
    projects_pending_review: int
    projects_archived: int
    total_tasks: int
    total_annotations: int
    ml_backends_total: int
    ml_backends_connected: int
    role_distribution: dict[str, int]
    # v0.8.1 · 过去 30 天注册来源
    registration_by_day: list[RegistrationDayPoint] = []
    # v0.9.5 · AI 文本批量预标已跑完、待人工接管的批次数（Sidebar/AdminDashboard 共用）
    pre_annotated_batches: int = 0


class ReviewTaskItem(BaseModel):
    task_id: str
    task_display_id: str
    file_name: str
    project_id: str
    project_name: str
    total_annotations: int
    total_predictions: int
    updated_at: str | None
    # 返修后重新送审的任务，在 reviewer 首页单独标明，避免和首审混在一起。
    is_rework: bool = False

    class Config:
        from_attributes = True


class ReviewingBatchItem(BaseModel):
    batch_id: str
    batch_display_id: str
    batch_name: str
    project_id: str
    project_name: str
    total_tasks: int
    review_tasks: int
    completed_tasks: int
    # 批次封面：该批第一张有缩略图的任务，用于卡片网格。presigned URL + blurhash 占位。
    thumbnail_url: str | None = None
    cover_blurhash: str | None = None
    # v0.7.2 · 责任人可视化：审核员看到这批是谁标的（单值）
    annotator: UserBrief | None = None

    class Config:
        from_attributes = True


class ReviewerDashboardStats(BaseModel):
    pending_review_count: int
    today_reviewed: int
    approval_rate: float
    # v0.6.6 · 24h 滚动通过率：完成 / (完成 + 退回)，仅看过去 24 小时
    approval_rate_24h: float
    total_reviewed: int
    pending_tasks: list[ReviewTaskItem]
    # v0.7.0 · 批次级聚合：当前 reviewing 状态的批次列表
    reviewing_batches: list[ReviewingBatchItem] = []
    # v0.8.4 · 效率看板 L2 扩展
    median_review_duration_ms: int | None = None
    reopen_after_approve_rate: float | None = None
    weekly_compare_pct: float | None = None
    daily_review_counts: list[int] = []  # 7 日审核数 sparkline


class RecentReviewItem(BaseModel):
    task_id: str
    task_display_id: str
    file_name: str
    project_id: str
    project_name: str
    status: str
    reviewed_at: str | None

    class Config:
        from_attributes = True


class AnnotatorDashboardStats(BaseModel):
    assigned_tasks: int
    today_completed: int
    weekly_completed: int
    total_completed: int
    personal_accuracy: float
    daily_counts: list[int]
    # v0.8.4 · 效率看板 L2 字段（缺数据 / 心跳未落地时为 None）
    median_duration_ms: int | None = None
    rejected_rate: float | None = None
    reopened_avg: float | None = None
    weekly_compare_pct: float | None = None
    weekly_target: int = 200
    # 心跳侧未落地 → 暂返 null，前端 graceful degrade
    active_minutes_today: int | None = None
    streak_days: int | None = None
    # v0.8.5 · 当日 0-23 时分钟数（按 task_events.started_at hour 聚合）
    hour_buckets: list[int] = []
    # M1 · 当前待重做退回任务数
    rejected_tasks_count: int = 0


class ReviewerDashboardExtras(BaseModel):
    """v0.8.4 · ReviewerDashboard 扩展字段（合并到 ReviewerDashboardStats）。"""

    median_review_duration_ms: int | None = None
    rejection_rate_24h: float | None = None
    reopen_after_approve_rate: float | None = None
    weekly_compare_pct: float | None = None


class AdminPersonItem(BaseModel):
    """v0.8.4 · AdminPeoplePage 卡片项。"""

    user_id: str
    name: str
    email: str
    role: str
    status: str  # online / offline
    project_count: int
    avatar_ref: str | None = None
    main_metric: int  # 标注员=本周完成 / 审核员=本周审核
    main_metric_label: str
    weekly_compare_pct: float | None = None
    throughput_score: int  # 0-100 团队分位
    quality_score: int  # 0-100
    activity_score: int  # 0-100
    sparkline_7d: list[int]
    rejected_rate: float | None = None
    alerts: list[str] = []  # 例: ["high_rejected", "drop_30"]


class AdminPeopleList(BaseModel):
    items: list[AdminPersonItem]
    total: int
    period: str


class AdminPersonDetail(BaseModel):
    user_id: str
    name: str
    email: str
    role: str
    project_count: int
    # 4 hero KPI
    throughput: int
    quality_score: int
    active_minutes: int | None
    composite_score: int
    weekly_compare_pct: float | None
    # 4 周趋势 (每周 1 点)
    trend_throughput: list[int]
    trend_quality: list[int]
    # 项目工作量分布
    project_distribution: list[dict]
    # 耗时直方图 buckets (10 桶 + 上界 ms)
    duration_histogram: list[dict]
    # p50 / p95
    p50_duration_ms: int | None
    p95_duration_ms: int | None
    # timeline 最近事件
    timeline: list[dict]
    # v0.12.4 · 质量归因(A1)
    reject_reason_breakdown: list[dict] = []  # [{reason_type, count, pct}]
    class_distribution: list[dict] = []  # [{class_name, count, pct}] top-N
    first_pass_yield: float | None = None


class MyPerformance(BaseModel):
    """v0.12.3 · 标注员自助绩效（自己 vs 团队均线，取经合集 §4.1 个人页）。"""

    user_id: str
    name: str
    period: str
    # 本期 hero
    throughput: int
    quality_score: int
    weekly_compare_pct: float | None
    # 4 周趋势（每周 1 点）：自身 + 团队均线
    trend_throughput: list[int]
    trend_quality: list[int]
    team_trend_throughput: list[float]
    # 耗时直方图 + 分位
    duration_histogram: list[dict]
    p50_duration_ms: int | None
    p95_duration_ms: int | None
    # v0.12.4 · 质量归因(A1)
    reject_reason_breakdown: list[dict]  # [{reason_type, count, pct}]
    class_distribution: list[dict]  # [{class_name, count, pct}] top-N
    first_pass_yield: float | None  # 一次通过(无 reopen) / 提交总数


class MyBatchItem(BaseModel):
    """v0.7.1 B-17 · 标注员视角的批次卡片：自己被分派的、状态为
    active / annotating / rejected / reviewing 的批次。"""

    batch_id: str
    batch_display_id: str
    batch_name: str
    project_id: str
    project_name: str
    status: str
    total_tasks: int
    completed_tasks: int
    review_tasks: int
    # B-20：标注员已动工但未送审的任务数（status='in_progress'），用于显示"标注中"进度。
    in_progress_tasks: int = 0
    approved_tasks: int
    rejected_tasks: int
    progress_pct: float
    review_feedback: str | None = None
    reviewed_at: str | None = None
    # 批次封面：该批第一张有缩略图的任务，用于卡片网格。presigned URL + blurhash 占位。
    thumbnail_url: str | None = None
    cover_blurhash: str | None = None
    # v0.7.2 · 责任人可视化：标注员看到这批的审核员是谁（单值）
    reviewer: UserBrief | None = None

    class Config:
        from_attributes = True


# ── v0.8.6 F4 · 预测成本统计 ─────────────────────────────────────────


class BackendCostBreakdown(BaseModel):
    backend_id: UUID | None = None
    backend_name: str | None = None
    predictions: int = 0
    failures: int = 0
    total_cost: float = 0.0
    avg_inference_time_ms: float | None = None


class ReviewerMiniStats(BaseModel):
    """v0.8.7 F5.3 · ReviewWorkbench 右侧栏 mini 仪表（当日通过/退回/平均耗时）。"""

    approved_today: int = 0
    rejected_today: int = 0
    avg_review_seconds: float | None = None


class PredictionCostStats(BaseModel):
    """AdminDashboard 预测成本卡片数据结构。"""

    range: Literal["7d", "30d"]
    total_predictions: int = 0
    failed_predictions: int = 0
    failure_rate: float = 0.0
    avg_inference_time_ms: float | None = None
    # v0.8.7 F2 · 延迟分位数（PERCENTILE_CONT on prediction_metas.inference_time_ms）
    p50_inference_time_ms: float | None = None
    p95_inference_time_ms: float | None = None
    p99_inference_time_ms: float | None = None
    total_cost: float = 0.0
    total_tokens: int = 0
    by_backend: list[BackendCostBreakdown] = []
