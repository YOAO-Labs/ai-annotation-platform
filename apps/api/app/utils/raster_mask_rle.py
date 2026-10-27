"""COCO uncompressed RLE codec for API validation and export paths.

The tracker backends use ``apps/_shared/mask_utils``. This API-local copy keeps
the production API image independent of sibling build contexts; shared golden
fixtures assert identical behavior across runtimes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MAX_IMAGE_MASK_DIMENSION = 8192
MAX_IMAGE_MASK_PIXELS = 67_108_864
MAX_VIDEO_MASK_DIMENSION = 4096
MAX_VIDEO_MASK_PIXELS = 16_777_216
MAX_DENSE_MASK_PIXELS = MAX_VIDEO_MASK_PIXELS
# Backward-compatible names describe the structural/canonical RLE envelope.
# Task-aware boundaries narrow video and interactive-AI payloads separately.
MAX_MASK_DIMENSION = MAX_IMAGE_MASK_DIMENSION
MAX_MASK_PIXELS = MAX_IMAGE_MASK_PIXELS
MAX_MASK_RUNS = 1_000_000


def _strict_positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_coco_rle(rle: Mapping[str, Any]) -> tuple[int, int, list[int]]:
    if rle.get("encoding") != "coco_rle":
        raise ValueError("encoding must be 'coco_rle'")
    size = rle.get("size")
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("size must be [height, width]")
    height = _strict_positive_int(size[0], "height")
    width = _strict_positive_int(size[1], "width")
    if height > MAX_MASK_DIMENSION or width > MAX_MASK_DIMENSION:
        raise ValueError(f"mask dimensions must be <= {MAX_MASK_DIMENSION}")
    pixels = height * width
    if pixels > MAX_MASK_PIXELS:
        raise ValueError(f"mask pixels must be <= {MAX_MASK_PIXELS}")
    raw_counts = rle.get("counts")
    if not isinstance(raw_counts, list) or not raw_counts:
        raise ValueError("counts must be a non-empty integer array")
    if len(raw_counts) > MAX_MASK_RUNS:
        raise ValueError(f"mask runs must be <= {MAX_MASK_RUNS}")
    counts: list[int] = []
    total = 0
    for index, value in enumerate(raw_counts):
        if type(value) is not int or value < 0:
            raise ValueError(f"counts[{index}] must be a non-negative integer")
        counts.append(value)
        total += value
        if total > pixels:
            raise ValueError("sum(counts) exceeds height * width")
    if total != pixels:
        raise ValueError("sum(counts) must equal height * width")
    return height, width, counts


def encode_coco_rle(
    pixels_row_major: Sequence[int], width: int, height: int
) -> dict[str, Any]:
    width = _strict_positive_int(width, "width")
    height = _strict_positive_int(height, "height")
    if width > MAX_MASK_DIMENSION or height > MAX_MASK_DIMENSION:
        raise ValueError(f"mask dimensions must be <= {MAX_MASK_DIMENSION}")
    if len(pixels_row_major) != width * height:
        raise ValueError("pixel buffer length must equal width * height")
    counts: list[int] = []
    foreground = False
    run_length = 0
    for x in range(width):
        for y in range(height):
            value = bool(pixels_row_major[y * width + x])
            if value == foreground:
                run_length += 1
            else:
                counts.append(run_length)
                run_length = 1
                foreground = value
    counts.append(run_length)
    if len(counts) > MAX_MASK_RUNS:
        raise ValueError(f"mask runs must be <= {MAX_MASK_RUNS}")
    return {"encoding": "coco_rle", "size": [height, width], "counts": counts}


def decode_coco_rle(rle: Mapping[str, Any]) -> bytearray:
    height, width, counts = validate_coco_rle(rle)
    out = bytearray(width * height)
    offset = 0
    foreground = False
    for run_length in counts:
        if foreground:
            for column_major_index in range(offset, offset + run_length):
                x, y = divmod(column_major_index, height)
                out[y * width + x] = 1
        offset += run_length
        foreground = not foreground
    return out


def coco_rle_area(rle: Mapping[str, Any]) -> int:
    """Return the foreground pixel count without materializing the mask."""
    _, _, counts = validate_coco_rle(rle)
    return sum(counts[1::2])


def coco_rle_bbox_pixels(rle: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    """Return tight (left, top, right, bottom) bounds without decoding pixels."""
    height, width, counts = validate_coco_rle(rle)
    offset = 0
    foreground = False
    min_x, min_y = width, height
    max_x = max_y = -1
    for run_length in counts:
        if foreground and run_length:
            start = offset
            end = offset + run_length - 1
            start_x, start_y = divmod(start, height)
            end_x, end_y = divmod(end, height)
            min_x = min(min_x, start_x)
            max_x = max(max_x, end_x)
            if start_x == end_x:
                min_y = min(min_y, start_y)
                max_y = max(max_y, end_y)
            else:
                min_y = 0
                max_y = height - 1
        offset += run_length
        foreground = not foreground
    if max_x < min_x or max_y < min_y:
        return None
    return min_x, min_y, max_x + 1, max_y + 1


def coco_rle_bbox_norm(rle: Mapping[str, Any]) -> dict[str, float]:
    """Return the tight normalized AABB without materializing the mask."""
    bounds = coco_rle_bbox_pixels(rle)
    if bounds is None:
        return {}
    height, width = rle["size"]
    left, top, right, bottom = bounds
    return {
        "x": left / width,
        "y": top / height,
        "w": (right - left) / width,
        "h": (bottom - top) / height,
    }
