"""Typed REST contracts for point-cloud cameras and sensor calibration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CameraVisibility = Literal["visible", "occluded", "truncated", "unknown"]
CalibrationRelationStatus = Literal["current", "stale"]


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class _ResponseModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        from_attributes=True,
        protected_namespaces=("model_validate", "model_dump"),
    )


class NormalizedCameraBbox(_RequestModel):
    """Normalized xywh camera box; bbox edges must remain inside the image."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_image(self) -> NormalizedCameraBbox:
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("camera bbox must stay inside normalized image bounds")
        return self


class SensorCalibration(_RequestModel):
    """Row-major 4x4 extrinsic, 3x3 intrinsic, and optional KITTI 4x4 rect."""

    extrinsic: list[float] = Field(min_length=16, max_length=16)
    intrinsic: list[float] = Field(min_length=9, max_length=9)
    rect: list[float] | None = Field(default=None, min_length=16, max_length=16)


class SensorCalibrationRead(_ResponseModel):
    """Additive readback of a calibration snapshot."""

    extrinsic: list[float] = Field(min_length=16, max_length=16)
    intrinsic: list[float] = Field(min_length=9, max_length=9)
    rect: list[float] | None = Field(default=None, min_length=16, max_length=16)


class NormalizedCameraBboxRead(_ResponseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class CameraAnnotationMemberCreate(_RequestModel):
    source_annotation_id: UUID
    camera_role: str = Field(min_length=1, max_length=50, pattern=r"^camera_")
    bbox: NormalizedCameraBbox
    visibility: CameraVisibility = "visible"
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)


class CameraAnnotationMemberUpdate(_RequestModel):
    bbox: NormalizedCameraBbox | None = None
    visibility: CameraVisibility | None = None
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def _requires_change(self) -> CameraAnnotationMemberUpdate:
        if self.bbox is None and self.visibility is None:
            raise ValueError("bbox or visibility is required")
        return self


class CameraAnnotationMemberDelete(_RequestModel):
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)


class CameraAnnotationMemberRestore(_RequestModel):
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)


class SensorCalibrationUpdate(_RequestModel):
    calibration: SensorCalibration
    expected_revision: int = Field(ge=1)
    expected_digest: str = Field(min_length=64, max_length=64)


class CameraProjectionResidual(_ResponseModel):
    iou: float = Field(ge=0, le=1)
    max_edge_residual_px: float = Field(ge=0)
    mean_edge_residual_px: float = Field(ge=0)
    max_edge_residual_ratio: float = Field(ge=0)
    projected_bbox: NormalizedCameraBboxRead


class CameraAnnotationMember(_ResponseModel):
    id: UUID
    task_id: UUID
    scene_track_id: UUID
    track_id: str
    class_name: str
    camera_dataset_item_id: UUID
    camera_role: str
    bbox: NormalizedCameraBboxRead
    visibility: CameraVisibility
    version: int
    is_active: bool
    calibration_revision: int
    calibration_digest: str
    current_calibration_revision: int
    current_calibration_digest: str
    relation_status: CalibrationRelationStatus
    track_revision: int = Field(ge=1)
    residual: CameraProjectionResidual | None = None
    created_at: datetime
    updated_at: datetime


class CameraAnnotationMemberList(_ResponseModel):
    items: list[CameraAnnotationMember]
    track_revision: int | None = Field(default=None, ge=1)
    projected_bbox: NormalizedCameraBboxRead | None = None


class SensorCalibrationRevision(_ResponseModel):
    dataset_item_id: UUID
    revision: int
    digest: str
    calibration: SensorCalibrationRead
    created_at: datetime | None = None


class SensorCalibrationHistory(_ResponseModel):
    current_revision: int = Field(ge=1)
    current_digest: str
    items: list[SensorCalibrationRevision]


class PointCloudCamera(_ResponseModel):
    name: str
    role: str
    dataset_item_id: UUID
    image_url: str = Field(repr=False)
    width: int | None = None
    height: int | None = None
    calibration: SensorCalibrationRead | None = None
    calibration_revision: int | None = None
    calibration_digest: str | None = None


class PointCloudEgoPose(_ResponseModel):
    frame_index: int = Field(ge=0)
    timestamp_us: int | None = None
    ego_translation: list[float] = Field(min_length=3, max_length=3)
    ego_rotation: list[float] = Field(min_length=4, max_length=4)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class PointCloudManifest(_ResponseModel):
    """Point cloud and camera signed URLs are time-limited manifest values."""

    task_id: UUID
    point_cloud_url: str = Field(repr=False)
    point_cloud_format: str = "pcd"
    cameras: list[PointCloudCamera]
    expires_in: int
    axis_convention: str | None = None
    scene_id: UUID | None = None
    scene_name: str | None = None
    frame_index: int | None = None
    scene_total_frames: int | None = None
    ego_pose: PointCloudEgoPose | None = None
