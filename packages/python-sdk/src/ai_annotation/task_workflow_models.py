"""Typed contracts for task context, image pyramids, cross-frame jobs, and leases."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import Annotation


class _TaskWorkflowRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class _TaskWorkflowResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        protected_namespaces=("model_validate", "model_dump"),
    )


class TaskAnnotationPage(_TaskWorkflowResponse):
    items: list[Annotation]
    next_cursor: str | None = None


class TaskNeighbor(_TaskWorkflowResponse):
    task_id: UUID
    frame_index: int


class TaskNeighbors(_TaskWorkflowResponse):
    scene_id: UUID | None = None
    scene_name: str | None = None
    frame_index: int | None = None
    scene_total_frames: int
    prev: list[TaskNeighbor] = Field(default_factory=list)
    next: list[TaskNeighbor] = Field(default_factory=list)


class NeighborFrameAnnotations(_TaskWorkflowResponse):
    task_id: UUID
    frame_index: int
    annotations: list[Annotation] = Field(default_factory=list)


class NeighborAnnotations(_TaskWorkflowResponse):
    scene_id: UUID | None = None
    frame_index: int | None = None
    frames: list[NeighborFrameAnnotations] = Field(default_factory=list)


TaskMaskCapabilityReason = Literal[
    "read_disabled",
    "deployment_disabled",
    "project_disabled",
    "region_disabled",
    "task_locked",
    "enabled",
]


class TaskMaskCapabilities(_TaskWorkflowResponse):
    read_enabled: bool
    write_enabled: bool
    legacy_polygon_commit_enabled: bool
    project_enabled: bool
    region_enabled: bool
    reason: TaskMaskCapabilityReason
    max_dimension: int
    max_pixels: int
    max_runs: int
    max_bytes: int


class TaskPredictionShape(_TaskWorkflowResponse):
    type: str
    class_name: str
    geometry: dict[str, Any]
    confidence: float
    attributes: dict[str, Any] = Field(default_factory=dict)
    # This is the original persisted-array offset, even after filtering or sorting.
    shape_index: int | None = None


class TaskPrediction(_TaskWorkflowResponse):
    id: UUID
    task_id: UUID
    project_id: UUID
    ml_backend_id: UUID | None = None
    model_version: str | None = None
    score: float | None = None
    source: str | None = None
    tool_unit_id: str = "bbox"
    result: list[TaskPredictionShape]
    cluster: int | None = None
    created_at: datetime
    inference_time_ms: int | None = None
    total_cost: float | None = None


class ImagePyramidLevel(_TaskWorkflowResponse):
    level: int
    scale_factor: int = Field(alias="scaleFactor")
    width: int
    height: int
    columns: int
    rows: int


class ImagePyramidOverviewManifest(_TaskWorkflowResponse):
    width: int
    height: int
    content_digest: str = Field(alias="contentDigest")


class ImagePyramidManifest(_TaskWorkflowResponse):
    schema_: Literal["aap-image-pyramid/v1"] = Field(alias="schema")
    generation: int
    source_fingerprint: str = Field(alias="sourceFingerprint")
    normalization_version: str = Field(alias="normalizationVersion")
    width: int
    height: int
    tile_size: int = Field(alias="tileSize")
    overlap: int
    format: Literal["webp"]
    levels: list[ImagePyramidLevel]
    overview: ImagePyramidOverviewManifest


class ImagePyramidOverviewUrl(_TaskWorkflowResponse):
    url: str = Field(repr=False)
    expires_at: datetime


class ImagePyramidResponse(_TaskWorkflowResponse):
    task_id: UUID
    status: Literal[
        "not_available",
        "missing",
        "pending",
        "building",
        "ready",
        "failed",
        "stale",
        "inconsistent",
    ]
    required: bool
    source_snapshot: str | None = None
    profile_version: str | None = None
    width: int | None = None
    height: int | None = None
    retryable: bool = False
    retry_after_ms: int | None = None
    generation: int | None = None
    building_generation: int | None = None
    building_status: Literal["pending", "building"] | None = None
    error_code: str | None = None
    manifest: ImagePyramidManifest | None = None
    overview: ImagePyramidOverviewUrl | None = None


class ImagePyramidOverviewAssetRequest(_TaskWorkflowRequest):
    kind: Literal["overview"]
    generation: int = Field(ge=1)


class ImagePyramidTileAssetRequest(_TaskWorkflowRequest):
    kind: Literal["tile"]
    generation: int = Field(ge=1)
    level: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


ImagePyramidAssetRequest = Annotated[
    ImagePyramidOverviewAssetRequest | ImagePyramidTileAssetRequest,
    Field(discriminator="kind"),
]


class ImagePyramidAssetUrlsRequest(_TaskWorkflowRequest):
    items: list[ImagePyramidAssetRequest] = Field(min_length=1, max_length=128)


class ImagePyramidAssetUrl(_TaskWorkflowResponse):
    kind: Literal["overview", "tile"]
    generation: int
    level: int | None = None
    x: int | None = None
    y: int | None = None
    url: str = Field(repr=False)


class ImagePyramidAssetUrlsResponse(_TaskWorkflowResponse):
    task_id: UUID
    generation: int
    expires_at: datetime
    items: list[ImagePyramidAssetUrl]


class ImagePyramidRetryResult(_TaskWorkflowResponse):
    task_id: UUID
    status: Literal["queued", "pending", "building"]
    celery_task_id: str | None = None
    job_id: UUID | None = None


class CrossFrameJobCreateRequest(_TaskWorkflowRequest):
    operation: Literal["propagate"] = "propagate"
    scope: Literal["selected", "all"]
    annotation_ids: list[UUID] = Field(default_factory=list, max_length=500)
    direction: Literal["forward", "backward"]
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    conflict_policy: Literal["skip_existing"] = "skip_existing"

    @model_validator(mode="after")
    def validate_scope_and_range(self) -> CrossFrameJobCreateRequest:
        if self.end_frame < self.start_frame:
            raise ValueError("end_frame must be >= start_frame")
        if self.end_frame - self.start_frame + 1 > 100:
            raise ValueError("cross-frame job range cannot exceed 100 frames")
        if self.scope == "selected" and not self.annotation_ids:
            raise ValueError("selected scope requires annotation_ids")
        if self.scope == "all" and self.annotation_ids:
            raise ValueError("all scope must not include annotation_ids")
        if len(set(self.annotation_ids)) != len(self.annotation_ids):
            raise ValueError("annotation_ids must be unique")
        return self


class TaskLock(_TaskWorkflowResponse):
    task_id: UUID
    user_id: UUID
    expire_at: datetime
    unique_id: UUID


class TaskLockHeartbeat(_TaskWorkflowResponse):
    status: str
