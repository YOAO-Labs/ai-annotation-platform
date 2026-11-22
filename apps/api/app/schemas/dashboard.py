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
    """注册来源按日聚合：邀请 vs 开放注册。"""

    date: str
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
    registration_by_day: list[RegistrationDayPoint] = []
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
    thumbnail_url: str | None = None
    cover_blurhash: str | None = None
    annotator: UserBrief | None = None

    class Config:
        from_attributes = True


class ReviewerDashboardStats(BaseModel):
    pending_review_count: int
    today_reviewed: int
    approval_rate: float
    approval_rate_24h: float
    total_reviewed: int
    pending_tasks: list[ReviewTaskItem]
    reviewing_batches: list[ReviewingBatchItem] = []
    median_review_duration_ms: int | None = None
    reopen_after_approve_rate: float | None = None
    weekly_compare_pct: float | None = None
    daily_review_counts: list[int] = []


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
    median_duration_ms: int | None = None
    rejected_rate: float | None = None
    reopened_avg: float | None = None
    weekly_compare_pct: float | None = None
    weekly_target: int = 200
    active_minutes_today: int | None = None
    streak_days: int | None = None
    hour_buckets: list[int] = []
    rejected_tasks_count: int = 0


class ReviewerDashboardExtras(BaseModel):
    """ReviewerDashboard 扩展字段（合并到 ReviewerDashboardStats）。"""

    median_review_duration_ms: int | None = None
    rejection_rate_24h: float | None = None
    reopen_after_approve_rate: float | None = None
    weekly_compare_pct: float | None = None


class AdminPersonItem(BaseModel):
    """AdminPeoplePage 卡片项。"""

    user_id: str
    name: str
    email: str
    role: str
    status: str
    project_count: int
    avatar_ref: str | None = None
    main_metric: int
    main_metric_label: str
    weekly_compare_pct: float | None = None
    throughput_score: int
    quality_score: int
    activity_score: int
    sparkline_7d: list[int]
    rejected_rate: float | None = None
    alerts: list[str] = []


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
    throughput: int
    quality_score: int
    active_minutes: int | None
    composite_score: int
    weekly_compare_pct: float | None
    trend_throughput: list[int]
    trend_quality: list[int]
    project_distribution: list[dict]
    duration_histogram: list[dict]
    p50_duration_ms: int | None
    p95_duration_ms: int | None
    timeline: list[dict]
    reject_reason_breakdown: list[dict] = []
    class_distribution: list[dict] = []
    first_pass_yield: float | None = None


class MyPerformance(BaseModel):
    """标注员自助绩效（自己 vs 团队均线，取经合集 §4.1 个人页）。"""

    user_id: str
    name: str
    period: str
    throughput: int
    quality_score: int
    weekly_compare_pct: float | None
    trend_throughput: list[int]
    trend_quality: list[int]
    team_trend_throughput: list[float]
    duration_histogram: list[dict]
    p50_duration_ms: int | None
    p95_duration_ms: int | None
    reject_reason_breakdown: list[dict]
    class_distribution: list[dict]
    first_pass_yield: float | None


class MyBatchItem(BaseModel):
    """标注员视角的批次卡片：自己被分派的、状态为
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
    in_progress_tasks: int = 0
    approved_tasks: int
    rejected_tasks: int
    progress_pct: float
    review_feedback: str | None = None
    reviewed_at: str | None = None
    thumbnail_url: str | None = None
    cover_blurhash: str | None = None
    reviewer: UserBrief | None = None

    class Config:
        from_attributes = True


class BackendCostBreakdown(BaseModel):
    backend_id: UUID | None = None
    backend_name: str | None = None
    predictions: int = 0
    failures: int = 0
    total_cost: float = 0.0
    avg_inference_time_ms: float | None = None


class ReviewerMiniStats(BaseModel):
    """ReviewWorkbench 右侧栏 mini 仪表（当日通过/退回/平均耗时）。"""

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
    p50_inference_time_ms: float | None = None
    p95_inference_time_ms: float | None = None
    p99_inference_time_ms: float | None = None
    total_cost: float = 0.0
    total_tokens: int = 0
    by_backend: list[BackendCostBreakdown] = []
