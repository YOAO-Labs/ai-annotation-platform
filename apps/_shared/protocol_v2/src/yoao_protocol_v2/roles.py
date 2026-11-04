"""Protocol v2.1 platform roles for backend parameter JSON Schema metadata."""

from __future__ import annotations

from enum import Enum


class PlatformRole(str, Enum):
    CONFIDENCE = "confidence"
    IOU = "iou"
    MAX_DET = "maxDet"
    TEXT_THRESHOLD = "textThreshold"
    SIMPLIFY_TOLERANCE = "simplifyTolerance"
    MODEL_VARIANT = "modelVariant"
