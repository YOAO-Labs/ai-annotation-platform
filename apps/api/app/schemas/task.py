from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Literal

from app.schemas._jsonb_types import LidarAxisConvention, SensorCalibration
from app.schemas.scene_pose import FramePose
from app.schemas.user import UserBrief
from app.schemas.image_pyramid import ImagePyramidSummary


class TaskContentIdentity(BaseModel):
    owner_kind: Literal["dataset_asset", "task_original"]
    asset_id: UUID | None
    task_original_id: UUID | None
    sha256: str
    size_bytes: int
    verification_state: Literal["verified"]
    original_availability: Literal["unchecked"]
    preview_availability: Literal["unknown"]


class VideoMetadata(BaseModel):
    duration_ms: int | None = None
    fps: float | None = None
    frame_count: int | None = None
    width: int | None = None
    height: int | None = None
    codec: str | None = None
    playback_path: str | None = None
    playback_codec: str | None = None
    playback_error: str | None = None
    poster_frame_path: str | None = None
    probe_error: str | None = None
    poster_error: str | None = None
    frame_timetable_frame_count: int | None = None
    frame_timetable_error: str | None = None


class TaskOut(BaseModel):
    id: UUID
    project_id: UUID
    display_id: str
    file_name: str
    file_url: str | None = None
    storage_mode: str = "managed"
    content_sha256: str | None = None
    file_type: str
    tags: list = []
    status: str
    assignee_id: UUID | None = None
    # v0.7.2 · 责任人可视化
    assignee: UserBrief | None = None
    reviewer: UserBrief | None = None
    is_labeled: bool = False
    overlap: int = 1
    total_annotations: int = 0
    total_predictions: int = 0
    batch_id: UUID | None = None
    sequence_order: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    thumbnail_url: str | None = None
    blurhash: str | None = None
    image_pyramid: ImagePyramidSummary | None = None
    video_metadata: VideoMetadata | None = None
    # v0.6.5 · 状态机锁定相关
    submitted_at: datetime | None = None
    reviewer_id: UUID | None = None
    reviewer_claimed_at: datetime | None = None
    reviewed_at: datetime | None = None
    reject_reason: str | None = None
    reject_reason_type: str | None = None
    # v0.8.7 F7 · 任务跳过
    skip_reason: str | None = None
    skipped_at: datetime | None = None
    reopened_count: int = 0
    last_reopened_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class ReviewClaimResponse(BaseModel):
    task_id: UUID
    reviewer_id: UUID
    reviewer_claimed_at: datetime
    is_self: bool


class TaskFileUrlResponse(BaseModel):
    url: str
    expires_in: int


MaskCapabilityReason = Literal[
    "read_disabled",
    "deployment_disabled",
    "project_disabled",
    "region_disabled",
    "task_locked",
    "enabled",
]


class TaskMaskCapabilitiesResponse(BaseModel):
    read_enabled: bool
    write_enabled: bool
    legacy_polygon_commit_enabled: bool
    project_enabled: bool
    region_enabled: bool
    reason: MaskCapabilityReason
    max_dimension: int
    max_pixels: int
    max_runs: int
    max_bytes: int


class TaskVideoManifestResponse(BaseModel):
    task_id: UUID
    dataset_item_id: UUID | None = None
    video_url: str
    poster_url: str | None = None
    metadata: VideoMetadata
    expires_in: int = 3600


class PointCloudCameraOut(BaseModel):
    name: str
    role: str
    dataset_item_id: UUID
    image_url: str
    width: int | None = None
    height: int | None = None
    calibration: SensorCalibration | None = None
    calibration_revision: int | None = None
    calibration_digest: str | None = None


class TaskPointCloudManifestResponse(BaseModel):
    task_id: UUID
    point_cloud_url: str
    point_cloud_format: str = "pcd"
    cameras: list[PointCloudCameraOut]
    expires_in: int
    axis_convention: LidarAxisConvention | None = None
    # v0.14.0 · 跨 task 帧序列定位字段。全 None 表示历史未 backfill,
    # 前端按"无 scene"兜底(不渲染跨帧导航)。
    scene_id: UUID | None = None
    scene_name: str | None = None
    frame_index: int | None = None
    scene_total_frames: int | None = None
    # v0.15.0 · 本帧 ego pose(ego→global)透出;无 scene / 无位姿行 → None。
    # 本版前端只做调试可见,不消费(跨帧自动化消费留 v0.15.1)。
    ego_pose: FramePose | None = None


class VideoFrameTimetableEntry(BaseModel):
    frame_index: int
    pts_ms: int
    is_keyframe: bool
    pict_type: str | None = None
    byte_offset: int | None = None


class TaskVideoFrameTimetableResponse(BaseModel):
    task_id: UUID
    fps: float | None = None
    frame_count: int | None = None
    source: Literal["ffprobe", "estimated"]
    frames: list[VideoFrameTimetableEntry]


class TaskLockResponse(BaseModel):
    task_id: UUID
    user_id: UUID
    expire_at: datetime
    unique_id: UUID

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    items: list[TaskOut]
    # v0.11.30 · 仅首页返回精确总数；cursor 翻页时为 None（前端复用首页值）。
    total: int | None = None
    limit: int
    offset: int
    next_cursor: str | None = None


class UploadInitRequest(BaseModel):
    project_id: UUID
    file_name: str = Field(min_length=1, max_length=500)
    content_type: str = Field(default="image/jpeg", min_length=1, max_length=255)


class UploadInitResponse(BaseModel):
    task_id: UUID
    upload_url: str
    expires_in: int
