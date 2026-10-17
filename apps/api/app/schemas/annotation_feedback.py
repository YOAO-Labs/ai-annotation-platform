"""I18 · AnnotationFeedback 统一反馈表 schema."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas._jsonb_types import CanvasDrawing, Mention
from app.schemas.discussion_actions import DiscussionActions

FeedbackKind = Literal["issue", "comment", "reject", "bug"]
FeedbackAnchorType = Literal["project", "task", "annotation", "pixel", "point_cloud"]
FeedbackStatus = Literal["open", "resolved", "wont_fix"]
FeedbackSeverity = Literal["info", "warn", "blocker"]


class MaskFeedbackCompareLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_kind: Literal[
        "previous_version",
        "tracker_candidate",
        "ai_candidate",
        "neighbor_keyframe",
    ]
    mode: Literal["overlay", "boundary", "xor", "added", "removed"]
    current_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_job_id: UUID | None = None
    candidate_job_revision: int | None = Field(default=None, ge=1)
    candidate_digest: str | None = None
    candidate_instance_id: str | None = None

    @model_validator(mode="after")
    def _validate_tracker_locator(self):
        tracker_values = (
            self.candidate_job_id,
            self.candidate_job_revision,
            self.candidate_digest,
        )
        if self.baseline_kind == "tracker_candidate" and any(
            value is None for value in tracker_values
        ):
            raise ValueError("tracker locator requires job, revision and digest")
        return self


class FeedbackVideoFrameRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_frame: int = Field(strict=True, ge=0)
    to_frame: int = Field(strict=True, ge=0)


class FeedbackVideoViewport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    center_x: float = Field(strict=True, allow_inf_nan=False)
    center_y: float = Field(strict=True, allow_inf_nan=False)
    zoom: float = Field(strict=True, allow_inf_nan=False, gt=0)


class FeedbackVideoTimelineWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_: float = Field(alias="from", strict=True, allow_inf_nan=False, ge=0)
    to: float = Field(strict=True, allow_inf_nan=False, ge=0)


class FeedbackVideoContext(BaseModel):
    """Versioned capture of source-frame, object and view context for a pixel anchor."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    track_id: str | None = Field(default=None, strict=True)
    annotation_version: int | None = Field(default=None, strict=True, ge=1)
    frame_range: FeedbackVideoFrameRange | None = None
    viewport: FeedbackVideoViewport | None = None
    timeline_window: FeedbackVideoTimelineWindow | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value):
        # Literal[1] alone also accepts True and 1.0 in Pydantic.
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value


class FeedbackAnchorPosition(BaseModel):
    """Pixel or 3D quality anchor position and durable locator."""

    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    frame: int | None = Field(default=None, ge=0)
    region_bbox: tuple[float, float, float, float] | None = None
    region_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    boundary_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mask_qc_issue_id: UUID | None = None
    point_cloud_quality_issue_id: UUID | None = None
    scene_id: UUID | None = None
    scene_track_id: UUID | None = None
    auxiliary_layers: list[str] = Field(default_factory=list, max_length=20)
    compare_locator: MaskFeedbackCompareLocator | None = None
    video_context: FeedbackVideoContext | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_video_source_frame(cls, value):
        if isinstance(value, dict) and value.get("video_context") is not None:
            if type(value.get("frame")) is not int or value["frame"] < 0:
                raise ValueError("video_context requires a non-negative integer frame")
        return value

    @model_validator(mode="after")
    def _validate_region_anchor(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("x and y must be provided together")
        if self.region_bbox is not None:
            x0, y0, x1, y1 = self.region_bbox
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError(
                    "region_bbox must be a normalized non-empty half-open box"
                )
        if self.boundary_digest is not None and self.region_digest is None:
            raise ValueError("boundary_digest requires region_digest")
        if self.compare_locator is not None and self.mask_qc_issue_id is None:
            raise ValueError("compare_locator requires mask_qc_issue_id")
        if self.mask_qc_issue_id is not None and (self.x is None or self.y is None):
            raise ValueError("mask_qc_issue_id requires x and y")
        if self.point_cloud_quality_issue_id is not None:
            if self.mask_qc_issue_id is not None:
                raise ValueError("quality anchors cannot mix 2D and 3D issues")
            if self.scene_id is None or self.frame is None:
                raise ValueError("point cloud quality anchor requires scene and frame")
        return self


class AnnotationFeedbackCreate(BaseModel):
    kind: FeedbackKind
    anchor_type: FeedbackAnchorType
    project_id: UUID
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    anchor_position: FeedbackAnchorPosition | None = None
    severity: FeedbackSeverity | None = None
    title: str | None = Field(default=None, max_length=500)
    body: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[Mention] = Field(default_factory=list)
    canvas_drawing: CanvasDrawing | None = None
    thread_parent_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_anchor(self):
        video_context = (
            self.anchor_position.video_context if self.anchor_position else None
        )
        if video_context is not None:
            if self.anchor_type != "pixel":
                raise ValueError("video_context requires a pixel anchor")
            if self.anchor_position.frame is None:
                raise ValueError("video_context requires frame")
            if (
                video_context.annotation_version is not None
                and self.annotation_id is None
            ):
                raise ValueError("annotation_version requires annotation_id")
        # 同 DB CHECK 约束逻辑, 但提前到 pydantic 层给出更友好错误.
        if self.anchor_type == "project":
            if self.task_id or self.annotation_id or self.anchor_position:
                raise ValueError(
                    "project anchor must not carry task_id/annotation_id/anchor_position"
                )
        elif self.anchor_type == "task":
            if not self.task_id:
                raise ValueError("task anchor requires task_id")
            if self.annotation_id or self.anchor_position:
                raise ValueError(
                    "task anchor must not carry annotation_id/anchor_position"
                )
        elif self.anchor_type == "annotation":
            if not (self.task_id and self.annotation_id):
                raise ValueError(
                    "annotation anchor requires both task_id and annotation_id"
                )
            if self.anchor_position:
                raise ValueError("annotation anchor must not carry anchor_position")
        elif self.anchor_type == "pixel":
            if not (self.task_id and self.anchor_position):
                raise ValueError("pixel anchor requires task_id and anchor_position")
            if self.anchor_position.x is None or self.anchor_position.y is None:
                raise ValueError("pixel anchor requires x and y")
        elif self.anchor_type == "point_cloud":
            if not (self.task_id and self.anchor_position):
                raise ValueError(
                    "point_cloud anchor requires task_id and anchor_position"
                )
            if self.anchor_position.point_cloud_quality_issue_id is None:
                raise ValueError(
                    "point_cloud anchor requires point_cloud_quality_issue_id"
                )
        has_drawing = (
            self.canvas_drawing is not None and len(self.canvas_drawing.shapes) > 0
        )
        if self.canvas_drawing is not None:
            if not has_drawing:
                raise ValueError("canvas_drawing must contain at least one shape")
            if not (
                self.kind == "comment"
                and self.anchor_type == "task"
                and self.thread_parent_id is None
            ):
                raise ValueError("canvas_drawing requires a native root task comment")
        # Native task comments are the only task discussion source that does not
        # carry an annotation target.
        # Rich annotation/pixel/point-cloud callers historically support an
        # attachment-only body, so do not impose this rule on those records.
        if (
            self.kind == "comment"
            and self.anchor_type == "task"
            and not self.attachments
            and not self.body.strip()
            and not has_drawing
        ):
            raise ValueError(
                "task comments must contain text, an attachment, or a drawing"
            )
        if self.mentions and not (
            self.kind == "comment"
            and self.anchor_type == "task"
            and self.thread_parent_id is None
        ):
            raise ValueError("mentions require a native root task comment")
        return self


class AnnotationFeedbackPatch(BaseModel):
    status: FeedbackStatus | None = None
    severity: FeedbackSeverity | None = None
    title: str | None = Field(default=None, max_length=500)
    body: str | None = None
    mentions: list[Mention] | None = None


class AnnotationFeedbackReply(BaseModel):
    body: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[Mention] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_canvas_drawing(cls, value):
        if isinstance(value, dict) and value.get("canvas_drawing") is not None:
            raise ValueError("canvas_drawing is not supported for feedback replies")
        if isinstance(value, dict) and value.get("mentions"):
            raise ValueError("mentions are not supported for feedback replies")
        return value


class AnnotationFeedbackOut(BaseModel):
    id: UUID
    kind: FeedbackKind
    anchor_type: FeedbackAnchorType
    project_id: UUID
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    anchor_position: dict[str, Any] | None = None
    status: FeedbackStatus
    severity: FeedbackSeverity | None = None
    title: str | None = None
    body: str
    author_id: UUID
    author_name: str | None = None
    attachments: list[dict[str, Any]] = []
    mentions: list[Mention] = []
    canvas_drawing: CanvasDrawing | None = None
    thread_parent_id: UUID | None = None
    is_active: bool
    resolved_at: datetime | None = None
    resolved_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    actions: DiscussionActions = Field(default_factory=DiscussionActions)

    @field_validator("anchor_position", mode="before")
    @classmethod
    def _passthrough(cls, v):
        return v

    class Config:
        from_attributes = True


class AnnotationFeedbackListPage(BaseModel):
    items: list[AnnotationFeedbackOut]
    next_cursor: str | None = None
    total: int | None = None
    status_counts: dict[str, int] | None = None


class AnnotationFeedbackThreadPage(BaseModel):
    root: AnnotationFeedbackOut
    items: list[AnnotationFeedbackOut]
    next_cursor: str | None = None
    total: int


def serialize_feedback_anchor(
    payload: AnnotationFeedbackCreate,
) -> dict[str, Any] | None:
    """Encode public anchor keys while preserving legacy omission rules."""
    anchor = payload.anchor_position
    if anchor is None:
        return None
    value = anchor.model_dump(mode="json", by_alias=True)
    if anchor.video_context is None:
        value.pop("video_context", None)
    else:
        value["video_context"] = anchor.video_context.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    if payload.anchor_type == "pixel":
        for key in (
            "point_cloud_quality_issue_id",
            "scene_id",
            "scene_track_id",
            "auxiliary_layers",
        ):
            value.pop(key, None)
    elif payload.anchor_type == "point_cloud":
        value = {
            key: value[key]
            for key in (
                "frame",
                "point_cloud_quality_issue_id",
                "scene_id",
                "scene_track_id",
                "auxiliary_layers",
            )
        }
    return value
