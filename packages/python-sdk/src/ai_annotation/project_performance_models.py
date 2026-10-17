"""Typed query and response models for project member performance."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from ai_annotation.models import _AAPModel

ProjectPerformanceWorkType = Literal["annotation", "review"]
ProjectPerformanceAccountStatus = Literal["all", "active", "inactive"]
ProjectPerformanceCoverageState = Literal["complete", "partial", "unknown"]
ProjectPerformanceMetricUnit = Literal[
    "tasks", "objects", "images", "decisions", "minutes", "percent"
]
DateBoundary = str | date | datetime


class ProjectPerformanceQuery(_AAPModel):
    """Shared filters; ``cursor`` pages members, events, or detail evidence."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: DateBoundary | None = Field(default=None, alias="from")
    to: DateBoundary | None = None
    timezone: str | None = None
    work_type: ProjectPerformanceWorkType = "annotation"
    account_status: ProjectPerformanceAccountStatus = "all"
    include_historical: bool = False
    q: str | None = Field(default=None, max_length=200)
    sort: str | None = "+name"
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class ProjectPerformanceScope(_AAPModel):
    from_: datetime = Field(alias="from")
    to: datetime
    timezone: str
    as_of: datetime


class ProjectPerformanceCoverage(_AAPModel):
    state: ProjectPerformanceCoverageState
    source: str
    detail: str | None = None


class ProjectPerformanceMetric(_AAPModel):
    value: int | float | None
    unit: ProjectPerformanceMetricUnit
    numerator: int | None = None
    denominator: int | None = None
    coverage: ProjectPerformanceCoverageState | None = None


class ProjectMemberPerformanceMetrics(_AAPModel):
    submitted_tasks: ProjectPerformanceMetric
    resubmissions: ProjectPerformanceMetric
    contributed_tasks: ProjectPerformanceMetric
    annotated_images: ProjectPerformanceMetric
    retained_objects: ProjectPerformanceMetric
    approved_task_outcomes: ProjectPerformanceMetric
    first_review_pass_rate: ProjectPerformanceMetric
    recorded_time_minutes: ProjectPerformanceMetric
    current_backlog: ProjectPerformanceMetric
    review_decisions: ProjectPerformanceMetric
    approvals: ProjectPerformanceMetric
    rejections: ProjectPerformanceMetric
    reviewed_tasks: ProjectPerformanceMetric
    recorded_review_minutes: ProjectPerformanceMetric
    review_backlog: ProjectPerformanceMetric


class ProjectMemberPerformance(_AAPModel):
    user_id: UUID
    name: str
    email: str
    project_role: str | None
    account_status: Literal["active", "inactive"]
    is_owner: bool
    is_current_member: bool
    member_since: datetime | None
    avatar_ref: str | None = None
    metrics: ProjectMemberPerformanceMetrics


class ProjectPerformanceTotals(_AAPModel):
    submitted_tasks: ProjectPerformanceMetric
    approved_task_outcomes: ProjectPerformanceMetric
    first_review_pass_rate: ProjectPerformanceMetric
    recorded_time_minutes: ProjectPerformanceMetric
    current_backlog: ProjectPerformanceMetric
    review_decisions: ProjectPerformanceMetric
    approvals: ProjectPerformanceMetric
    rejections: ProjectPerformanceMetric
    review_backlog: ProjectPerformanceMetric
    annotated_images: ProjectPerformanceMetric
    retained_objects: ProjectPerformanceMetric


class ProjectPerformanceTrendPoint(_AAPModel):
    date: str
    submitted_tasks: int | None
    approved_task_outcomes: int | None
    review_decisions: int | None


class ProjectPerformanceBreakdown(_AAPModel):
    reason_type: str | None = None
    class_name: str | None = None
    count: int
    pct: float | None = None


class ProjectPerformanceSourceBreakdown(_AAPModel):
    source: str
    count: int
    pct: float | None = None


class ProjectPerformanceGeometryBreakdown(_AAPModel):
    annotation_type: str
    count: int
    pct: float | None = None


class ProjectPerformanceEvidenceItem(_AAPModel):
    id: str
    at: datetime
    action: str
    task_id: UUID | None = None
    task_display_id: str | None = None
    detail: str | None = None
    contributor_name: str | None = None


class ProjectMembersPerformance(_AAPModel):
    scope: ProjectPerformanceScope
    coverage: ProjectPerformanceCoverage
    project_totals: ProjectPerformanceTotals
    items: list[ProjectMemberPerformance]
    next_cursor: str | None = None


class ProjectMemberPerformanceDetail(_AAPModel):
    scope: ProjectPerformanceScope
    coverage: ProjectPerformanceCoverage
    member: ProjectMemberPerformance
    trend: list[ProjectPerformanceTrendPoint]
    reject_reasons: list[ProjectPerformanceBreakdown]
    class_distribution: list[ProjectPerformanceBreakdown]
    source_distribution: list[ProjectPerformanceSourceBreakdown]
    geometry_distribution: list[ProjectPerformanceGeometryBreakdown]
    evidence: list[ProjectPerformanceEvidenceItem]
    evidence_next_cursor: str | None = None


class ProjectMemberPerformanceEvents(_AAPModel):
    scope: ProjectPerformanceScope
    items: list[ProjectPerformanceEvidenceItem]
    next_cursor: str | None = None
