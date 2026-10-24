from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class VideoChunkDiagnostics(BaseModel):
    source_codec: str | None = None
    output_codec: str | None = None
    keyframe_aligned: bool | None = None
    start_byte_offset: int | None = None
    end_byte_offset: int | None = None
    smart_copy_eligible: bool | None = None
    fallback_reason: str | None = None


class VideoChunkOut(BaseModel):
    chunk_id: int
    start_frame: int
    end_frame: int
    status: Literal["pending", "ready", "failed"]
    url: str | None = None
    byte_size: int | None = None
    generation_mode: Literal["smart_copy", "transcode"] | None = None
    diagnostics: VideoChunkDiagnostics | None = None
    retry_after: int | None = None
    error: str | None = None


class VideoChunksResponse(BaseModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    chunk_size_frames: int
    fallback_video_url: str | None = None
    chunks: list[VideoChunkOut]


class VideoChunkSampleOut(BaseModel):
    frame_index: int
    pts_ms: int
    duration_ms: int
    is_keyframe: bool
    size_bytes: int
    offset_in_chunk: int


class VideoChunkSamplesResponse(BaseModel):
    dataset_item_id: UUID
    chunk_id: int
    codec_string: str
    # base64 编码的 AVC/HEVCDecoderConfigurationRecord (avcC/hvcC)，WebCodecs
    # VideoDecoderConfig.description 所需；旧 chunk 无 extradata 时为 None。
    description: str | None = None
    width: int
    height: int
    samples: list[VideoChunkSampleOut]


class VideoFrameOut(BaseModel):
    frame_index: int
    width: int
    format: Literal["webp", "jpeg"]
    status: Literal["pending", "ready", "failed"]
    url: str | None = None
    retry_after: int | None = None
    error: str | None = None


class VideoFramePrefetchRequest(BaseModel):
    frame_indices: list[int] = Field(default_factory=list, min_length=1, max_length=500)
    width: int = Field(default=512, ge=1, le=4096)
    format: Literal["webp", "jpeg"] = "webp"


class VideoFrameRetryRequest(BaseModel):
    frame_indices: list[int] = Field(default_factory=list, max_length=500)
    width: int = Field(default=512, ge=1, le=4096)
    format: Literal["webp", "jpeg"] = "webp"
    force: bool = False


class VideoFramePrefetchResponse(BaseModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    frames: list[VideoFrameOut]


class VideoSegmentOut(BaseModel):
    id: UUID
    segment_index: int
    start_frame: int
    end_frame: int
    work_start_frame: int
    work_end_frame: int
    status: Literal["open", "assigned", "locked", "completed"]
    assignee_id: UUID | None = None
    locked_by: UUID | None = None
    locked_at: datetime | None = None
    lock_expires_at: datetime | None = None


class VideoSegmentsResponse(BaseModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    segment_size_frames: int
    collaboration_enabled: bool = False
    overlap_frames: int = 0
    segments: list[VideoSegmentOut]


class VideoManifestV2Response(BaseModel):
    task_id: UUID | None = None
    dataset_item_id: UUID
    video_url: str | None = None
    poster_url: str | None = None
    fps: float | None = None
    frame_count: int | None = None
    duration_ms: int | None = None
    chunks_manifest_url: str
    frame_timetable_url: str
    frame_service_base: str
    chunk_size_frames: int
    segments: list[VideoSegmentOut] = Field(default_factory=list)
    frame_cache_formats: list[Literal["webp", "jpeg"]] = ["webp", "jpeg"]
    expires_in: int = 3600
