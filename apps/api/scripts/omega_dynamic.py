"""Timestamp and instance-safe primitives for the offline all-exposure study."""

from __future__ import annotations

import numpy as np
from numba import njit

from vggt_common import _box_geometry, interpolate_box


def overlapping_windows(count, width=144, stride=72):
    if count < 1 or width < 1 or not 0 < stride <= width:
        raise ValueError("invalid window specification")
    width = min(count, width)
    starts = list(range(0, count - width + 1, stride))
    if starts[-1] != count - width:
        starts.append(count - width)
    windows = [list(range(start, start + width)) for start in starts]
    owner = np.full(count, -1, np.int16)
    margin = np.full(count, -1, int)
    for i, indices in enumerate(windows):
        for local, source in enumerate(indices):
            score = min(local, width - local - 1)
            if score > margin[source]:
                owner[source], margin[source] = i, score
    if (owner < 0).any():
        raise ValueError("window coverage is incomplete")
    return windows, owner


def fast_voxel(points, voxel=0.1):
    """Collision-free packed voxel keys; keep the first actual source sample."""
    if voxel <= 0 or not np.isfinite(points).all():
        raise ValueError("invalid voxel input")
    cells = np.floor(points / voxel).astype(np.int64) + (1 << 20)
    if len(cells) and (cells.min() < 0 or cells.max() >= (1 << 21)):
        raise ValueError("voxel coordinates exceed the lossless key range")
    cells = cells.astype(np.uint64)
    keys = (cells[:, 0] << np.uint64(42)) | (cells[:, 1] << np.uint64(21)) | cells[:, 2]
    _, first = np.unique(keys, return_index=True)
    return np.sort(first)


def nearest_indices(rows, timestamp, maximum_us, limit=None):
    eligible = [
        i for i, r in enumerate(rows) if abs(r["timestamp"] - timestamp) <= maximum_us
    ]
    eligible.sort(
        key=lambda i: (abs(rows[i]["timestamp"] - timestamp), rows[i]["timestamp"], i)
    )
    return eligible if limit is None else eligible[:limit]


@njit(cache=False, nogil=True)
def _assign(points, centres, rotations, halves):
    labels = np.full(len(points), -1, np.int16)
    for i in range(len(points)):
        found = -1
        for j in range(len(centres)):
            inside = True
            for axis in range(3):
                value = 0.0
                for k in range(3):
                    value += (points[i, k] - centres[j, k]) * rotations[j, k, axis]
                if abs(value) > halves[j, axis]:
                    inside = False
                    break
            if inside:
                if found >= 0:
                    found = -2
                    break
                found = j
        labels[i] = found
    return labels


def assign_instances(points, boxes, padding=0.3):
    """An overlapping-box point is ambiguous and never assigned to either actor."""
    if not boxes:
        return np.full(len(points), -1, np.int16)
    geometry = [_box_geometry(b, padding) for b in boxes]
    return _assign(
        np.asarray(points, float),
        np.asarray([g[0] for g in geometry]),
        np.asarray([g[1] for g in geometry]),
        np.asarray([g[2] for g in geometry]),
    )


def warp_instance(points, source_box, target_box):
    """Rigid object motion in metric coordinates; box size never scales geometry."""
    if source_box["instance_token"] != target_box["instance_token"]:
        raise ValueError("motion must preserve instance identity")
    source = _box_geometry(source_box, 0)
    target = _box_geometry(target_box, 0)
    local = (np.asarray(points, float) - source[0]) @ source[1]
    return local @ target[1].T + target[0]


class ActorTracks:
    def __init__(self, tracks):
        self.tracks = {t[0]["instance_token"]: t for t in tracks}
        self.tokens = sorted(self.tracks)
        self.ids = {token: i for i, token in enumerate(self.tokens)}

    def box(self, token, timestamp, maximum_gap_us=750000):
        track = self.tracks[token]
        times = np.asarray([b["timestamp"] for b in track])
        if len(times) < 2 or timestamp < times[0] or timestamp > times[-1]:
            return None
        right = min(
            max(int(np.searchsorted(times, timestamp, side="right")), 1), len(times) - 1
        )
        if times[right] - times[right - 1] > maximum_gap_us:
            return None
        result = interpolate_box(track[right - 1 : right + 1], timestamp)
        if result.get("clamped_timestamp", False):
            raise ValueError("strict motion interpolation unexpectedly clamped")
        return result

    def boxes(self, timestamp):
        return [
            b for token in self.tokens if (b := self.box(token, timestamp)) is not None
        ]

    def rigid(self, token):
        category = self.tracks[token][0]["category"]
        return category.startswith(
            (
                "vehicle.car",
                "vehicle.truck",
                "vehicle.bus",
                "vehicle.trailer",
                "vehicle.construction",
                "vehicle.emergency",
            )
        )
