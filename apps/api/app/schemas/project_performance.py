"""Project scoped member performance API contracts.

Metrics deliberately carry their unit and coverage.  A missing value stays
``null`` so an incomplete historical source cannot be mistaken for zero.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PerformanceWorkType = Literal["annotation", "review"]
PerformanceAccountStatus = Literal["all", "active", "inactive"]
PerformanceCoverageState = Literal["complete", "partial", "unknown"]
PerformanceMetricUnit = Literal[
    "tasks", "objects", "images", "decisions", "minutes", "percent"
]


class PerformanceScope(BaseModel):
    from_: datetime = Field(alias="from")
    to: datetime
    timezone: str
    as_of: datetime

    model_config = ConfigDict(populate_by_name=True)


class PerformanceCoverage(BaseModel):
    state: PerformanceCoverageState
    source: str
    detail: str | None = None


class PerformanceMetric(BaseModel):
    value: int | float | None
    unit: PerformanceMetricUnit
    numerator: int | None = None
    denominator: int | None = None
    coverage: PerformanceCoverageState | None = None


class PerformanceMemberMetrics(BaseModel):
    submitted_tasks: PerformanceMetric
    resubmissions: PerformanceMetric
    contributed_tasks: PerformanceMetric
    annotated_images: PerformanceMetric
    retained_objects: PerformanceMetric
    approved_task_outcomes: PerformanceMetric
    first_review_pass_rate: PerformanceMetric
    recorded_time_minutes: PerformanceMetric
    current_backlog: PerformanceMetric
    review_decisions: PerformanceMetric
    approvals: PerformanceMetric
    rejections: PerformanceMetric
    reviewed_tasks: PerformanceMetric
    recorded_review_minutes: PerformanceMetric
    review_backlog: PerformanceMetric


class PerformanceMember(BaseModel):
    user_id: UUID
    name: str
    email: str
    project_role: str | None
    account_status: Literal["active", "inactive"]
    is_owner: bool
    is_current_member: bool
    member_since: datetime | None
    avatar_ref: str | None = None
    metrics: PerformanceMemberMetrics


class PerformanceTotals(BaseModel):
    submitted_tasks: PerformanceMetric
    approved_task_outcomes: PerformanceMetric
    first_review_pass_rate: PerformanceMetric
    recorded_time_minutes: PerformanceMetric
    current_backlog: PerformanceMetric
    review_decisions: PerformanceMetric
    approvals: PerformanceMetric
    rejections: PerformanceMetric
    review_backlog: PerformanceMetric
    annotated_images: PerformanceMetric
    retained_objects: PerformanceMetric


class PerformanceTrendPoint(BaseModel):
    date: str
    submitted_tasks: int | None
    approved_task_outcomes: int | None
    review_decisions: int | None


class PerformanceBreakdown(BaseModel):
    reason_type: str | None = None
    class_name: str | None = None
    count: int
    pct: float | None = None


class PerformanceSourceBreakdown(BaseModel):
    source: str
    count: int
    pct: float | None = None


class PerformanceGeometryBreakdown(BaseModel):
    annotation_type: str
    count: int
    pct: float | None = None


class PerformanceEvidenceItem(BaseModel):
    id: str
    at: datetime
    action: str
    task_id: UUID | None = None
    task_display_id: str | None = None
    detail: str | None = None
    contributor_name: str | None = None


class PerformanceMembersResponse(BaseModel):
    scope: PerformanceScope
    coverage: PerformanceCoverage
    project_totals: PerformanceTotals
    items: list[PerformanceMember]
    next_cursor: str | None = None


class PerformanceMemberDetailResponse(BaseModel):
    scope: PerformanceScope
    coverage: PerformanceCoverage
    member: PerformanceMember
    trend: list[PerformanceTrendPoint]
    reject_reasons: list[PerformanceBreakdown]
    class_distribution: list[PerformanceBreakdown]
    source_distribution: list[PerformanceSourceBreakdown]
    geometry_distribution: list[PerformanceGeometryBreakdown]
    evidence: list[PerformanceEvidenceItem]
    evidence_next_cursor: str | None = None


class PerformanceEventsResponse(BaseModel):
    scope: PerformanceScope
    items: list[PerformanceEvidenceItem]
    next_cursor: str | None = None
