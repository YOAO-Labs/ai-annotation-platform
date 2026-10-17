"""Typed contracts for point-cloud quality scans, review, and governance."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_annotation.point_cloud_quality_config_models import (
    PointCloudQualityConfig,
    PointCloudQualityConfigRead,
    PointCloudQualityGovernanceConfig,
    PointCloudQualityGovernanceConfigRead,
    PointCloudQualityRuleCode,
    PointCloudQualityThresholdConfig,
    PointCloudQualityThresholdConfigRead,
    PointCloudQualityThresholdOverride,
    PointCloudQualityThresholdOverrideRead,
)

PointCloudQualitySeverity = Literal["blocker", "warning", "info"]
PointCloudQualityIssueStatus = Literal["open", "resolved", "wont_fix", "stale"]
PointCloudQualityReviewVerdict = Literal[
    "confirmed", "false_positive", "accepted_exception", "uncertain"
]

__all__ = [
    "PointCloudQualityConfig",
    "PointCloudQualityConfigRead",
    "PointCloudQualityGovernanceConfig",
    "PointCloudQualityGovernanceConfigRead",
    "PointCloudQualityIssue",
    "PointCloudQualityIssuePage",
    "PointCloudQualityIssuePatch",
    "PointCloudQualityIssueStatus",
    "PointCloudQualityLocator",
    "PointCloudQualityMetricSummary",
    "PointCloudQualityRuleCode",
    "PointCloudQualityRun",
    "PointCloudQualityRunRequest",
    "PointCloudQualitySeverity",
    "PointCloudQualityThresholdConfig",
    "PointCloudQualityThresholdConfigRead",
    "PointCloudQualityThresholdOverride",
    "PointCloudQualityThresholdOverrideRead",
    "PointCloudQualityReviewVerdict",
    "PointCloudQualityEvaluation",
    "PointCloudQualityEvaluationClassSummary",
    "PointCloudQualityEvaluationListItem",
    "PointCloudQualityEvaluationPage",
    "PointCloudQualityEvaluationRuleSummary",
    "PointCloudQualityEvaluationSummary",
    "PointCloudQualityEvaluationTarget",
]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow", from_attributes=True)


class PointCloudQualityRunRequest(_RequestModel):
    scope: Literal["project", "scene_ids", "task_ids", "annotation_ids"]
    scene_ids: list[UUID] = Field(default_factory=list, max_length=500)
    task_ids: list[UUID] = Field(default_factory=list, max_length=2000)
    annotation_ids: list[UUID] = Field(default_factory=list, max_length=5000)
    expected_versions: dict[str, int] = Field(default_factory=dict)

    @field_validator("scene_ids", "task_ids", "annotation_ids")
    @classmethod
    def _canonical_ids(cls, value: list[UUID]) -> list[UUID]:
        return sorted(set(value), key=str)

    @field_validator("expected_versions")
    @classmethod
    def _canonical_versions(cls, value: dict[str, int]) -> dict[str, int]:
        return dict(sorted((str(UUID(key)), version) for key, version in value.items()))

    @model_validator(mode="after")
    def _validate_scope(self):
        ids = {
            "scene_ids": self.scene_ids,
            "task_ids": self.task_ids,
            "annotation_ids": self.annotation_ids,
        }
        if self.scope == "project" and any(ids.values()):
            raise ValueError("project scope cannot include ids")
        for name, values in ids.items():
            if self.scope == name and not values:
                raise ValueError(f"{name} scope requires ids")
            if self.scope != name and values:
                raise ValueError(f"{self.scope} scope cannot include {name}")
        return self


class PointCloudQualityIssuePatch(_RequestModel):
    status: Literal["open", "resolved", "wont_fix"]
    reason: str | None = Field(default=None, max_length=2000)
    review_verdict: PointCloudQualityReviewVerdict | None = None
    review_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_review_state(self):
        if self.status == "wont_fix" and not (self.reason or "").strip():
            raise ValueError("wont_fix requires a reason")
        if self.status == "open" and self.review_verdict is not None:
            raise ValueError("open issue cannot keep a review verdict")
        if self.review_verdict == "confirmed" and self.status != "resolved":
            raise ValueError("confirmed verdict requires resolved status")
        if (
            self.review_verdict in {"false_positive", "accepted_exception", "uncertain"}
            and self.status != "wont_fix"
        ):
            raise ValueError(f"{self.review_verdict} verdict requires wont_fix status")
        return self


class PointCloudQualityRun(_ResponseModel):
    id: UUID
    project_id: UUID
    async_job_id: UUID | None
    status: Literal["pending", "running", "completed", "failed", "cancelled", "stale"]
    progress_pct: int
    scope_json: dict[str, Any]
    config_revision: int
    config_digest: str
    source_snapshot_digest: str
    summary: dict[str, Any]
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    reused: bool = False


class PointCloudQualityLocator(_ResponseModel):
    scene_id: UUID
    frame_index: int | None = None
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    scene_track_id: UUID | None = None
    camera: str | None = None
    auxiliary_layers: list[str] = Field(default_factory=list)


class PointCloudQualityIssue(_ResponseModel):
    id: UUID
    run_id: UUID | None
    last_seen_run_id: UUID | None
    project_id: UUID
    scene_id: UUID
    task_id: UUID | None
    annotation_id: UUID | None
    annotation_version: int | None
    scene_track_id: UUID | None
    track_revision: int | None
    related_annotation_ids: list[UUID]
    source_versions: dict[str, int]
    class_name: str | None
    code: str
    rule_version: int
    severity: PointCloudQualitySeverity
    status: PointCloudQualityIssueStatus
    frame_start: int | None
    frame_end: int | None
    metric: dict[str, Any]
    threshold: dict[str, Any]
    evidence: dict[str, Any]
    locator: PointCloudQualityLocator
    suggested_command: str | None
    resolution_reason: str | None
    resolved_by_id: UUID | None
    resolved_at: datetime | None
    review_verdict: PointCloudQualityReviewVerdict | None
    review_note: str | None
    reviewed_by_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PointCloudQualityIssuePage(_ResponseModel):
    items: list[PointCloudQualityIssue]
    total: int


class PointCloudQualityMetricSummary(_ResponseModel):
    sample_count: int
    triggered_count: int
    confirmed: int
    false_positive: int
    accepted_exception: int
    uncertain: int
    decidable_count: int
    observed_precision: float | None
    observed_false_positive_rate: float | None
    confirmed_retention: float | None


class PointCloudQualityEvaluationRuleSummary(_ResponseModel):
    code: str
    baseline: PointCloudQualityMetricSummary
    candidate: PointCloudQualityMetricSummary


class PointCloudQualityEvaluationClassSummary(_ResponseModel):
    class_name: str
    baseline: PointCloudQualityMetricSummary
    candidate: PointCloudQualityMetricSummary


class PointCloudQualityEvaluationTarget(_ResponseModel):
    code: str
    class_name: str | None
    status: Literal["insufficient_data", "hold", "promote"]
    reasons: list[str]
    baseline: PointCloudQualityMetricSummary
    candidate: PointCloudQualityMetricSummary


class PointCloudQualityEvaluationSummary(_ResponseModel):
    metric_contract: dict[str, str]
    sample_count: int
    baseline: PointCloudQualityMetricSummary
    candidate: PointCloudQualityMetricSummary
    rules: list[PointCloudQualityEvaluationRuleSummary]
    classes: list[PointCloudQualityEvaluationClassSummary]
    changed_targets: list[PointCloudQualityEvaluationTarget]


class PointCloudQualityEvaluation(_ResponseModel):
    id: UUID
    project_id: UUID
    created_by_id: UUID | None
    baseline_config_revision: int
    baseline_config_digest: str
    baseline_config_snapshot: dict[str, Any]
    candidate_config_digest: str
    candidate_config_snapshot: dict[str, Any]
    cutoff_at: datetime
    sample_count: int
    summary: PointCloudQualityEvaluationSummary
    gate_status: Literal["insufficient_data", "hold", "promote"]
    gate_reasons: list[dict[str, Any]]
    promoted_by_id: UUID | None
    promoted_at: datetime | None
    promoted_config_revision: int | None
    created_at: datetime


class PointCloudQualityEvaluationListItem(_ResponseModel):
    id: UUID
    project_id: UUID
    created_by_id: UUID | None
    baseline_config_revision: int
    baseline_config_digest: str
    candidate_config_digest: str
    cutoff_at: datetime
    sample_count: int
    summary: PointCloudQualityEvaluationSummary
    gate_status: Literal["insufficient_data", "hold", "promote"]
    gate_reasons: list[dict[str, Any]]
    promoted_by_id: UUID | None
    promoted_at: datetime | None
    promoted_config_revision: int | None
    created_at: datetime


class PointCloudQualityEvaluationPage(_ResponseModel):
    items: list[PointCloudQualityEvaluationListItem]
    total: int
