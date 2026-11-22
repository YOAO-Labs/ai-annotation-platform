from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas._jsonb_types import RasterMaskGeometry, VideoTrackMaskGeometry
from app.schemas.annotation_slice import AnnotationSliceResponse, SlicePoint

MaskGeometry = RasterMaskGeometry | VideoTrackMaskGeometry
NonNegativeInt = Annotated[int, Field(ge=0)]
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


class MaskMutationScope(BaseModel):
    media: Literal["image", "video"]
    frame_index: int | None = Field(default=None, ge=0)
    segment_id: UUID | None = None
    instance_filter: Literal["same_class", "all"] = "same_class"
    class_name: str | None = Field(default=None, min_length=1, max_length=100)
    overlap_policy: Literal["allow", "erase_same_class", "erase_all"] = "allow"
    strict_non_overlap: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_media_scope(self):
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


class MaskExpectedVersion(BaseModel):
    annotation_id: UUID
    version: int = Field(ge=1)

    model_config = ConfigDict(extra="forbid")


class MaskUpdateMutation(BaseModel):
    kind: Literal["update"]
    annotation_id: UUID
    geometry: MaskGeometry

    model_config = ConfigDict(extra="forbid")


class MaskCreateMutation(BaseModel):
    kind: Literal["create"]
    source_annotation_ids: list[UUID] = Field(min_length=1, max_length=1000)
    geometry: MaskGeometry

    model_config = ConfigDict(extra="forbid")

    @field_validator("source_annotation_ids")
    @classmethod
    def _unique_sources(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("source_annotation_ids must be unique")
        return value


class MaskDeleteMutation(BaseModel):
    kind: Literal["delete"]
    annotation_id: UUID

    model_config = ConfigDict(extra="forbid")


MaskMutation = Annotated[
    MaskUpdateMutation | MaskCreateMutation | MaskDeleteMutation,
    Field(discriminator="kind"),
]


class MaskMutationAffectedReport(BaseModel):
    annotation_id: UUID
    version: int = Field(ge=1)
    changed_pixels: int = Field(ge=0)
    unresolved: bool = False

    model_config = ConfigDict(extra="forbid")


class MaskMutationReport(BaseModel):
    source_areas: list[NonNegativeInt] = Field(default_factory=list, max_length=1000)
    result_areas: list[NonNegativeInt] = Field(default_factory=list, max_length=1000)
    before_area: int | None = Field(default=None, ge=0)
    after_area: int | None = Field(default=None, ge=0)
    changed_pixels: int | None = Field(default=None, ge=0)
    before_components: int | None = Field(default=None, ge=0)
    after_components: int | None = Field(default=None, ge=0)
    before_holes: int | None = Field(default=None, ge=0)
    after_holes: int | None = Field(default=None, ge=0)
    bounds: (
        tuple[
            NonNegativeInt,
            NonNegativeInt,
            NonNegativeInt,
            NonNegativeInt,
        ]
        | None
    ) = None
    connectivity: Literal[4, 8] | None = None
    affected_annotations: list[MaskMutationAffectedReport] = Field(
        default_factory=list, max_length=1000
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("affected_annotations")
    @classmethod
    def _unique_affected_annotations(
        cls, value: list[MaskMutationAffectedReport]
    ) -> list[MaskMutationAffectedReport]:
        ids = [item.annotation_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("affected_annotations annotation_id must be unique")
        return value


class MaskMutationCommitRequest(BaseModel):
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    operation: MaskOperationKind
    scope: MaskMutationScope
    source_frame_index: int | None = Field(default=None, ge=0)
    cut_path: list[SlicePoint] | None = Field(default=None, min_length=2, max_length=2)
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_versions: list[MaskExpectedVersion] = Field(
        default_factory=list, max_length=1000
    )
    mutations: list[MaskMutation] = Field(min_length=1, max_length=1000)
    report: MaskMutationReport = Field(default_factory=MaskMutationReport)

    model_config = ConfigDict(extra="forbid")

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
    def _unique_write_targets(self):
        if self.operation == "slice_mask":
            if self.scope.media != "image" or self.cut_path is None:
                raise ValueError(
                    "slice_mask requires image scope and two cut_path points"
                )
            if self.cut_path[0] == self.cut_path[1]:
                raise ValueError("slice_mask cut_path must have two distinct points")
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


class MaskLineageEdgeOut(BaseModel):
    source_annotation_id: UUID | None = None
    result_annotation_id: UUID | None = None
    relation: str
    source_version: int | None = None
    result_version: int | None = None
    frame_index: int | None = None


class MaskMutationAnnotationResult(BaseModel):
    id: UUID
    version: int = Field(ge=1)


class MaskMutationCommitResponse(BaseModel):
    operation_id: UUID
    updated_annotations: list[MaskMutationAnnotationResult] = Field(
        default_factory=list
    )
    created_annotations: list[MaskMutationAnnotationResult] = Field(
        default_factory=list
    )
    deleted_annotation_ids: list[UUID] = Field(default_factory=list)
    result_versions: dict[str, int] = Field(default_factory=dict)
    lineage_edges: list[MaskLineageEdgeOut] = Field(default_factory=list)
    before_digest: str
    after_digest: str
    audit_id: int
    idempotent_replay: bool = False
    slice_restore: AnnotationSliceResponse | None = None
