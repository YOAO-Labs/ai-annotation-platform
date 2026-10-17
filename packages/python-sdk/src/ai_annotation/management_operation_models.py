"""Typed contracts for dashboard, annotation import, and prediction cleanup."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from ai_annotation.models import UserBrief, _AAPModel


class OnboardingProjectSummary(_AAPModel):
    project_id: UUID
    assigned_task_count: int = 0
    opened_task_count: int = 0
    saved_annotation_count: int = 0
    reviewed_task_count: int = 0
    reviewed_task_id: UUID | None = None
    reviewed_task_display_id: str | None = None
    reviewed_task_status: Literal["completed", "rejected"] | None = None
    reviewed_task_reason: str | None = None


class RecentReviewItem(_AAPModel):
    task_id: str
    task_display_id: str
    file_name: str
    project_id: str
    project_name: str
    status: str
    reviewed_at: str | None = None


class MyBatchItem(_AAPModel):
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


class ReviewerMiniStats(_AAPModel):
    approved_today: int = 0
    rejected_today: int = 0
    avg_review_seconds: float | None = None


class AdminPersonDetail(_AAPModel):
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
    project_distribution: list[dict[str, Any]]
    duration_histogram: list[dict[str, Any]]
    p50_duration_ms: int | None
    p95_duration_ms: int | None
    timeline: list[dict[str, Any]]
    reject_reason_breakdown: list[dict[str, Any]] = Field(default_factory=list)
    class_distribution: list[dict[str, Any]] = Field(default_factory=list)
    first_pass_yield: float | None = None


class BackendCostBreakdown(_AAPModel):
    backend_id: UUID | None = None
    backend_name: str | None = None
    predictions: int = 0
    failures: int = 0
    total_cost: float = 0.0
    avg_inference_time_ms: float | None = None


class PredictionCostStats(_AAPModel):
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
    by_backend: list[BackendCostBreakdown] = Field(default_factory=list)


class AnnotationImportErrorEntry(_AAPModel):
    task_match: dict[str, Any] = Field(default_factory=dict)
    reason: str


class AnnotationImportResult(_AAPModel):
    imported: int = 0
    skipped: int = 0
    errors: list[AnnotationImportErrorEntry] = Field(default_factory=list)
    dry_run: bool = False


PredictionSourceScope = Literal["ml_backend", "external_import", "all"]


class PredictionPurgeRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_scope: PredictionSourceScope = "external_import"
    task_ids: list[UUID] | None = None
    dry_run: bool = False


class PredictionPurgeCounts(_AAPModel):
    ml_backend: int = 0
    external_import: int = 0
    unknown: int = 0
    total: int = 0


class PredictionPurgeResponse(_AAPModel):
    source_scope: PredictionSourceScope
    task_ids: list[UUID] | None = None
    dry_run: bool
    counts: PredictionPurgeCounts
