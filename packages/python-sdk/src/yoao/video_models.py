"""Typed request and response models for video assets and chapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _VideoModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class VideoMetadata(_VideoModel):
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


class TaskVideoManifestResponse(_VideoModel):
    task_id: UUID
    dataset_item_id: UUID | None = None
    video_url: str
    poster_url: str | None = None
    metadata: VideoMetadata
    expires_in: int = 3600


class VideoSegmentOut(_VideoModel):
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


class VideoSegmentsResponse(_VideoModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    segment_size_frames: int
    collaboration_enabled: bool = False
    overlap_frames: int = 0
    segments: list[VideoSegmentOut]


class VideoFrameTimetableEntry(_VideoModel):
    frame_index: int
    pts_ms: int
    is_keyframe: bool
    pict_type: str | None = None
    byte_offset: int | None = None


class TaskVideoFrameTimetableResponse(_VideoModel):
    task_id: UUID
    fps: float | None = None
    frame_count: int | None = None
    source: Literal["ffprobe", "estimated"]
    frames: list[VideoFrameTimetableEntry]


class VideoChunkDiagnostics(_VideoModel):
    source_codec: str | None = None
    output_codec: str | None = None
    keyframe_aligned: bool | None = None
    start_byte_offset: int | None = None
    end_byte_offset: int | None = None
    smart_copy_eligible: bool | None = None
    fallback_reason: str | None = None


class VideoChunkOut(_VideoModel):
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


class VideoChunksResponse(_VideoModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    chunk_size_frames: int
    fallback_video_url: str | None = None
    chunks: list[VideoChunkOut]


class VideoChunkSampleOut(_VideoModel):
    frame_index: int
    pts_ms: int
    duration_ms: int
    is_keyframe: bool
    size_bytes: int
    offset_in_chunk: int


class VideoChunkSamplesResponse(_VideoModel):
    dataset_item_id: UUID
    chunk_id: int
    codec_string: str
    description: str | None = None
    width: int
    height: int
    samples: list[VideoChunkSampleOut]


class VideoFrameOut(_VideoModel):
    frame_index: int
    width: int
    format: Literal["webp", "jpeg"]
    status: Literal["pending", "ready", "failed"]
    url: str | None = None
    retry_after: int | None = None
    error: str | None = None


class VideoFramePrefetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_indices: list[int] = Field(default_factory=list, min_length=1, max_length=500)
    width: int = Field(default=512, ge=1, le=4096)
    format: Literal["webp", "jpeg"] = "webp"


class VideoFrameRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_indices: list[int] = Field(default_factory=list, max_length=500)
    width: int = Field(default=512, ge=1, le=4096)
    format: Literal["webp", "jpeg"] = "webp"
    force: bool = False


class VideoFramePrefetchResponse(_VideoModel):
    dataset_item_id: UUID
    task_id: UUID | None = None
    frames: list[VideoFrameOut]


class VideoManifestV2Response(_VideoModel):
    task_id: UUID | None = None
    dataset_item_id: UUID
    video_url: str
    poster_url: str | None = None
    fps: float | None = None
    frame_count: int | None = None
    duration_ms: int | None = None
    chunks_manifest_url: str
    frame_timetable_url: str
    frame_service_base: str
    chunk_size_frames: int
    segments: list[VideoSegmentOut] = Field(default_factory=list)
    frame_cache_formats: list[Literal["webp", "jpeg"]] = Field(
        default_factory=lambda: ["webp", "jpeg"]
    )
    expires_in: int = 3600


ChapterSource = Literal["manual", "sampled"]
VideoImageFormat = Literal["webp", "jpeg"]


class VideoChapterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)
    frame_step: int | None = Field(default=None, ge=1)
    source: ChapterSource | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _check_frame_order(self) -> VideoChapterCreate:
        if self.end_frame < self.start_frame:
            raise ValueError("end_frame must be >= start_frame")
        return self


class VideoChapterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=40)
    metadata: dict[str, Any] | None = None
    frame_step: int | None = Field(default=None, ge=1)
    source: ChapterSource | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class VideoChapterOut(_VideoModel):
    id: UUID
    dataset_item_id: UUID
    start_frame: int
    end_frame: int
    title: str
    color: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    frame_step: int | None = None
    source: ChapterSource = "manual"
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None


class VideoChapterList(_VideoModel):
    chapters: list[VideoChapterOut] = Field(default_factory=list)
