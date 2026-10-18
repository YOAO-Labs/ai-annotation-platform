from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


ImagePyramidStatus = Literal[
    "not_available",
    "missing",
    "pending",
    "building",
    "ready",
    "failed",
    "stale",
    "inconsistent",
]


class ImagePyramidSummary(BaseModel):
    status: ImagePyramidStatus
    source_snapshot: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profile_version: str | None = None
    generation: int | None = None
    width: int | None = None
    height: int | None = None
    tile_size: int = 512
    format: str | None = None
    required: bool
    building_generation: int | None = None
    building_status: Literal["pending", "building"] | None = None
    retryable: bool = False
    retry_after_ms: int | None = None
    error_code: str | None = None


class ImagePyramidLevel(BaseModel):
    level: int
    scaleFactor: int
    width: int
    height: int
    columns: int
    rows: int


class ImagePyramidOverviewManifest(BaseModel):
    width: int
    height: int
    contentDigest: str


class ImagePyramidManifest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: Literal["aap-image-pyramid/v1"] = Field(alias="schema")
    generation: int
    sourceFingerprint: str
    normalizationVersion: str
    width: int
    height: int
    tileSize: int
    overlap: int
    format: Literal["webp"]
    levels: list[ImagePyramidLevel]
    overview: ImagePyramidOverviewManifest


class ImagePyramidOverviewUrl(BaseModel):
    url: str
    expires_at: datetime


class ImagePyramidResponse(BaseModel):
    task_id: UUID
    status: ImagePyramidStatus
    required: bool
    source_snapshot: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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


class ImagePyramidOverviewAssetRequest(BaseModel):
    kind: Literal["overview"]
    generation: int = Field(ge=1)


class ImagePyramidTileAssetRequest(BaseModel):
    kind: Literal["tile"]
    generation: int = Field(ge=1)
    level: int = Field(ge=0)
    x: int = Field(ge=0)
    y: int = Field(ge=0)


ImagePyramidAssetRequest = Annotated[
    ImagePyramidOverviewAssetRequest | ImagePyramidTileAssetRequest,
    Field(discriminator="kind"),
]


class ImagePyramidAssetUrlsRequest(BaseModel):
    items: list[ImagePyramidAssetRequest] = Field(min_length=1, max_length=128)


class ImagePyramidAssetUrl(BaseModel):
    kind: Literal["overview", "tile"]
    generation: int
    level: int | None = None
    x: int | None = None
    y: int | None = None
    url: str


class ImagePyramidAssetUrlsResponse(BaseModel):
    task_id: UUID
    generation: int
    expires_at: datetime
    items: list[ImagePyramidAssetUrl]


class ImagePyramidRetryResponse(BaseModel):
    task_id: UUID
    status: Literal["queued", "pending", "building"]
    celery_task_id: str | None = None
    job_id: UUID | None = None
