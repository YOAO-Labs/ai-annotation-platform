"""Typed contracts for scene timelines and versioned track operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel

TemporalRole = Literal["keyframe", "derived", "sample"]
SceneTrackCommandKind = Literal[
    "split", "merge", "mark_absent", "resume", "terminate", "revert"
]
SceneTrackWriteCommandKind = Literal[
    "split", "merge", "mark_absent", "resume", "terminate"
]
LegacyTrackOperationKind = Literal["split", "merge"]


class _SceneTrackRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class SceneTrackCommandRequest(_SceneTrackRequest):
    kind: SceneTrackWriteCommandKind
    track_id: str = Field(min_length=1, max_length=64)
    secondary_track_id: str | None = Field(default=None, min_length=1, max_length=64)
    frame_index: int | None = Field(default=None, ge=0)
    resume_frame: int | None = Field(default=None, ge=0)
    source_annotation_id: UUID | None = None
    confirm_member_deactivation: bool = False

    @model_validator(mode="after")
    def validate_kind_fields(self) -> SceneTrackCommandRequest:
        if self.kind == "merge":
            if self.secondary_track_id is None:
                raise ValueError("secondary_track_id is required for merge")
            if self.secondary_track_id == self.track_id:
                raise ValueError("merge requires two different tracks")
        elif self.secondary_track_id is not None:
            raise ValueError("secondary_track_id is accepted only for merge")

        if self.kind in {"split", "mark_absent", "terminate"}:
            if self.frame_index is None:
                raise ValueError("frame_index is required for this command")
        elif self.kind == "resume":
            if self.resume_frame is None:
                raise ValueError("resume_frame is required for resume")
            if self.source_annotation_id is None:
                raise ValueError("source_annotation_id is required for resume")
        return self


class SceneTrackCommandExecuteRequest(SceneTrackCommandRequest):
    snapshot_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128)


class SceneTrackRevertRequest(_SceneTrackRequest):
    idempotency_key: str = Field(min_length=8, max_length=128)


class LegacyTrackOperationRequest(_SceneTrackRequest):
    operation: LegacyTrackOperationKind
    primary_track_id: str = Field(min_length=1, max_length=64)
    secondary_track_id: str | None = Field(default=None, min_length=1, max_length=64)
    split_after_frame: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> LegacyTrackOperationRequest:
        if self.operation == "split":
            if self.split_after_frame is None:
                raise ValueError("split_after_frame is required for split")
            if self.secondary_track_id is not None:
                raise ValueError("secondary_track_id is not accepted for split")
        else:
            if self.secondary_track_id is None:
                raise ValueError("secondary_track_id is required for merge")
            if self.secondary_track_id == self.primary_track_id:
                raise ValueError("merge requires two different tracks")
            if self.split_after_frame is not None:
                raise ValueError("split_after_frame is not accepted for merge")
        return self


class LegacyTrackOperationExecuteRequest(LegacyTrackOperationRequest):
    snapshot_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class SceneTimelineTrackOccurrence(_AAPModel):
    annotation_id: UUID
    source: str
    class_name: str
    temporal_role: TemporalRole = "sample"


class SceneTimelineFrame(_AAPModel):
    frame_index: int
    state: Literal["available", "unavailable", "missing"]
    task_id: UUID | None = None
    task_status: str | None = None
    annotation_count: int = 0
    selected_track: SceneTimelineTrackOccurrence | None = None
    selected_track_present: bool | None = None


class SceneTimeline(_AAPModel):
    summary_version: Literal[1] = 1
    scene_id: UUID | None = None
    scene_name: str | None = None
    current_frame_index: int | None = None
    scene_start_frame: int | None = None
    scene_end_frame: int | None = None
    populated_frame_count: int = 0
    window_start_frame: int | None = None
    window_end_frame: int | None = None
    frames: list[SceneTimelineFrame] = Field(default_factory=list)


class SceneTrackInterval(_AAPModel):
    id: UUID
    start_frame: int
    end_frame: int | None = None
    source: Literal["legacy_envelope", "manual", "imported", "derived"]
    version: int


class SceneTrackMemberSummary(_AAPModel):
    total: int
    by_temporal_role: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    keyframe_frames: list[int] = Field(default_factory=list)
    derived_frames: list[int] = Field(default_factory=list)
    sample_frames: list[int] = Field(default_factory=list)


class SceneTrackDetail(_AAPModel):
    contract_version: Literal[1] = 1
    id: UUID
    project_id: UUID
    scene_id: UUID
    scene_name: str | None = None
    track_id: str
    class_name: str
    presence_mode: Literal["inferred", "explicit"]
    attributes: dict[str, Any] = Field(default_factory=dict)
    attributes_meta: dict[str, Any] = Field(default_factory=dict)
    revision: int
    retired_at: datetime | None = None
    current_frame: int
    intervals: list[SceneTrackInterval] = Field(default_factory=list)
    members: SceneTrackMemberSummary
    available_commands: list[SceneTrackCommandKind] = Field(default_factory=list)


class SceneTrackDiagnosticIssue(_AAPModel):
    code: str
    track_id: str | None = None
    annotation_id: UUID | None = None
    frame_index: int | None = None


class SceneTrackDiagnosticReport(_AAPModel):
    contract_version: Literal[1] = 1
    scene_id: UUID
    track_count: int
    linked_member_count: int
    issue_counts: dict[str, int] = Field(default_factory=dict)
    issues: list[SceneTrackDiagnosticIssue] = Field(default_factory=list)
    truncated: bool = False


class SceneTrackCommandMemberImpact(_AAPModel):
    total: int = 0
    by_temporal_role: dict[str, int] = Field(default_factory=dict)
    frames: list[int] = Field(default_factory=list)
    requires_confirmation: bool = False


class SceneTrackCommandPreview(_AAPModel):
    contract_version: Literal[1] = 1
    kind: SceneTrackCommandKind
    scene_id: UUID
    scene_name: str | None = None
    track_id: str
    secondary_track_id: str | None = None
    frame_index: int | None = None
    resume_frame: int | None = None
    source_revisions: dict[str, int] = Field(default_factory=dict)
    before_intervals: dict[str, list[SceneTrackInterval]] = Field(default_factory=dict)
    after_intervals: dict[str, list[SceneTrackInterval]] = Field(default_factory=dict)
    affected_members: SceneTrackCommandMemberImpact
    snapshot_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class SceneTrackCommandResult(SceneTrackCommandPreview):
    operation_id: UUID
    status: Literal["committed", "reverted"] = "committed"
    created_track_id: str | None = None
    result_revisions: dict[str, int] = Field(default_factory=dict)


class SceneTrackOperationListItem(_AAPModel):
    id: UUID
    kind: SceneTrackCommandKind
    status: Literal["committed", "reverted"]
    created_at: datetime
    completed_at: datetime
    response: SceneTrackCommandResult


class SceneTrackOperationList(_AAPModel):
    contract_version: Literal[1] = 1
    operations: list[SceneTrackOperationListItem] = Field(default_factory=list)


class TrackSummary(_AAPModel):
    track_id: str
    class_name: str
    member_count: int
    first_frame: int
    last_frame: int


class TrackOperationCandidates(_AAPModel):
    contract_version: Literal[1] = 1
    primary: TrackSummary
    candidates: list[TrackSummary] = Field(default_factory=list)
    truncated: bool = False


class LegacyTrackOperationPreview(_AAPModel):
    contract_version: Literal[1] = 1
    operation: LegacyTrackOperationKind
    scene_id: UUID
    scene_name: str | None = None
    primary: TrackSummary
    secondary: TrackSummary | None = None
    survivor_track_id: str
    affected_member_count: int
    rewritten_member_count: int
    snapshot_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class LegacyTrackOperationResult(LegacyTrackOperationPreview):
    created_track_id: str | None = None
    updated_member_count: int
