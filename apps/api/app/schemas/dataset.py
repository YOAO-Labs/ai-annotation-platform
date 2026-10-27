import unicodedata
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from aap_assets_source.upload_contract import validate_relative_file_name

from aap_assets_catalog.spatial_values import LidarAxisConvention

from app.schemas._jsonb_types import DatasetItemMetadata
from app.schemas.dataset_source import AgentSourceStatus


class DatasetCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str = ""
    data_type: str = Field(default="image", max_length=30)
    is_temporal: bool = False
    # v0.13.11 · 点云数据集 lidar 坐标系约定，写进 Dataset.metadata_["axis_convention"]。
    axis_convention: LidarAxisConvention | None = None


class DatasetUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    # v0.13.11 · 未传 = 不改；显式 None = 清除；具体值 = 覆盖。
    axis_convention: LidarAxisConvention | None = None


class DatasetOut(BaseModel):
    id: UUID
    display_id: str
    name: str
    description: str
    data_type: str
    is_temporal: bool = False
    has_scenes: bool = False
    file_count: int
    total_size: int = 0
    created_by: UUID
    project_count: int = 0
    archived_at: datetime | None = None
    archived_by: UUID | None = None
    lifecycle_revision: int = 1
    # v0.13.11 · 派生自 metadata_["axis_convention"]，None = 视作 iso_8855。
    axis_convention: LidarAxisConvention | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


DatasetAvailabilityState = Literal[
    "verified", "missing", "changed", "preview_only", "unavailable", "unverified"
]


class DatasetSceneOption(BaseModel):
    id: UUID
    display_id: str
    name: str


class DatasetSceneOptions(BaseModel):
    items: list[DatasetSceneOption]
    total: int
    selected: DatasetSceneOption | None


class DatasetItemAvailability(BaseModel):
    agent_status: AgentSourceStatus | None = None
    state: DatasetAvailabilityState = "unverified"
    reason_code: str = "not_verified"
    observed_at: datetime | None = None
    preview_available: bool = False
    original_cache_recorded: bool = False
    original_cache_recorded_at: datetime | None = None
    original_cache_expires_at: datetime | None = None
    source_id: UUID | None = None
    source_name: str | None = None
    location_id: UUID | None = None
    allowed_actions: list[str] = Field(default_factory=list)


class DatasetItemOut(BaseModel):
    id: UUID
    dataset_id: UUID
    file_name: str
    file_path: str
    file_type: str
    file_size: int | None = None
    content_hash: str | None = None
    width: int | None = None
    height: int | None = None
    # v0.13.1 · 结构化视图: calibration 仅点云相机项有, extra="allow" 保留其它 key。
    metadata: DatasetItemMetadata = Field(default_factory=DatasetItemMetadata)
    file_url: str | None = None
    original_identity_path: str | None = None
    thumbnail_url: str | None = None
    blurhash: str | None = None
    created_at: datetime
    availability: DatasetItemAvailability = Field(
        default_factory=DatasetItemAvailability
    )

    class Config:
        from_attributes = True


class DatasetListResponse(BaseModel):
    items: list[DatasetOut]
    total: int
    limit: int
    offset: int


class DatasetItemListResponse(BaseModel):
    items: list[DatasetItemOut]
    total: int
    limit: int
    offset: int


class DatasetWorkbenchSource(BaseModel):
    id: UUID
    name: str
    location_id: UUID
    enabled: bool
    unregistered_at: datetime | None


class DatasetProjectUsage(BaseModel):
    id: UUID
    name: str
    display_id: str
    type_label: str
    total_tasks: int
    labeled_tasks: int
    completed_tasks: int
    review_tasks: int
    can_manage: bool


class DatasetWorkbench(BaseModel):
    dataset_id: UUID
    availability_counts: dict[str, int]
    sources: list[DatasetWorkbenchSource]
    projects: list[DatasetProjectUsage]


class DatasetTrainingVersion(BaseModel):
    id: UUID
    project_id: UUID
    project_name: str
    name: str
    status: str
    created_at: datetime


class DatasetTrainingVersionList(BaseModel):
    items: list[DatasetTrainingVersion]
    total: int
    limit: int
    offset: int


class DatasetLinkRequest(BaseModel):
    project_id: UUID


class DatasetUploadInitRequest(BaseModel):
    file_name: str = Field(
        min_length=1, max_length=500, json_schema_extra={"x-max-utf8-bytes": 897}
    )
    content_type: str = Field(default="image/jpeg", min_length=1, max_length=255)

    @field_validator("file_name")
    @classmethod
    def safe_file_name(cls, value: str) -> str:
        return validate_relative_file_name(value)

    @field_validator("content_type")
    @classmethod
    def canonical_content_type(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("content_type must not have surrounding whitespace")
        return value


class DatasetUploadInitResponse(BaseModel):
    item_id: UUID
    upload_url: str
    expires_in: int


class DatasetImportFromConnectionRequest(BaseModel):
    connection_id: UUID
    source_path: str = Field(default="", max_length=2048)
    recursive: bool = True
    include_globs: list[str] | None = Field(default=None, max_length=256)

    @field_validator("include_globs")
    @classmethod
    def bounded_include_globs(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if any(
            len(value) > 255
            or any(unicodedata.category(char) == "Cc" for char in value)
            for value in values
        ):
            raise ValueError("include_globs contains an invalid pattern")
        return values


class DatasetImportFromConnectionResponse(BaseModel):
    job_id: UUID


class DatasetImageBackfillResponse(BaseModel):
    dataset_id: UUID
    operation_id: UUID
    job_id: UUID
    purpose: Literal["dimensions", "thumbnail"]
    status: Literal["pending", "running", "ready", "failed", "canceled"]
    accepted_before: datetime


class SniffAxisConventionCandidate(BaseModel):
    convention: LidarAxisConvention
    score: float


class SniffAxisConventionCamera(BaseModel):
    camera_role: str | None = None
    best: LidarAxisConvention
    score: float


class SniffAxisConventionResponse(BaseModel):
    best: LidarAxisConvention | None = None
    score: float | None = None
    candidates: list[SniffAxisConventionCandidate] = Field(default_factory=list)
    source: Literal["task_link", "dataset_item"] | None = None
    camera_role: str | None = None
    camera_item_id: UUID | None = None
    per_camera: list[SniffAxisConventionCamera] | None = None
    agreement: float | None = None
