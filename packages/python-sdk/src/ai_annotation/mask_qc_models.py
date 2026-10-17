"""Typed request and response contracts for Mask QC and repairs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_annotation.models import _AAPModel
from ai_annotation.video_tracker_models import CocoRleMaskContent as CocoRleMaskContent

MaskQCSeverity = Literal["info", "warning", "blocker"]
MaskQCSeverityOverride = Literal["info", "warning", "blocker", "off"]
MaskQCIssueStatus = Literal["open", "resolved", "wont_fix", "stale"]
MaskQCIssuePatchStatus = Literal["open", "resolved", "wont_fix"]
MaskRepairKind = Literal[
    "delete_small_islands",
    "fill_small_holes",
    "resolve_same_class_overlap",
    "rerun_local_sam",
    "rerun_tracker",
]
MaskCompareBaseline = Literal[
    "previous_version",
    "tracker_candidate",
    "ai_candidate",
    "neighbor_keyframe",
]


class _MaskQCRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class MaskQCRunRequest(_MaskQCRequest):
    scope: Literal["project", "task_ids", "annotation_ids"]
    task_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    annotation_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    expected_versions: dict[str, int] = Field(default_factory=dict)

    @field_validator("task_ids", "annotation_ids")
    @classmethod
    def canonical_ids(cls, value: list[UUID]) -> list[UUID]:
        return sorted(set(value), key=str)

    @field_validator("expected_versions")
    @classmethod
    def canonical_expected_versions(cls, value: dict[str, int]) -> dict[str, int]:
        canonical: dict[str, int] = {}
        for annotation_id, version in value.items():
            parsed_id = str(UUID(annotation_id))
            if version < 1:
                raise ValueError("expected annotation versions must be positive")
            if parsed_id in canonical and canonical[parsed_id] != version:
                raise ValueError("duplicate expected annotation version")
            canonical[parsed_id] = version
        return dict(sorted(canonical.items()))

    @model_validator(mode="after")
    def scope_shape(self) -> MaskQCRunRequest:
        if self.scope == "project" and (self.task_ids or self.annotation_ids):
            raise ValueError("project scope cannot include task_ids or annotation_ids")
        if self.scope == "task_ids" and (not self.task_ids or self.annotation_ids):
            raise ValueError("task_ids scope requires only task_ids")
        if self.scope == "annotation_ids" and (
            not self.annotation_ids or self.task_ids
        ):
            raise ValueError("annotation_ids scope requires only annotation_ids")
        return self


class MaskQCIssuePatch(_MaskQCRequest):
    status: MaskQCIssuePatchStatus


class MaskRepairAction(_MaskQCRequest):
    issue_id: UUID
    kind: MaskRepairKind
    backend_id: UUID | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    model_key: str | None = Field(default=None, min_length=1, max_length=80)
    from_frame: int | None = Field(default=None, ge=0)
    to_frame: int | None = Field(default=None, ge=0)
    direction: Literal["forward", "backward", "bidirectional"] | None = None
    segment_id: UUID | None = None
    allow_bbox_fallback: bool = False
    text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def kind_options(self) -> MaskRepairAction:
        ai_options = (
            any(
                value is not None
                for value in (
                    self.backend_id,
                    self.model_id,
                    self.model_key,
                    self.from_frame,
                    self.to_frame,
                    self.direction,
                    self.segment_id,
                    self.text,
                )
            )
            or self.allow_bbox_fallback
        )
        if self.kind == "rerun_local_sam":
            if self.backend_id is None:
                raise ValueError("rerun_local_sam requires backend_id")
            if (
                any(
                    value is not None
                    for value in (
                        self.model_key,
                        self.from_frame,
                        self.to_frame,
                        self.direction,
                        self.segment_id,
                        self.text,
                    )
                )
                or self.allow_bbox_fallback
            ):
                raise ValueError("rerun_local_sam received tracker-only options")
        elif self.kind == "rerun_tracker":
            required = (
                self.backend_id,
                self.model_id,
                self.model_key,
                self.from_frame,
                self.to_frame,
                self.direction,
            )
            if any(value is None for value in required):
                raise ValueError("rerun_tracker requires backend/model/window options")
            if self.from_frame is not None and self.to_frame is not None:
                if self.from_frame > self.to_frame:
                    raise ValueError("from_frame must be <= to_frame")
        elif ai_options:
            raise ValueError("deterministic repairs do not accept backend options")
        return self


class MaskRepairDryRunRequest(_MaskQCRequest):
    actions: list[MaskRepairAction] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_issue_actions(self) -> MaskRepairDryRunRequest:
        issue_ids = [action.issue_id for action in self.actions]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("an issue may only appear once in a repair plan")
        return self


class MaskRepairExecuteRequest(_MaskQCRequest):
    receipt: str = Field(
        min_length=32,
        max_length=128,
        pattern=r"^mrp_[A-Za-z0-9_-]+$",
        repr=False,
    )
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MaskRepairRollbackRequest(_MaskQCRequest):
    expected_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MaskQCRun(_AAPModel):
    id: UUID
    project_id: UUID
    async_job_id: UUID | None
    status: str
    progress_pct: int
    config_revision: int
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_versions: dict[str, int]
    summary: dict[str, Any]
    created_at: datetime
    completed_at: datetime | None
    reused: bool = False


class MaskQCRegionBBox(_AAPModel):
    x0: float
    y0: float
    x1: float
    y1: float


class MaskQCIssue(_AAPModel):
    id: UUID
    run_id: UUID | None
    last_seen_run_id: UUID | None
    project_id: UUID
    task_id: UUID
    annotation_id: UUID
    annotation_version: int
    related_annotation_ids: list[UUID]
    source_versions: dict[str, int]
    code: str
    severity: MaskQCSeverity
    status: str
    effective_status: MaskQCIssueStatus
    frame_start: int | None
    frame_end: int | None
    metric: dict[str, Any]
    threshold: dict[str, Any]
    region_bbox: MaskQCRegionBBox | None
    region_mask_ref: dict[str, Any] | None
    region_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: dict[str, Any]
    suggestion: str | None
    resolved_by_id: UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MaskQCIssuePage(_AAPModel):
    items: list[MaskQCIssue]
    next_cursor: str | None = None


class TaskMaskQCSummary(_AAPModel):
    task_id: UUID
    run_id: UUID | None
    qc_digest: str | None
    source_snapshot_digest: str | None
    status: Literal[
        "not_applicable",
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "stale",
    ]
    progress_pct: int
    counts: dict[str, int] = Field(default_factory=dict)
    blocking: bool = False


class MaskCompareSide(_AAPModel):
    annotation_id: UUID
    annotation_version: int
    frame_index: int | None = None
    source: str
    state: str | None = None
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: tuple[int, int]
    content_path: str
    candidate_job_id: UUID | None = None
    candidate_digest: str | None = None
    candidate_instance_id: str | None = None


class MaskCompareMetrics(_AAPModel):
    current_area_pixels: int
    baseline_area_pixels: int
    intersection_pixels: int
    union_pixels: int
    changed_pixels: int
    added_pixels: int
    removed_pixels: int
    iou_numerator: int
    iou_denominator: int
    dice_numerator: int
    dice_denominator: int


class MaskCompareResult(_AAPModel):
    baseline_kind: MaskCompareBaseline
    current: MaskCompareSide
    baseline: MaskCompareSide
    metrics: MaskCompareMetrics
    loss: list[str] = Field(default_factory=list)


class MaskRepairPlanItem(_AAPModel):
    issue_id: UUID
    task_id: UUID | None = None
    annotation_ids: list[UUID] = Field(default_factory=list)
    kind: MaskRepairKind
    frame_index: int | None = None
    source_versions: dict[str, int] = Field(default_factory=dict)
    changed_pixels: int = 0
    mutation_count: int = 0
    candidate_count: int = 0
    scope_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    skip_code: str | None = None
    skip_detail: str | None = None


class MaskRepairPlanSummary(_AAPModel):
    action_count: int
    executable_count: int
    skipped_count: int
    mutation_count: int
    candidate_count: int
    changed_pixels: int
    shard_count: int


class MaskRepairDryRunResult(_AAPModel):
    receipt: str = Field(repr=False)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    items: list[MaskRepairPlanItem]
    summary: MaskRepairPlanSummary


class MaskRepairBatch(_AAPModel):
    id: UUID
    project_id: UUID
    async_job_id: UUID | None
    rollback_async_job_id: UUID | None
    status: str
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: dict[str, Any]
    result: dict[str, Any]
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_expires_at: datetime
    rollback_expires_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
    rolled_back_at: datetime | None
