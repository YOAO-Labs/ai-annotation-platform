from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aap_workspace.point_quality_configuration_values import (
    PointCloudQualityConfig as _PointCloudQualityConfig,
)


class PointCloudQualityRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class PointCloudQualityRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    async_job_id: UUID | None
    status: str
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


class PointCloudQualityIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    severity: Literal["blocker", "warning", "info"]
    status: Literal["open", "resolved", "wont_fix", "stale"]
    frame_start: int | None
    frame_end: int | None
    metric: dict[str, Any]
    threshold: dict[str, Any]
    evidence: dict[str, Any]
    locator: dict[str, Any]
    suggested_command: str | None
    resolution_reason: str | None
    resolved_by_id: UUID | None
    resolved_at: datetime | None
    review_verdict: (
        Literal["confirmed", "false_positive", "accepted_exception", "uncertain"] | None
    )
    review_note: str | None
    reviewed_by_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PointCloudQualityIssuePage(BaseModel):
    items: list[PointCloudQualityIssueOut]
    total: int


class PointCloudQualityIssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "resolved", "wont_fix"]
    reason: str | None = Field(default=None, max_length=2000)
    review_verdict: (
        Literal["confirmed", "false_positive", "accepted_exception", "uncertain"] | None
    ) = None
    review_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _require_wont_fix_reason(self):
        if self.status == "wont_fix" and not (self.reason or "").strip():
            raise ValueError("wont_fix requires a reason")
        if self.status == "open" and self.review_verdict is not None:
            raise ValueError("open issue cannot keep a review verdict")
        if self.review_verdict == "confirmed" and self.status != "resolved":
            raise ValueError("confirmed verdict requires resolved status")
        if (
            self.review_verdict
            in {
                "false_positive",
                "accepted_exception",
                "uncertain",
            }
            and self.status != "wont_fix"
        ):
            raise ValueError(f"{self.review_verdict} verdict requires wont_fix status")
        return self


class PointCloudQualityEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_config: _PointCloudQualityConfig


class PointCloudQualityEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    summary: dict[str, Any]
    gate_status: Literal["insufficient_data", "hold", "promote"]
    gate_reasons: list[dict[str, Any]]
    promoted_by_id: UUID | None
    promoted_at: datetime | None
    promoted_config_revision: int | None
    created_at: datetime


class PointCloudQualityEvaluationListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by_id: UUID | None
    baseline_config_revision: int
    baseline_config_digest: str
    candidate_config_digest: str
    cutoff_at: datetime
    sample_count: int
    summary: dict[str, Any]
    gate_status: Literal["insufficient_data", "hold", "promote"]
    gate_reasons: list[dict[str, Any]]
    promoted_by_id: UUID | None
    promoted_at: datetime | None
    promoted_config_revision: int | None
    created_at: datetime


class PointCloudQualityEvaluationPage(BaseModel):
    items: list[PointCloudQualityEvaluationListItem]
    total: int
