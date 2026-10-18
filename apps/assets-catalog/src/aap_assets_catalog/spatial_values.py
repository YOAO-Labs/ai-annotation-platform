"""Portable value types for spatial Assets facts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Each convention describes the source frame mapped to the platform ISO frame.
LidarAxisConvention = Literal[
    "iso_8855",
    "ros_rep103",
    "kitti_camera",
    "opencv_camera",
    "apollo",
    "y_forward",
    "sustechpoints_demo",
    "raw",
]
AxisFrame = Literal["iso", "source"]


class SensorCalibration(BaseModel):
    """v0.13.1 · 相机标定。extrinsic row-major 4x4 外参(16), intrinsic row-major
    3x3 内参(9), rect 为 KITTI 可选矫正矩阵 4x4(16)。存进 DatasetItem.metadata_
    的 "calibration" key。投影: extrinsic·[x,y,z,1] → xyz → intrinsic·xyz → 透视除法。"""

    extrinsic: list[float] = Field(min_length=16, max_length=16)
    intrinsic: list[float] = Field(min_length=9, max_length=9)
    rect: list[float] | None = Field(default=None, min_length=16, max_length=16)

    model_config = ConfigDict(extra="forbid")


class FramePose(BaseModel):
    frame_index: int = Field(..., ge=0)
    timestamp_us: int | None = None
    # ego→global:translation [x,y,z],rotation 四元数 [w,x,y,z](nuScenes 原样)
    ego_translation: list[float] = Field(..., min_length=3, max_length=3)
    ego_rotation: list[float] = Field(..., min_length=4, max_length=4)
    source_metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


def calibration_digest(calibration: SensorCalibration) -> str:
    """Hash the exact canonical JSON representation used by calibration history."""
    encoded = json.dumps(
        calibration.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = (
    "AxisFrame",
    "FramePose",
    "LidarAxisConvention",
    "SensorCalibration",
    "calibration_digest",
)
