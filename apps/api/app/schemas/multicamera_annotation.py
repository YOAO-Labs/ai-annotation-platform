from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from aap_assets_catalog.spatial_values import SensorCalibration


CameraVisibility = Literal["visible", "occluded", "truncated", "unknown"]
CalibrationRelationStatus = Literal["current", "stale"]


class NormalizedCameraBbox(BaseModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("camera bbox must stay inside normalized image bounds")
        return self


class CameraAnnotationMemberCreate(BaseModel):
    source_annotation_id: UUID
    camera_role: str = Field(min_length=1, max_length=50, pattern=r"^camera_")
    bbox: NormalizedCameraBbox
    visibility: CameraVisibility = "visible"
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)


class CameraAnnotationMemberUpdate(BaseModel):
    bbox: NormalizedCameraBbox | None = None
    visibility: CameraVisibility | None = None
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_change(self):
        if self.bbox is None and self.visibility is None:
            raise ValueError("bbox or visibility is required")
        return self


class CameraAnnotationMemberDelete(BaseModel):
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)


class CameraAnnotationMemberRestore(BaseModel):
    expected_version: int = Field(ge=1)
    expected_track_revision: int = Field(ge=1)
    expected_calibration_revision: int = Field(ge=1)
    expected_calibration_digest: str = Field(min_length=64, max_length=64)


class CameraProjectionResidual(BaseModel):
    iou: float = Field(ge=0, le=1)
    max_edge_residual_px: float = Field(ge=0)
    mean_edge_residual_px: float = Field(ge=0)
    max_edge_residual_ratio: float = Field(ge=0)
    projected_bbox: NormalizedCameraBbox


class CameraAnnotationMemberOut(BaseModel):
    id: UUID
    task_id: UUID
    scene_track_id: UUID
    track_id: str
    class_name: str
    camera_dataset_item_id: UUID
    camera_role: str
    bbox: NormalizedCameraBbox
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


class CameraAnnotationMemberList(BaseModel):
    items: list[CameraAnnotationMemberOut]
    track_revision: int | None = Field(default=None, ge=1)
    projected_bbox: NormalizedCameraBbox | None = None


class SensorCalibrationUpdate(BaseModel):
    calibration: SensorCalibration
    expected_revision: int = Field(ge=1)
    expected_digest: str = Field(min_length=64, max_length=64)


class SensorCalibrationRevisionOut(BaseModel):
    dataset_item_id: UUID
    revision: int
    digest: str
    calibration: SensorCalibration
    created_at: datetime | None = None


class SensorCalibrationHistoryOut(BaseModel):
    current_revision: int = Field(ge=1)
    current_digest: str = Field(min_length=64, max_length=64)
    items: list[SensorCalibrationRevisionOut]
