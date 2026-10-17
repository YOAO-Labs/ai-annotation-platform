"""Typed request and response contracts for video review and track editing."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from ai_annotation.models import Annotation, _AAPModel


class _VideoRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class CocoRleMaskRef(_VideoRequest):
    """Immutable object reference accepted by video mask-keyframe edits."""

    encoding: Literal["coco_rle_ref", "coco_rle_gzip"] = "coco_rle_ref"
    storage_encoding: Literal["identity", "gzip"] | None = None
    size: tuple[StrictInt, StrictInt]
    object_key: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runs: StrictInt = Field(ge=1, le=1_000_000)
    bytes: StrictInt = Field(ge=1, le=4 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_reference(self) -> "CocoRleMaskRef":
        height, width = self.size
        if not (0 < height <= 8192 and 0 < width <= 8192):
            raise ValueError("mask height and width must be in [1, 8192]")
        gzip_key = self.object_key.endswith(".json.gz")
        expected = (
            f"raster-masks/sha256/{self.sha256[:2]}/{self.sha256[2:4]}/"
            f"{self.sha256}.json"
        )
        if gzip_key:
            expected += ".gz"
        if self.object_key != expected:
            raise ValueError("mask object_key must match its sha256 content address")
        if self.storage_encoding == "gzip" and not gzip_key:
            raise ValueError("gzip masks must use a .json.gz object key")
        if self.storage_encoding == "identity" and gzip_key:
            raise ValueError("identity masks must use a .json object key")
        if self.encoding == "coco_rle_gzip" and not gzip_key:
            raise ValueError("coco_rle_gzip masks must use a .json.gz object key")
        if (
            gzip_key
            and self.encoding == "coco_rle_ref"
            and self.storage_encoding != "gzip"
        ):
            raise ValueError(".json.gz mask references must declare gzip storage")
        return self


class CocoRleContent(_VideoRequest):
    """Uncompressed COCO RLE JSON used by the mask-content endpoints."""

    encoding: Literal["coco_rle"] = "coco_rle"
    size: tuple[StrictInt, StrictInt]
    counts: list[StrictInt] = Field(min_length=1, max_length=1_000_000)

    @model_validator(mode="after")
    def validate_rle(self) -> "CocoRleContent":
        height, width = self.size
        if not (0 < height <= 8192 and 0 < width <= 8192):
            raise ValueError("mask height and width must be in [1, 8192]")
        pixels = height * width
        if pixels > 67_108_864:
            raise ValueError("mask pixel count must be <= 67108864")
        total = 0
        for count in self.counts:
            if count < 0:
                raise ValueError("mask counts must be non-negative integers")
            total += count
            if total > pixels:
                raise ValueError("sum(counts) must not exceed height * width")
        if total != pixels:
            raise ValueError("sum(counts) must equal height * width")
        return self


class VideoMaskContentUploadRequest(CocoRleContent):
    """RLE body for task-scoped upload; storage encoding is a separate choice."""

    storage_encoding: Literal["identity", "gzip"] | None = None


class VideoMaskKeyframeSaveRequest(_VideoRequest):
    mask: CocoRleMaskRef
    source: Literal["manual", "prediction"] = "manual"
    occluded: bool = False
    attributes: dict[str, Any] | None = None


class VideoMaskKeyframeOperationRequest(_VideoRequest):
    operation: Literal["delete_keyframe", "mark_outside", "restore_held"]


class VideoTrackQualityRunRequest(_VideoRequest):
    left_segment_id: UUID
    right_segment_id: UUID


class VideoTrackPairDecision(_VideoRequest):
    left_annotation_id: UUID
    right_annotation_id: UUID
    decision: Literal["same_track", "different_track"]


class VideoTrackQualityAcceptRequest(_VideoRequest):
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs: list[VideoTrackPairDecision] = Field(max_length=1000)


class VideoTrackQualityIssue(_AAPModel):
    id: UUID
    left_annotation_id: UUID | None = None
    right_annotation_id: UUID | None = None
    code: str
    frame_start: int
    frame_end: int
    metric: dict[str, Any]


class VideoTrackQualityRun(_AAPModel):
    id: UUID
    task_id: UUID
    left_segment_id: UUID
    right_segment_id: UUID
    async_job_id: UUID | None
    status: str
    progress_pct: int
    input_digest: str
    sampling_digest: str
    fragments: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any]
    pairs: list[dict[str, Any]]
    error_message: str | None
    accepted_by_id: UUID | None
    accepted_at: datetime | None
    stale_at: datetime | None
    created_at: datetime
    issues: list[VideoTrackQualityIssue] = Field(default_factory=list)


class VideoTrackCompositionRequest(_VideoRequest):
    operation: Literal["aggregate_bboxes", "split_track", "merge_tracks", "join_tracks"]
    annotation_ids: list[UUID] = Field(min_length=1)
    frame_index: int | None = Field(default=None, ge=0)
    delete_sources: bool = True
    gap_mode: Literal["interpolate", "outside"] = "interpolate"

    @model_validator(mode="after")
    def validate_composition(self) -> "VideoTrackCompositionRequest":
        if len(set(self.annotation_ids)) != len(self.annotation_ids):
            raise ValueError("annotation_ids must be unique")
        if self.operation == "split_track":
            if len(self.annotation_ids) != 1 or self.frame_index is None:
                raise ValueError(
                    "split_track requires one annotation_id and frame_index"
                )
        if (
            self.operation in {"merge_tracks", "join_tracks"}
            and len(self.annotation_ids) != 2
        ):
            raise ValueError(f"{self.operation} requires exactly two annotation_ids")
        return self


class VideoTrackCompositionResponse(_AAPModel):
    operation: Literal["aggregate_bboxes", "split_track", "merge_tracks", "join_tracks"]
    updated_annotations: list[Annotation] = Field(default_factory=list)
    created_annotations: list[Annotation] = Field(default_factory=list)
    deleted_annotation_ids: list[UUID] = Field(default_factory=list)


class VideoTrackConvertToBboxesRequest(_VideoRequest):
    operation: Literal["copy", "split"] = "copy"
    scope: Literal["frame", "track"] = "frame"
    frame_index: int | None = Field(default=None, ge=0)
    frame_mode: Literal["keyframes", "all_frames"] = "keyframes"

    @model_validator(mode="after")
    def require_frame_for_frame_scope(self) -> "VideoTrackConvertToBboxesRequest":
        if self.scope == "frame" and self.frame_index is None:
            raise ValueError("frame_index is required when scope is frame")
        return self


class VideoTrackConvertToBboxesResponse(_AAPModel):
    source_annotation: Annotation | None
    created_annotations: list[Annotation]
    deleted_source: bool
    removed_frame_indexes: list[int] = Field(default_factory=list)
