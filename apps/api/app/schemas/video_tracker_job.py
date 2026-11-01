from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas._jsonb_types import CocoRleMaskRef


TrackerDirection = Literal["forward", "backward", "bidirectional"]
TrackerJobKind = Literal["tracking", "correction"]
TrackerJobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    # v0.21.28 · 候选/接受流。
    "pending_review",
    "partially_reviewed",
    "accepted",
    "discarded",
]


class TrackerExemplar(BaseModel):
    """v0.21.19 · text-driven 追踪 (sam3_video) 的单个视觉示例框。

    bbox: 归一化 xyxy [x1, y1, x2, y2]; label: True=正框(扩召回) / False=负框(排误检)。
    复用 sam3 图片侧 `Exemplar` 形状，沿用归一化坐标红线。
    """

    bbox: list[float]
    label: bool = True

    @model_validator(mode="after")
    def _validate_bbox(self) -> "TrackerExemplar":
        if len(self.bbox) != 4:
            raise ValueError("exemplar.bbox=[x1,y1,x2,y2] required (length 4)")
        if any(not math.isfinite(v) for v in self.bbox):
            raise ValueError("exemplar.bbox must be finite (no NaN/Inf)")
        x1, y1, x2, y2 = self.bbox
        if not (0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0):
            raise ValueError(
                "exemplar.bbox must be normalized [x1,y1,x2,y2] with 0≤x1<x2≤1 and 0≤y1<y2≤1"
            )
        return self


class VideoTrackerPropagateRequest(BaseModel):
    from_frame: int = Field(ge=0)
    to_frame: int = Field(ge=0)
    model_key: str = Field(default="mock_bbox", min_length=1, max_length=80)
    direction: TrackerDirection = "forward"
    segment_id: UUID | None = None
    prompt: dict[str, Any] = Field(default_factory=dict)
    sam_variant: str | None = (
        None  # v0.10.36 · 透传到 adapter context (缺省后端回退 tiny)
    )
    # v0.21.19 · text-driven 追踪 (sam3_video): 文本 query + 可选视觉示例框。显式字段,
    # 由 create_tracker_job 写进 prompt JSONB、adapter 透传到 backend /predict context。
    text: str | None = Field(default=None, max_length=500)
    exemplars: list[TrackerExemplar] | None = None
    output_geometry: Literal["bbox", "polygon", "mask"] | None = None
    # v0.22.1 · B · 无源检测 (画布级发起): source_annotation_id 缺省 = 无源, 新建轨迹类别
    # 由 target_class_name/target_tool_unit_id 显式指定。有源延展时留空 (从 path / 继承源)。
    source_annotation_id: UUID | None = None
    # v0.22.2 · M · 多选批量: 一次对 N 条已有轨迹批量延展 (各 obj_id ↔ 各源)。给出 (≥1 条)
    # 时走多源分支——各源写成带 source_annotation_id 的 seed、job.annotation_id 存 NULL (不认
    # 单主, 各源各回填); 单数 source_annotation_id 保留 (单源快捷/兼容)。
    source_annotation_ids: list[UUID] | None = None
    target_class_name: str | None = Field(default=None, max_length=100)
    target_tool_unit_id: str | None = Field(default=None, max_length=30)

    @field_validator("prompt")
    @classmethod
    def _prompt_must_be_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value or {})


class VideoMaskKeyframeSaveRequest(BaseModel):
    mask: CocoRleMaskRef
    source: Literal["manual", "prediction"] = "manual"
    occluded: bool = False
    attributes: dict[str, Any] | None = None


class VideoMaskKeyframeOperationRequest(BaseModel):
    operation: Literal["delete_keyframe", "mark_outside", "restore_held"]


class VideoMaskCorrectionRequest(BaseModel):
    correction_frame: int = Field(ge=0)
    from_frame: int = Field(ge=0)
    to_frame: int = Field(ge=0)
    model_key: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    backend_id: UUID
    direction: TrackerDirection
    segment_id: UUID | None = None
    source_annotation_version: int = Field(ge=1)
    corrected_mask_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allow_bbox_fallback: bool = False
    text: str | None = Field(default=None, max_length=500)
    sam_variant: str | None = None

    @model_validator(mode="after")
    def _valid_correction_window(self) -> "VideoMaskCorrectionRequest":
        if self.from_frame > self.to_frame:
            raise ValueError("from_frame must be <= to_frame")
        if not self.from_frame <= self.correction_frame <= self.to_frame:
            raise ValueError("correction_frame must be inside the requested window")
        if self.direction == "forward" and self.correction_frame >= self.to_frame:
            raise ValueError("forward correction requires a later target frame")
        if self.direction == "backward" and self.correction_frame <= self.from_frame:
            raise ValueError("backward correction requires an earlier target frame")
        if self.direction == "bidirectional" and (
            self.correction_frame <= self.from_frame
            or self.correction_frame >= self.to_frame
        ):
            raise ValueError(
                "bidirectional correction requires target frames on both sides"
            )
        return self


class VideoTrackerJobOut(BaseModel):
    id: UUID
    task_id: UUID
    dataset_item_id: UUID
    annotation_id: UUID | None = None
    # v0.22.2 · M · 本 job 接受后触及的轨迹 id (回填源 + 新建)。accept 时落进 job.prompt
    # (免 DB 迁移), 序列化时由 _hydrate_touched_from_prompt 提到顶层。未接受的 job → None。
    touched_annotation_ids: list[UUID] | None = None
    segment_id: UUID | None = None
    created_by: UUID | None = None
    status: TrackerJobStatus
    job_kind: TrackerJobKind = "tracking"
    track_id_snapshot: str | None = None
    correction_frame: int | None = None
    revision: int = 1
    review_replayed: bool = False
    model_key: str
    direction: TrackerDirection
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

    @model_validator(mode="after")
    def _hydrate_touched_from_prompt(self) -> "VideoTrackerJobOut":
        # v0.22.2 · M · touched_annotation_ids 存于 prompt JSONB (accept 时写), 序列化时提到
        # 顶层便于前端直接读 (无需再钻 prompt)。未接受的 job prompt 无此键 → 保持 None。
        if self.touched_annotation_ids is None:
            raw = (self.prompt or {}).get("touched_annotation_ids")
            if isinstance(raw, list):
                self.touched_annotation_ids = [UUID(str(x)) for x in raw]
        return self

    class Config:
        from_attributes = True


class VideoTrackerDecisionRequest(BaseModel):
    """Select and decide an explicit target/window slice of staged candidates."""

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
    def _unique_instance_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("instance_ids must contain non-empty values <= 128 chars")
        if len(set(normalized)) != len(normalized):
            raise ValueError("instance_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def _valid_window(self) -> "VideoTrackerDecisionRequest":
        region_selector = (
            self.qc_issue_id is not None or self.candidate_digest is not None
        )
        legacy_selector = any(
            value is not None
            for value in (self.instance_ids, self.from_frame, self.to_frame)
        )
        if region_selector:
            if self.qc_issue_id is None or self.candidate_digest is None:
                raise ValueError(
                    "qc_issue_id and candidate_digest must be provided together"
                )
            if legacy_selector:
                raise ValueError(
                    "QC issue and instance/window decision selectors are mutually exclusive"
                )
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
