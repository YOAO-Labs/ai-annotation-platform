from typing import Any

from .kernel import (
    Box3D,
    QualityFinding,
    QualityThresholds,
    TrackInterval,
    TrackMember,
    evaluate_box,
    evaluate_track,
    parse_pcd_positions,
)


class PointCloudQualityError(RuntimeError):
    def __init__(self, status_code: int, reason: str, **detail: Any):
        super().__init__(reason)
        self.status_code = status_code
        self.detail = {"reason": reason, **detail}


__all__ = [
    "Box3D",
    "QualityFinding",
    "QualityThresholds",
    "TrackInterval",
    "TrackMember",
    "evaluate_box",
    "evaluate_track",
    "parse_pcd_positions",
    "PointCloudQualityError",
]
