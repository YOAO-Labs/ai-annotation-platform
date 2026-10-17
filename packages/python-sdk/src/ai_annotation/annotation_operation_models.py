"""Typed request and response models for annotation transform operations."""

from __future__ import annotations

import math
import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from ai_annotation.task_workflow_models import (
    TaskPrediction as Prediction,
    TaskPredictionShape as PredictionShape,
)
from ai_annotation.models import Annotation
from ai_annotation.video_review_models import CocoRleContent, CocoRleMaskRef


class _OperationRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class _OperationResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=("model_validate", "model_dump"),
    )


ConversionTarget = Literal["mask", "polygon", "bbox"]
ConversionOperation = Literal["copy", "replace"]
ConversionScope = Literal["image", "current_frame", "keyframes"]


class AnnotationConversionDryRunRequest(_OperationRequest):
    annotation_ids: list[UUID] = Field(min_length=1, max_length=100)
    target: ConversionTarget
    operation: ConversionOperation = "copy"
    scope: ConversionScope
    frame_index: int | None = Field(default=None, ge=0)
    materialize_held: bool = False

    @field_validator("annotation_ids")
    @classmethod
    def _unique_annotation_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("annotation_ids must be unique")
        return value

    @model_validator(mode="after")
    def _validate_scope(self) -> AnnotationConversionDryRunRequest:
        if self.scope == "image":
            if self.frame_index is not None or self.materialize_held:
                raise ValueError(
                    "image scope must not include frame_index or materialize_held"
                )
            return self
        if self.scope == "current_frame" and self.frame_index is None:
            raise ValueError("current_frame scope requires frame_index")
        if self.scope == "keyframes" and self.frame_index is not None:
            raise ValueError("keyframes scope must not include frame_index")
        if self.scope == "keyframes" and self.materialize_held:
            raise ValueError("keyframes scope does not materialize held frames")
        return self


class AnnotationConversionItemReport(_OperationResponse):
    source_annotation_id: UUID
    source_type: str
    target_type: str
    source_version: int = Field(ge=1)
    frame_indexes: list[int] = Field(default_factory=list)
    result_count: int = Field(ge=1)
    source_area_pixels: int = Field(ge=0)
    target_area_pixels: int = Field(ge=0)
    changed_pixels: int = Field(ge=0)
    source_components: int = Field(ge=0)
    target_components: int = Field(ge=0)
    source_holes: int = Field(ge=0)
    target_holes: int = Field(ge=0)
    source_vertices: int = Field(ge=0)
    target_vertices: int = Field(ge=0)
    materialized_held_frames: int = Field(ge=0)
    lossy: bool
    reasons: list[str] = Field(default_factory=list)


class AnnotationConversionSummary(_OperationResponse):
    source_count: int = Field(ge=1)
    result_count: int = Field(ge=1)
    materialized_held_frames: int = Field(ge=0)
    lossy_count: int = Field(ge=0)


class AnnotationConversionDryRunResponse(_OperationResponse):
    plan_token: str = Field(repr=False)
    expires_at: datetime
    target: ConversionTarget
    operation: ConversionOperation
    scope: ConversionScope
    items: list[AnnotationConversionItemReport]
    summary: AnnotationConversionSummary


class AnnotationConversionExecuteRequest(_OperationRequest):
    plan_token: str = Field(
        min_length=32,
        max_length=128,
        pattern=r"^cvp_[A-Za-z0-9_-]+$",
        repr=False,
    )
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    confirm_replace: bool = False
    confirm_lossy: bool = False


class AnnotationConversionLineage(_OperationResponse):
    source_annotation_id: UUID | None = None
    result_annotation_id: UUID | None = None
    source_version: int | None = None
    result_version: int | None = None
    frame_index: int | None = None


class AnnotationConversionExecuteResponse(_OperationResponse):
    operation_id: UUID
    updated_annotations: list[Annotation] = Field(default_factory=list)
    created_annotations: list[Annotation] = Field(default_factory=list)
    deleted_annotation_ids: list[UUID] = Field(default_factory=list)
    lineage_edges: list[AnnotationConversionLineage] = Field(default_factory=list)
    report: AnnotationConversionSummary
    idempotent_replay: bool = False


class PropagateRequest(_OperationRequest):
    target_task_id: UUID
    override_psr: dict[str, Any] | None = None


class PropagateResponse(_OperationResponse):
    annotation: Annotation
    motion_compensated: bool = False


class PropagateBatchRequest(_OperationRequest):
    target_task_id: UUID
    annotation_ids: list[UUID] | None = None


class PropagateBatchItem(_OperationResponse):
    source_annotation_id: UUID
    annotation: Annotation


class PropagateBatchResponse(_OperationResponse):
    items: list[PropagateBatchItem] = Field(default_factory=list)
    motion_compensated: bool = False


class InterpolateRangeRequest(_OperationRequest):
    track_id: str
    to_task_id: UUID


class InterpolateRangeResponse(_OperationResponse):
    annotations: list[Annotation] = Field(default_factory=list)
    motion_compensated: bool = False
    skipped_frames: list[int] = Field(default_factory=list)


class SecondaryInferenceRequest(_OperationRequest):
    ml_backend_id: UUID
    write_target: Literal["attributes", "geometry"] = "attributes"
    write_keys: list[str] | None = None
    label: str | None = None
    model_id: str | None = None
    model_variants: dict[str, str] | None = None
    params: dict[str, Any] | None = None
    task: str | None = None
    prompt_type: Literal["text"] | None = None
    prompt: str | None = None
    class_filter: list[int] | None = None
    pad: float = Field(default=0.08, ge=0.0, le=0.5, allow_inf_nan=False)


class SecondaryInferenceResponse(_OperationResponse):
    annotation: Annotation
    created_children: list[Annotation] = Field(default_factory=list)


class RasterMaskGeometry(_OperationRequest):
    type: Literal["raster_mask"] = "raster_mask"
    mask: CocoRleMaskRef


class VideoTrackMaskKeyframe(_OperationRequest):
    frame_index: StrictInt = Field(ge=0)
    mask: CocoRleMaskRef
    source: Literal["manual", "prediction"] = "manual"
    occluded: bool = False
    attributes: dict[str, Any] | None = None


class VideoTrackOutsideRange(_OperationRequest):
    from_: int = Field(alias="from", ge=0)
    to: int = Field(ge=0)
    source: Literal["manual", "prediction"] = "manual"


class VideoTrackMaskGeometry(_OperationRequest):
    type: Literal["video_track_mask"] = "video_track_mask"
    track_id: str = Field(min_length=1, max_length=64)
    semantic_label: str | None = None
    keyframes: list[VideoTrackMaskKeyframe] = Field(min_length=1, max_length=3000)
    outside: list[VideoTrackOutsideRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_track_contract(self) -> VideoTrackMaskGeometry:
        frames = [keyframe.frame_index for keyframe in self.keyframes]
        if frames != sorted(frames) or len(frames) != len(set(frames)):
            raise ValueError("mask keyframes must be sorted and unique")
        if any(item.from_ > item.to for item in self.outside):
            raise ValueError("outside ranges must satisfy from <= to")
        return self


MaskGeometry = RasterMaskGeometry | VideoTrackMaskGeometry
MaskOperationKind = Literal[
    "slice_mask",
    "split_components",
    "copy_component",
    "copy_keyframe",
    "join_masks",
    "overlap",
    "delete_small_islands",
    "fill_small_holes",
    "resolve_same_class_overlap",
    "mask_repair_rollback",
]
_NonNegativeInt = Annotated[int, Field(ge=0)]
_SliceCoordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
_SlicePoint = tuple[_SliceCoordinate, _SliceCoordinate]


class MaskMutationScope(_OperationRequest):
    media: Literal["image", "video"]
    frame_index: int | None = Field(default=None, ge=0)
    segment_id: UUID | None = None
    instance_filter: Literal["same_class", "all"] = "same_class"
    class_name: str | None = Field(default=None, min_length=1, max_length=100)
    overlap_policy: Literal["allow", "erase_same_class", "erase_all"] = "allow"
    strict_non_overlap: bool = False

    @model_validator(mode="after")
    def _validate_media_scope(self) -> MaskMutationScope:
        if self.media == "image" and (
            self.frame_index is not None or self.segment_id is not None
        ):
            raise ValueError("image scope must not include frame_index or segment_id")
        if self.media == "video" and (
            self.frame_index is None or self.segment_id is None
        ):
            raise ValueError("video scope requires frame_index and segment_id")
        if self.instance_filter == "same_class" and not self.class_name:
            raise ValueError("same_class scope requires class_name")
        if (
            self.overlap_policy == "erase_same_class"
            and self.instance_filter != "same_class"
        ):
            raise ValueError("erase_same_class requires same_class instance_filter")
        if self.overlap_policy == "erase_all" and self.instance_filter != "all":
            raise ValueError("erase_all requires all instance_filter")
        return self


class MaskExpectedVersion(_OperationRequest):
    annotation_id: UUID
    version: int = Field(ge=1)


class MaskUpdateMutation(_OperationRequest):
    kind: Literal["update"]
    annotation_id: UUID
    geometry: MaskGeometry


class MaskCreateMutation(_OperationRequest):
    kind: Literal["create"]
    source_annotation_ids: list[UUID] = Field(min_length=1, max_length=1000)
    geometry: MaskGeometry

    @field_validator("source_annotation_ids")
    @classmethod
    def _unique_source_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("source_annotation_ids must be unique")
        return value


class MaskDeleteMutation(_OperationRequest):
    kind: Literal["delete"]
    annotation_id: UUID


MaskMutation = Annotated[
    MaskUpdateMutation | MaskCreateMutation | MaskDeleteMutation,
    Field(discriminator="kind"),
]


class MaskMutationAffectedReport(_OperationRequest):
    annotation_id: UUID
    version: int = Field(ge=1)
    changed_pixels: int = Field(ge=0)
    unresolved: bool = False


class MaskMutationReport(_OperationRequest):
    source_areas: list[_NonNegativeInt] = Field(default_factory=list, max_length=1000)
    result_areas: list[_NonNegativeInt] = Field(default_factory=list, max_length=1000)
    before_area: int | None = Field(default=None, ge=0)
    after_area: int | None = Field(default=None, ge=0)
    changed_pixels: int | None = Field(default=None, ge=0)
    before_components: int | None = Field(default=None, ge=0)
    after_components: int | None = Field(default=None, ge=0)
    before_holes: int | None = Field(default=None, ge=0)
    after_holes: int | None = Field(default=None, ge=0)
    bounds: (
        tuple[_NonNegativeInt, _NonNegativeInt, _NonNegativeInt, _NonNegativeInt] | None
    ) = None
    connectivity: Literal[4, 8] | None = None
    affected_annotations: list[MaskMutationAffectedReport] = Field(
        default_factory=list, max_length=1000
    )

    @field_validator("affected_annotations")
    @classmethod
    def _unique_affected_ids(
        cls, value: list[MaskMutationAffectedReport]
    ) -> list[MaskMutationAffectedReport]:
        ids = [item.annotation_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("affected annotation ids must be unique")
        return value


class MaskMutationCommitRequest(_OperationRequest):
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation: MaskOperationKind
    scope: MaskMutationScope
    source_frame_index: int | None = Field(default=None, ge=0)
    cut_path: list[_SlicePoint] | None = Field(default=None, min_length=2, max_length=2)
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_versions: list[MaskExpectedVersion] = Field(
        default_factory=list, max_length=1000
    )
    mutations: list[MaskMutation] = Field(min_length=1, max_length=1000)
    report: MaskMutationReport = Field(default_factory=MaskMutationReport)

    @field_validator("expected_versions")
    @classmethod
    def _stable_expected_versions(
        cls, value: list[MaskExpectedVersion]
    ) -> list[MaskExpectedVersion]:
        ids = [item.annotation_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("expected_versions annotation_id must be unique")
        if ids != sorted(ids, key=str):
            raise ValueError("expected_versions must be sorted by annotation_id")
        return value

    @model_validator(mode="after")
    def _validate_operation_scope(self) -> MaskMutationCommitRequest:
        if self.operation == "slice_mask":
            if self.scope.media != "image" or self.cut_path is None:
                raise ValueError("slice_mask requires image scope and cut_path")
            if self.cut_path[0] == self.cut_path[1]:
                raise ValueError("slice_mask cut_path must have distinct points")
        elif self.cut_path is not None or "cut_path" in self.model_fields_set:
            raise ValueError("cut_path is only valid for slice_mask")
        targets = [
            mutation.annotation_id
            for mutation in self.mutations
            if isinstance(mutation, (MaskUpdateMutation, MaskDeleteMutation))
        ]
        if len(targets) != len(set(targets)):
            raise ValueError("an annotation may only be updated or deleted once")
        if self.operation == "copy_keyframe":
            if self.scope.media != "video" or self.source_frame_index is None:
                raise ValueError("video copy_keyframe requires source_frame_index")
        elif self.source_frame_index is not None:
            raise ValueError("source_frame_index is only valid for copy_keyframe")
        return self


class MaskMutationLineageEdge(_OperationResponse):
    source_annotation_id: UUID | None = None
    result_annotation_id: UUID | None = None
    relation: str
    source_version: int | None = None
    result_version: int | None = None
    frame_index: int | None = None


class MaskMutationAnnotationResult(_OperationResponse):
    id: UUID
    version: int = Field(ge=1)


class PolygonSliceCommitRequest(_OperationRequest):
    annotation_id: UUID
    expected_version: Annotated[int, Field(ge=1, strict=True)]
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    cut_path: list[_SlicePoint] = Field(min_length=2, max_length=256)


class AnnotationSliceRestoreRequest(_OperationRequest):
    target: Literal["before", "after"]
    expected_versions: dict[UUID, Annotated[int, Field(ge=1, strict=True)]] = Field(
        min_length=2, max_length=2
    )
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")


class AnnotationSliceResponse(_OperationResponse):
    operation_id: UUID
    slice_operation_id: UUID
    source_annotation_id: UUID
    created_annotation_id: UUID
    result_versions: dict[str, int]
    active_annotation_ids: list[UUID]
    target: Literal["before", "after"]
    restore_expires_at: datetime
    idempotent_replay: bool = False
    no_op: bool = False


class MaskMutationCommitResponse(_OperationResponse):
    operation_id: UUID
    updated_annotations: list[MaskMutationAnnotationResult] = Field(
        default_factory=list
    )
    created_annotations: list[MaskMutationAnnotationResult] = Field(
        default_factory=list
    )
    deleted_annotation_ids: list[UUID] = Field(default_factory=list)
    result_versions: dict[str, int] = Field(default_factory=dict)
    lineage_edges: list[MaskMutationLineageEdge] = Field(default_factory=list)
    before_digest: str
    after_digest: str
    audit_id: int
    idempotent_replay: bool = False
    slice_restore: AnnotationSliceResponse | None = None


class PredictionAcceptOptions(_OperationRequest):
    """CLI/input convenience fields split into accept query and body arguments."""

    shape_index: int | None = Field(default=None, ge=0)
    video_segment_id: UUID | None = None
    override_class_name: str | None = None
    attribute_overrides: dict[str, Any] | None = None


class NativeMaskCandidatePreview(_OperationRequest):
    points: list[tuple[float, float]] = Field(min_length=3, max_length=8192)

    @field_validator("points")
    @classmethod
    def _normalized_points(
        cls, value: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        for x, y in value:
            if (
                not math.isfinite(x)
                or not math.isfinite(y)
                or not (0 <= x <= 1 and 0 <= y <= 1)
            ):
                raise ValueError("preview coordinates must be finite and normalized")
        return value


class NativeMaskRle(CocoRleContent):
    """Inline native-candidate RLE, using the shared bounded COCO RLE model."""

    @model_validator(mode="after")
    def _validate_protocol_bounds(self) -> NativeMaskRle:
        height, width = self.size
        if height > 4096 or width > 4096 or height * width > 4096 * 4096:
            raise ValueError("native mask dimensions must be <= 4096 per axis")
        canonical = json.dumps(
            {"encoding": "coco_rle", "size": list(self.size), "counts": self.counts},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(canonical) > 4 * 1024 * 1024:
            raise ValueError("native mask RLE JSON must be <= 4 MiB")
        return self


class NativeMaskCandidateValue(_OperationRequest):
    rle: NativeMaskRle
    masklabels: list[str] = Field(min_length=1, max_length=1)
    preview: NativeMaskCandidatePreview | None = None

    @field_validator("masklabels")
    @classmethod
    def _validate_mask_label(cls, value: list[str]) -> list[str]:
        if not value[0] or len(value[0]) > 128:
            raise ValueError("masklabels must contain one non-empty label <= 128 chars")
        return value

    @model_validator(mode="after")
    def _require_foreground(self) -> NativeMaskCandidateValue:
        if sum(self.rle.counts[1::2]) <= 0:
            raise ValueError("native mask candidate must contain foreground pixels")
        return self


class NativeMaskCandidate(_OperationRequest):
    type: Literal["mask"] = "mask"
    value: NativeMaskCandidateValue = Field(repr=False)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    candidate_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AiMaskCandidateInput(_OperationRequest):
    candidate: NativeMaskCandidate = Field(repr=False)
    candidate_index: StrictInt = Field(ge=0, le=1024)
    prompt_revision: str = Field(min_length=1, max_length=256)
    receipt: str = Field(min_length=16, max_length=4096, repr=False)


class AiMaskPromptSummary(_OperationRequest):
    family: Literal[
        "point",
        "interactive_box",
        "exemplar",
        "mask",
        "scribble",
        "correction_frame",
    ]
    positive_points: StrictInt = Field(default=0, ge=0, le=8192)
    negative_points: StrictInt = Field(default=0, ge=0, le=8192)
    boxes: StrictInt = Field(default=0, ge=0, le=64)
    positive_scribbles: StrictInt = Field(default=0, ge=0, le=64)
    negative_scribbles: StrictInt = Field(default=0, ge=0, le=64)
    multimask: bool = False
    parameters_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_totals(self) -> AiMaskPromptSummary:
        if self.positive_points + self.negative_points > 8192:
            raise ValueError("total prompt points must be <= 8192")
        if self.positive_scribbles + self.negative_scribbles > 64:
            raise ValueError("total prompt scribbles must be <= 64")
        return self


class AiMaskRoutingLineage(_OperationRequest):
    requested_backend_id: UUID
    backend_pool_id: UUID | None = None
    backend_instance_id: UUID
    model_id: str = Field(min_length=1, max_length=128)


class AiMaskInferenceLineage(_OperationRequest):
    model_version: str | None = Field(default=None, max_length=100)
    inference_time_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    cache_hit: bool | None = None
    model_load_ms: float | None = Field(default=None, ge=0, le=86_400_000)


class AiMaskAcceptTarget(_OperationRequest):
    mode: Literal["create", "refine"] = "create"
    source_annotation_id: UUID | None = None
    source_version: StrictInt | None = Field(default=None, ge=1)
    frame_index: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_mode(self) -> AiMaskAcceptTarget:
        if self.mode == "create" and (
            self.source_annotation_id is not None or self.source_version is not None
        ):
            raise ValueError("create target cannot include source annotation fields")
        if self.mode == "refine" and (
            self.source_annotation_id is None or self.source_version is None
        ):
            raise ValueError(
                "refine target requires source_annotation_id and source_version"
            )
        return self


class AiMaskAcceptRequest(_OperationRequest):
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    candidate: AiMaskCandidateInput = Field(repr=False)
    class_name: str = Field(min_length=1, max_length=100)
    video_segment_id: UUID | None = None
    target: AiMaskAcceptTarget = Field(default_factory=AiMaskAcceptTarget)
    prompt_summary: AiMaskPromptSummary
    routing: AiMaskRoutingLineage
    inference: AiMaskInferenceLineage = Field(default_factory=AiMaskInferenceLineage)


class AiMaskAcceptResponse(_OperationResponse):
    prediction: Prediction
    annotation: Annotation
    source_version: int | None = None
    result_version: int
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: bool = False


__all__ = [
    "AiMaskAcceptRequest",
    "AiMaskAcceptResponse",
    "AiMaskAcceptTarget",
    "AiMaskCandidateInput",
    "AiMaskInferenceLineage",
    "AiMaskPromptSummary",
    "AiMaskRoutingLineage",
    "AnnotationConversionDryRunRequest",
    "AnnotationConversionDryRunResponse",
    "AnnotationConversionExecuteRequest",
    "AnnotationConversionExecuteResponse",
    "AnnotationConversionItemReport",
    "AnnotationConversionLineage",
    "AnnotationConversionSummary",
    "AnnotationSliceResponse",
    "AnnotationSliceRestoreRequest",
    "ConversionOperation",
    "ConversionScope",
    "ConversionTarget",
    "InterpolateRangeRequest",
    "InterpolateRangeResponse",
    "MaskCreateMutation",
    "MaskDeleteMutation",
    "MaskExpectedVersion",
    "MaskGeometry",
    "MaskMutation",
    "MaskMutationAnnotationResult",
    "MaskMutationCommitRequest",
    "MaskMutationCommitResponse",
    "MaskMutationLineageEdge",
    "MaskMutationReport",
    "MaskMutationScope",
    "MaskOperationKind",
    "MaskUpdateMutation",
    "NativeMaskCandidate",
    "NativeMaskCandidatePreview",
    "NativeMaskCandidateValue",
    "NativeMaskRle",
    "PolygonSliceCommitRequest",
    "Prediction",
    "PredictionAcceptOptions",
    "PredictionShape",
    "PropagateBatchRequest",
    "PropagateBatchResponse",
    "PropagateRequest",
    "PropagateResponse",
    "RasterMaskGeometry",
    "SecondaryInferenceRequest",
    "SecondaryInferenceResponse",
    "VideoTrackMaskGeometry",
    "VideoTrackMaskKeyframe",
    "VideoTrackOutsideRange",
]
