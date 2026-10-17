"""Storage diagnostics and video-asset maintenance contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

VideoAssetKind = Literal["probe", "poster", "frame_timetable", "chunk", "frame"]


class _Response(BaseModel):
    model_config = ConfigDict(extra="allow")


class StorageHealth(_Response):
    status: str
    bucket: str


class BucketSummary(_Response):
    name: str
    status: str
    role: str
    object_count: int = 0
    total_size_bytes: int = 0
    error: str | None = None


class BucketsResponse(_Response):
    items: list[BucketSummary]
    total_object_count: int
    total_size_bytes: int


class VideoAssetFailure(_Response):
    asset_key: str
    asset_type: VideoAssetKind
    dataset_item_id: UUID
    file_name: str
    error: str
    task_id: UUID | None = None
    task_display_id: str | None = None
    project_id: UUID | None = None
    project_name: str | None = None
    updated_at: datetime | None = None
    chunk_id: int | None = None
    frame_index: int | None = None
    width: int | None = None
    format: str | None = None


class VideoAssetFailuresResponse(_Response):
    items: list[VideoAssetFailure]
    total: int
    limit: int
    offset: int


class VideoAssetRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_type: VideoAssetKind
    dataset_item_id: UUID
    chunk_id: int | None = None
    frame_index: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1, le=4096)
    format: Literal["webp", "jpeg"] | None = None

    @model_validator(mode="after")
    def required_locator(self) -> VideoAssetRetryRequest:
        if self.asset_type == "chunk" and self.chunk_id is None:
            raise ValueError("chunk_id is required for chunk retry")
        if self.asset_type == "frame" and any(
            value is None for value in (self.frame_index, self.width, self.format)
        ):
            raise ValueError(
                "frame_index, width and format are required for frame retry"
            )
        return self


class VideoAssetRetryResponse(_Response):
    status: Literal["queued"]
    asset_type: VideoAssetKind
    dataset_item_id: UUID
