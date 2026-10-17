"""Typed contracts for video tracker jobs and candidate review."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_annotation.models import _AAPModel

VideoTrackerStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "pending_review",
    "partially_reviewed",
    "accepted",
    "discarded",
]
VideoTrackerDirection = Literal["forward", "backward", "bidirectional"]
VideoTrackerJobKind = Literal["tracking", "correction"]


class _VideoTrackerRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class VideoTrackerExemplar(_VideoTrackerRequest):
    """Normalized xyxy example box used by text driven tracking."""

    bbox: list[float] = Field(min_length=4, max_length=4)
    label: bool = True

    @field_validator("bbox")
    @classmethod
    def _normalized_box(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("bbox values must be finite")
        x1, y1, x2, y2 = value
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("bbox must be normalized xyxy coordinates")
        return value


class VideoTrackerCreateRequest(_VideoTrackerRequest):
    """Request body shared by task-level tracking and legacy propagation."""

    from_frame: int = Field(ge=0)
    to_frame: int = Field(ge=0)
    model_key: str = Field(default="mock_bbox", min_length=1, max_length=80)
    direction: VideoTrackerDirection = "forward"
    segment_id: UUID | None = None
    prompt: dict[str, Any] = Field(default_factory=dict)
    sam_variant: str | None = None
    text: str | None = Field(default=None, max_length=500)
    exemplars: list[VideoTrackerExemplar] | None = None
    output_geometry: Literal["bbox", "polygon", "mask"] | None = None
    source_annotation_id: UUID | None = None
    source_annotation_ids: list[UUID] | None = None
    target_class_name: str | None = Field(default=None, max_length=100)
    target_tool_unit_id: str | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def _ordered_frames(self) -> "VideoTrackerCreateRequest":
        if self.from_frame > self.to_frame:
            raise ValueError("from_frame must be <= to_frame")
        return self


class VideoMaskCorrectionRequest(_VideoTrackerRequest):
    """Versioned correction request; preserve the source version and digest."""

    correction_frame: int = Field(ge=0)
    from_frame: int = Field(ge=0)
    to_frame: int = Field(ge=0)
    model_key: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    backend_id: UUID
    direction: VideoTrackerDirection
    segment_id: UUID | None = None
    source_annotation_version: int = Field(ge=1)
    corrected_mask_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allow_bbox_fallback: bool = False
    text: str | None = Field(default=None, max_length=500)
    sam_variant: str | None = None

    @model_validator(mode="after")
    def _valid_window(self) -> "VideoMaskCorrectionRequest":
        if self.from_frame > self.to_frame:
            raise ValueError("from_frame must be <= to_frame")
        if not self.from_frame <= self.correction_frame <= self.to_frame:
            raise ValueError("correction_frame must be inside the requested window")
        if self.direction == "forward" and self.correction_frame >= self.to_frame:
            raise ValueError("forward correction requires a later target frame")
        if self.direction == "backward" and self.correction_frame <= self.from_frame:
            raise ValueError("backward correction requires an earlier target frame")
        if self.direction == "bidirectional" and not (
            self.from_frame < self.correction_frame < self.to_frame
        ):
            raise ValueError("bidirectional correction needs frames on both sides")
        return self


class VideoTrackerDecisionRequest(_VideoTrackerRequest):
    """Revision guarded decision for an explicit instance/window or QC issue."""

    instance_ids: list[str] | None = Field(default=None, min_length=1, max_length=256)
    from_frame: int | None = Field(default=None, ge=0)
    to_frame: int | None = Field(default=None, ge=0)
    qc_issue_id: UUID | None = None
    candidate_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]
    expected_source_versions: dict[UUID, int] = Field(default_factory=dict)
    job_revision: int = Field(ge=1)
    override_manual: bool = False

    @field_validator("instance_ids")
    @classmethod
    def _normalized_instance_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("instance_ids must be non-empty and at most 128 chars")
        if len(set(normalized)) != len(normalized):
            raise ValueError("instance_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def _exclusive_selector(self) -> "VideoTrackerDecisionRequest":
        issue_selector = (
            self.qc_issue_id is not None or self.candidate_digest is not None
        )
        slice_selector = any(
            value is not None
            for value in (self.instance_ids, self.from_frame, self.to_frame)
        )
        if issue_selector:
            if self.qc_issue_id is None or self.candidate_digest is None:
                raise ValueError(
                    "qc_issue_id and candidate_digest must be provided together"
                )
            if slice_selector:
                raise ValueError("QC issue and instance/window selectors are exclusive")
            return self
        if (
            self.instance_ids is None
            or self.from_frame is None
            or self.to_frame is None
        ):
            raise ValueError(
                "instance_ids, from_frame and to_frame are required together"
            )
        if self.from_frame > self.to_frame:
            raise ValueError("from_frame must be <= to_frame")
        return self


class VideoTrackerJob(_AAPModel):
    id: UUID
    task_id: UUID
    dataset_item_id: UUID
    annotation_id: UUID | None = None
    touched_annotation_ids: list[UUID] | None = None
    segment_id: UUID | None = None
    created_by: UUID | None = None
    status: VideoTrackerStatus
    job_kind: VideoTrackerJobKind = "tracking"
    track_id_snapshot: str | None = None
    correction_frame: int | None = None
    revision: int = 1
    review_replayed: bool = False
    model_key: str
    direction: VideoTrackerDirection
    from_frame: int
    to_frame: int
    prompt: dict[str, Any] = Field(default_factory=dict)
    event_channel: str
    celery_task_id: str | None = None
    cancel_requested_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class VideoTrackerJobListItem(_AAPModel):
    id: UUID
    task_id: UUID
    project_id: UUID
    project_name: str | None = None
    project_display_id: str | None = None
    dataset_item_id: UUID
    annotation_id: UUID | None = None
    segment_id: UUID | None = None
    created_by: UUID | None = None
    status: VideoTrackerStatus
    model_key: str
    direction: str
    from_frame: int
    to_frame: int
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class VideoTrackerJobCounts(_AAPModel):
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    pending_review: int = 0
    partially_reviewed: int = 0
    accepted: int = 0
    discarded: int = 0


class VideoTrackerJobsResponse(_AAPModel):
    items: list[VideoTrackerJobListItem]
    next_cursor: str | None = None
    counts: VideoTrackerJobCounts


class VideoTrackerPreviewResult(_AAPModel):
    frame_index: int
    geometry: dict[str, Any] | None = None
    confidence: float | None = None
    outside: bool = False
    instance_id: str | None = None
    primary: bool = False
    candidate_key: str
    geometry_digest: str
    source_annotation_id: UUID | None = None
    target_annotation_id: UUID | None = None
    manual_protected: bool = False


class VideoTrackerJobPreview(_AAPModel):
    job_id: UUID
    status: VideoTrackerStatus
    annotation_id: UUID | None = None
    job_kind: VideoTrackerJobKind = "tracking"
    correction_frame: int | None = None
    direction: VideoTrackerDirection | None = None
    from_frame: int
    to_frame: int
    fallback_reason: str | None = None
    seed_mode: Literal["native_mask", "bbox"] | None = None
    protect_manual: bool = False
    results: list[VideoTrackerPreviewResult] = Field(default_factory=list)
    grid_step: int = 1
    output_geometry: str = "bbox"
    job_revision: int = 1
    expected_source_versions: dict[UUID, int] = Field(default_factory=dict)
    candidate_total: int = 0
    candidate_pending: int = 0
    candidate_accepted: int = 0
    candidate_rejected: int = 0


class CocoRleMaskContent(_AAPModel):
    """Materialized COCO RLE returned as JSON by the mask-content endpoint."""

    encoding: Literal["coco_rle"]
    size: tuple[int, int]
    counts: list[int]
