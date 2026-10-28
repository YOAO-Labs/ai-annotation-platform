"""Strict conversion from original-image pixels to protocol rectangle labels."""
from __future__ import annotations

import math
from collections.abc import Sequence

from .types import Detection


def rectangles(detections: Sequence[Detection], classes: Sequence[str], width: int, height: int) -> list[dict]:
    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    if not classes or any(not isinstance(name, str) or not name for name in classes):
        raise ValueError("model must provide a nonempty ordered class list")
    result = []
    for detection in detections:
        if not isinstance(detection, Detection):
            raise ValueError("predict must return Detection instances")
        index = detection.class_index
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(classes):
            raise ValueError("native class index is outside the model class list")
        x1, y1, x2, y2, score = detection.x1, detection.y1, detection.x2, detection.y2, detection.score
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x1, y1, x2, y2, score)):
            raise ValueError("coordinates and score must be finite numbers")
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height and 0 <= score <= 1):
            raise ValueError("box must be nonempty and within the original image; score must be in [0,1]")
        result.append({"type": "rectanglelabels", "native_class_index": index, "score": score,
                       "original_width": width, "original_height": height,
                       "value": {"x": 100 * x1 / width, "y": 100 * y1 / height,
                                 "width": 100 * (x2 - x1) / width, "height": 100 * (y2 - y1) / height,
                                 "rotation": 0, "rectanglelabels": [classes[index]]}})
    return result
