"""Source-bound, display-only point-cloud enhancement contracts."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._jsonb_types import LidarAxisConvention

PointCloudEnhancementProfile = Literal[
    "omega-current-mono-512", "omega-current-mono-512-full-lidar"
]


class PointCloudEnhancementConfig(BaseModel):
    schema_version: Literal[1] = 1
    enabled: bool = False
    profile: PointCloudEnhancementProfile = "omega-current-mono-512"
    ml_backend_pool_id: UUID | None = None

    model_config = ConfigDict(extra="forbid")


class PointCloudAuxiliaryCamera(BaseModel):
    role: str
    name: str
    point_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PointCloudAugmentationResponse(BaseModel):
    task_id: UUID
    enabled: bool
    status: Literal[
        "disabled", "not_available", "missing", "building", "ready", "failed", "stale"
    ]
    reason: str | None = None
    asset_id: UUID | None = None
    generation: int | None = None
    profile: PointCloudEnhancementProfile | None = None
    input_digest: str | None = None
    coordinate_frame: Literal["primary_pointcloud"] = "primary_pointcloud"
    axis_convention: LidarAxisConvention | None = None
    cameras: list[PointCloudAuxiliaryCamera] = Field(default_factory=list)


class PointCloudAuxiliaryAssetRequest(BaseModel):
    asset_id: UUID
    generation: int = Field(ge=1)
    camera_role: str = Field(pattern=r"^camera_[A-Za-z0-9_-]+$", max_length=50)

    model_config = ConfigDict(extra="forbid")


class PointCloudAuxiliaryAssetResponse(BaseModel):
    url: str
    expires_in: int = Field(ge=1, le=300)
    format: Literal["pcd"] = "pcd"
    point_count: int
    sha256: str
    generation: int
    camera_role: str
    coordinate_frame: Literal["primary_pointcloud"] = "primary_pointcloud"
    axis_convention: LidarAxisConvention | None = None
