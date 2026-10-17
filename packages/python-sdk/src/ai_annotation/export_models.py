"""Typed request models for project and batch exports."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ai_annotation.models import _AAPModel


class VideoExportSegmentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["segments"] = Field(
        default="segments", description="Select an inclusive range of video segments."
    )
    start_segment_id: uuid.UUID = Field(
        description="UUID of the first included video segment."
    )
    end_segment_id: uuid.UUID = Field(
        description="UUID of the last included video segment."
    )


class VideoExportFrameSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["frames"] = Field(
        default="frames", description="Select an inclusive range of source frames."
    )
    from_frame: int = Field(
        ge=0, description="Inclusive zero-based source frame at the start of the range."
    )
    to_frame: int = Field(
        ge=0, description="Inclusive zero-based source frame at the end of the range."
    )

    @model_validator(mode="after")
    def validate_order(self) -> VideoExportFrameSelection:
        if self.to_frame < self.from_frame:
            raise ValueError("to_frame must be greater than or equal to from_frame")
        return self


VideoExportSelection = Annotated[
    VideoExportSegmentSelection | VideoExportFrameSelection,
    Field(discriminator="kind"),
]


class VideoExportScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID = Field(description="UUID of the video task to export.")
    selection: VideoExportSelection = Field(
        description="Inclusive source-frame or segment range for the video task."
    )


class LidarExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kitti_camera_role: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
        pattern=r"^camera_[A-Za-z0-9_.-]+$",
        description="Selected KITTI camera role, for example camera_front.",
    )


class ExportRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: VideoExportScopeRequest | None = None
    lidar: LidarExportOptions | None = None


class LidarExportPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    targets: list[str] = Field(min_length=1)
    lidar: LidarExportOptions | None = None


class LidarExportIssue(_AAPModel):
    code: str
    message: str
    task_id: uuid.UUID | None = None
    task_display_id: str | None = None
    frame_key: str | None = None
    camera_role: str | None = None


class LidarExportPreflightResult(_AAPModel):
    ready: bool
    camera_roles: list[str]
    selected_camera_role: str | None = None
    checked_tasks: int
    issue_count: int
    issues_truncated: bool = False
    issues: list[LidarExportIssue]
