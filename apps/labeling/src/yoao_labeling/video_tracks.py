from __future__ import annotations

import math
from typing import Any

VIDEO_FRAME_MODES = {"keyframes", "all_frames"}


def is_polygon_track(geometry: dict) -> bool:
    """v0.21.20 · geometry 是否为 polygon track (关键帧存 points 而非 bbox)。"""
    return geometry.get("type") == "video_track_polygon"


def is_polyline_track(geometry: dict) -> bool:
    """v0.21.20 · geometry 是否为 polyline track (开路径 points, 不闭合)。"""
    return geometry.get("type") == "video_track_polyline"


def is_mask_track(geometry: dict) -> bool:
    return geometry.get("type") == "video_track_mask"


def _is_points_track(geometry: dict) -> bool:
    """polygon / polyline track: 关键帧存 points; 二者共享 points 形状分派。"""
    return is_polygon_track(geometry) or is_polyline_track(geometry)


def _clean_frame(value: object) -> int | None:
    try:
        frame = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0, frame)


def sorted_keyframes(geometry: dict) -> list[dict]:
    keyframes = geometry.get("keyframes")
    if not isinstance(keyframes, list):
        return []
    return sorted(
        [kf for kf in keyframes if isinstance(kf, dict)],
        key=lambda kf: int(kf.get("frame_index", 0)),
    )


def clean_keyframe(kf: dict, *, include_attributes: bool = True) -> dict:
    # v0.21.20 · polygon 关键帧存 points, bbox 关键帧存 bbox; 按存在的形状键保留。
    row: dict = {
        "frame_index": int(kf.get("frame_index", 0)),
        "source": kf.get("source", "manual"),
        "occluded": bool(kf.get("occluded", False)),
    }
    if kf.get("mask") is not None:
        row["mask"] = dict(kf["mask"])
    elif kf.get("points") is not None:
        row["points"] = [list(pt) for pt in (kf.get("points") or [])]
    else:
        row["bbox"] = kf.get("bbox") or {}
    if include_attributes and isinstance(kf.get("attributes"), dict):
        row["attributes"] = kf["attributes"]
    return row


def clean_outside_range(range_: dict) -> dict | None:
    from_frame = _clean_frame(range_.get("from"))
    to_frame = _clean_frame(range_.get("to"))
    if from_frame is None or to_frame is None:
        return None
    start = min(from_frame, to_frame)
    end = max(from_frame, to_frame)
    return {
        "from": start,
        "to": end,
        "source": "prediction" if range_.get("source") == "prediction" else "manual",
    }


def normalize_outside_ranges(ranges: list[dict] | None) -> list[dict]:
    cleaned = [
        range_
        for range_ in (clean_outside_range(item) for item in ranges or [])
        if range_ is not None
    ]
    cleaned.sort(key=lambda item: (item["from"], item["to"], item["source"]))
    merged: list[dict] = []
    for range_ in cleaned:
        previous = merged[-1] if merged else None
        if (
            previous
            and previous["source"] == range_["source"]
            and range_["from"] <= previous["to"] + 1
        ):
            previous["to"] = max(previous["to"], range_["to"])
            continue
        merged.append(dict(range_))
    return merged


def remove_frame_from_outside_ranges(
    ranges: list[dict] | None, frame_index: int
) -> list[dict]:
    """Make one frame visible while preserving every surrounding outside range."""

    frame = max(0, int(frame_index))
    result: list[dict] = []
    for range_ in normalize_outside_ranges(ranges):
        if frame < range_["from"] or frame > range_["to"]:
            result.append(range_)
            continue
        if range_["from"] < frame:
            result.append({**range_, "to": frame - 1})
        if frame < range_["to"]:
            result.append({**range_, "from": frame + 1})
    return normalize_outside_ranges(result)


def remove_manual_frame_from_outside_ranges(
    ranges: list[dict] | None, frame_index: int
) -> tuple[list[dict], bool]:
    """Remove one frame only from manual outside ranges.

    Prediction-owned ranges remain authoritative.  The boolean reports whether a
    manual range actually covered the frame so callers can distinguish a no-op
    from a successful held restore.
    """

    frame = max(0, int(frame_index))
    result: list[dict] = []
    removed = False
    for range_ in normalize_outside_ranges(ranges):
        if (
            range_["source"] != "manual"
            or frame < range_["from"]
            or frame > range_["to"]
        ):
            result.append(range_)
            continue
        removed = True
        if range_["from"] < frame:
            result.append({**range_, "to": frame - 1})
        if frame < range_["to"]:
            result.append({**range_, "from": frame + 1})
    return normalize_outside_ranges(result), removed


def effective_outside_ranges(geometry: dict) -> list[dict]:
    return normalize_outside_ranges(geometry.get("outside") or [])


def frame_is_outside(geometry: dict, frame_index: int) -> bool:
    return any(
        int(range_["from"]) <= frame_index <= int(range_["to"])
        for range_ in effective_outside_ranges(geometry)
    )


def range_intersects_outside(
    ranges: list[dict], from_frame: int, to_frame: int
) -> bool:
    start = min(from_frame, to_frame)
    end = max(from_frame, to_frame)
    return any(
        int(range_["from"]) <= end and int(range_["to"]) >= start for range_ in ranges
    )


def lerp_bbox(before: dict, after: dict, ratio: float) -> dict:
    before_bbox = before.get("bbox") or {}
    after_bbox = after.get("bbox") or {}
    return {
        key: round(
            float(before_bbox.get(key, 0))
            + (float(after_bbox.get(key, 0)) - float(before_bbox.get(key, 0))) * ratio,
            6,
        )
        for key in ("x", "y", "w", "h")
    }


def _resample_closed_polygon(points: list, n: int) -> list[list[float]]:
    """把闭合多边形按弧长重采样为 n 个等距顶点 (从 index 0 起)。

    顶点数不等的两帧插值前先各自重采样到公共 n, 使顶点一一对应。退化 (顶点<2 /
    周长 0) 时回退到复制首点, 不抛异常。
    """
    pts = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
    if n <= 0 or len(pts) < 2:
        return [list(p) for p in pts]
    seg_lengths = [
        math.hypot(
            pts[(i + 1) % len(pts)][0] - pts[i][0],
            pts[(i + 1) % len(pts)][1] - pts[i][1],
        )
        for i in range(len(pts))
    ]
    perim = sum(seg_lengths)
    if perim == 0:
        return [list(pts[0]) for _ in range(n)]
    cum = [0.0]
    for length in seg_lengths:
        cum.append(cum[-1] + length)
    step = perim / n
    out: list[list[float]] = []
    for k in range(n):
        target = k * step
        seg = 0
        while seg < len(seg_lengths) - 1 and cum[seg + 1] < target:
            seg += 1
        seg_len = seg_lengths[seg]
        t = 0.0 if seg_len == 0 else (target - cum[seg]) / seg_len
        x0, y0 = pts[seg]
        x1, y1 = pts[(seg + 1) % len(pts)]
        out.append([round(x0 + (x1 - x0) * t, 6), round(y0 + (y1 - y0) * t, 6)])
    return out


def _best_rotation_offset(a: list[list[float]], b: list[list[float]]) -> int:
    """在 b 的 n 个循环起点里选与 a 逐点距离和最小者, 减少插值中途扭曲。"""
    n = len(a)
    if n == 0 or len(b) != n:
        return 0
    best_offset, best_cost = 0, float("inf")
    for offset in range(n):
        cost = 0.0
        for i in range(n):
            bx, by = b[(i + offset) % n]
            cost += (a[i][0] - bx) ** 2 + (a[i][1] - by) ** 2
        if cost < best_cost:
            best_cost, best_offset = cost, offset
    return best_offset


def lerp_polygon(before: dict, after: dict, ratio: float) -> list[list[float]]:
    """v0.21.20 · polygon 关键帧插值: 弧长参数化重采样到公共顶点数 + 旋转对齐后逐点 lerp。"""
    a = [p for p in (before.get("points") or []) if len(p) >= 2]
    b = [p for p in (after.get("points") or []) if len(p) >= 2]
    if not a:
        return [list(p) for p in b]
    if not b:
        return [list(p) for p in a]
    n = max(len(a), len(b))
    ra = _resample_closed_polygon(a, n)
    rb = _resample_closed_polygon(b, n)
    offset = _best_rotation_offset(ra, rb)
    return [
        [
            round(ra[i][0] + (rb[(i + offset) % n][0] - ra[i][0]) * ratio, 6),
            round(ra[i][1] + (rb[(i + offset) % n][1] - ra[i][1]) * ratio, 6),
        ]
        for i in range(n)
    ]


def _resample_open_polyline(points: list, n: int) -> list[list[float]]:
    """把开路径折线按弧长重采样为 n 个等距顶点 (含首尾端点, 不闭合)。

    与 _resample_closed_polygon 的区别: 段数 = 顶点数-1 (无回环闭合边),
    采样步长 = 全长/(n-1) 使两端点必被采到。退化时安全回退。
    """
    pts = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
    if n <= 1 or len(pts) < 2:
        return [list(p) for p in pts]
    seg_lengths = [
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    ]
    total = sum(seg_lengths)
    if total == 0:
        return [list(pts[0]) for _ in range(n)]
    cum = [0.0]
    for length in seg_lengths:
        cum.append(cum[-1] + length)
    step = total / (n - 1)
    out: list[list[float]] = []
    for k in range(n):
        target = min(k * step, total)
        seg = 0
        while seg < len(seg_lengths) - 1 and cum[seg + 1] < target:
            seg += 1
        seg_len = seg_lengths[seg]
        t = 0.0 if seg_len == 0 else (target - cum[seg]) / seg_len
        x0, y0 = pts[seg]
        x1, y1 = pts[seg + 1]
        out.append([round(x0 + (x1 - x0) * t, 6), round(y0 + (y1 - y0) * t, 6)])
    return out


def lerp_polyline(before: dict, after: dict, ratio: float) -> list[list[float]]:
    """v0.21.20 · polyline (开路径) 关键帧插值: 开路径弧长重采样到公共顶点数 + 逐点 lerp。

    与 lerp_polygon 的区别: 开路径重采样 (首尾端点固定对应) + 无旋转对齐 (端点即对应)。
    """
    a = [p for p in (before.get("points") or []) if len(p) >= 2]
    b = [p for p in (after.get("points") or []) if len(p) >= 2]
    if not a:
        return [list(p) for p in b]
    if not b:
        return [list(p) for p in a]
    n = max(len(a), len(b))
    ra = _resample_open_polyline(a, n)
    rb = _resample_open_polyline(b, n)
    return [
        [
            round(ra[i][0] + (rb[i][0] - ra[i][0]) * ratio, 6),
            round(ra[i][1] + (rb[i][1] - ra[i][1]) * ratio, 6),
        ]
        for i in range(len(ra))
    ]


def _shape_fields(kf: dict, *, points: bool) -> dict:
    """v0.21.20 · 按 track 类型取关键帧形状字段: polygon/polyline→points, bbox→bbox。"""
    if points:
        return {"points": [list(pt) for pt in (kf.get("points") or [])]}
    return {"bbox": kf.get("bbox") or {}}


def _coerce_geometry(geometry_or_keyframes: dict | list[dict]) -> dict:
    if isinstance(geometry_or_keyframes, list):
        return {
            "type": "video_track_bbox",
            "track_id": "",
            "keyframes": geometry_or_keyframes,
        }
    return geometry_or_keyframes


def resolve_mask_track_state_at_frame(geometry: dict, frame_index: int) -> dict:
    """Resolve a mask track without conflating model absence with manual outside.

    Persisted mask tracks still use the existing ``outside`` ranges. Prediction
    ranges mean that a model explicitly observed no instance (``absent``), while
    manual ranges retain the user-authored ``outside`` meaning. The compatibility
    resolver below continues returning ``None`` for either state.
    """

    if not is_mask_track(geometry):
        raise ValueError("detailed mask resolution requires video_track_mask")
    frame = max(0, int(frame_index))
    covering_ranges = [
        range_
        for range_ in effective_outside_ranges(geometry)
        if int(range_["from"]) <= frame <= int(range_["to"])
    ]
    if covering_ranges:
        state = (
            "outside"
            if any(range_["source"] == "manual" for range_ in covering_ranges)
            else "absent"
        )
        return {
            "frame_index": frame,
            "state": state,
            "resolved_from_frame": None,
            "source": "manual" if state == "outside" else "prediction",
            "mask": None,
            "confidence": None,
            "correction_lineage": None,
        }

    keyframes = sorted_keyframes(geometry)
    candidates = [
        keyframe
        for keyframe in keyframes
        if not range_intersects_outside(
            effective_outside_ranges(geometry),
            int(keyframe.get("frame_index", 0)),
            int(keyframe.get("frame_index", 0)),
        )
    ]
    if not candidates:
        return {
            "frame_index": frame,
            "state": "outside",
            "resolved_from_frame": None,
            "source": None,
            "mask": None,
            "confidence": None,
            "correction_lineage": None,
        }
    selected = min(
        candidates,
        key=lambda keyframe: (
            abs(int(keyframe.get("frame_index", 0)) - frame),
            int(keyframe.get("frame_index", 0)),
        ),
    )
    selected_frame = int(selected.get("frame_index", 0))
    state = (
        "occluded"
        if bool(selected.get("occluded", False))
        else ("exact" if selected_frame == frame else "held")
    )
    resolved = {
        "frame_index": frame,
        "state": state,
        "resolved_from_frame": selected_frame,
        "source": selected.get("source", "manual"),
        "mask": dict(selected.get("mask") or {}),
        "confidence": selected.get("confidence"),
        "correction_lineage": selected.get("correction_lineage"),
        "occluded": bool(selected.get("occluded", False)),
    }
    if isinstance(selected.get("bbox"), dict):
        resolved["bbox"] = dict(selected["bbox"])
    if isinstance(selected.get("mask_rle"), dict):
        resolved["mask_rle"] = dict(selected["mask_rle"])
    if isinstance(selected.get("attributes"), dict):
        resolved["attributes"] = selected["attributes"]
    return resolved


def resolve_track_at_frame(
    geometry_or_keyframes: dict | list[dict], frame_index: int
) -> dict | None:
    geometry = _coerce_geometry(geometry_or_keyframes)
    points_track = _is_points_track(geometry)
    keyframes = sorted_keyframes(geometry)
    outside_ranges = effective_outside_ranges(geometry)
    if range_intersects_outside(outside_ranges, frame_index, frame_index):
        return None

    if is_mask_track(geometry):
        detailed = resolve_mask_track_state_at_frame(geometry, frame_index)
        if detailed["state"] in {"absent", "outside"}:
            return None
        resolved = {
            "frame_index": frame_index,
            "mask": detailed["mask"],
            "source": detailed["source"],
            "occluded": detailed["state"] == "occluded",
        }
        for field in ("bbox", "mask_rle", "attributes"):
            if field in detailed:
                resolved[field] = detailed[field]
        return resolved

    exact = next(
        (kf for kf in keyframes if int(kf.get("frame_index", 0)) == frame_index),
        None,
    )
    if exact:
        return {
            "frame_index": frame_index,
            **_shape_fields(exact, points=points_track),
            "source": exact.get("source", "manual"),
            "occluded": bool(exact.get("occluded", False)),
        }

    before = next(
        (
            kf
            for kf in reversed(keyframes)
            if int(kf.get("frame_index", 0)) < frame_index
            and not range_intersects_outside(
                outside_ranges,
                int(kf.get("frame_index", 0)),
                int(kf.get("frame_index", 0)),
            )
        ),
        None,
    )
    after = next(
        (
            kf
            for kf in keyframes
            if int(kf.get("frame_index", 0)) > frame_index
            and not range_intersects_outside(
                outside_ranges,
                int(kf.get("frame_index", 0)),
                int(kf.get("frame_index", 0)),
            )
        ),
        None,
    )
    if not before or not after:
        return None
    before_frame = int(before.get("frame_index", 0))
    after_frame = int(after.get("frame_index", 0))
    if after_frame == before_frame or range_intersects_outside(
        outside_ranges, before_frame + 1, after_frame - 1
    ):
        return None
    ratio = (frame_index - before_frame) / (after_frame - before_frame)
    if is_polygon_track(geometry):
        interp_shape = {"points": lerp_polygon(before, after, ratio)}
    elif is_polyline_track(geometry):
        interp_shape = {"points": lerp_polyline(before, after, ratio)}
    else:
        interp_shape = {"bbox": lerp_bbox(before, after, ratio)}
    return {
        "frame_index": frame_index,
        **interp_shape,
        "source": "interpolated",
        "occluded": False,
    }


def resolved_track_frames(
    geometry: dict,
    *,
    frame_mode: str,
    frame_count: int | None = None,
) -> list[dict]:
    if frame_mode not in VIDEO_FRAME_MODES:
        raise ValueError("video_frame_mode must be one of: keyframes, all_frames")

    points_track = _is_points_track(geometry)
    keyframes = sorted_keyframes(geometry)
    outside_ranges = effective_outside_ranges(geometry)
    if frame_mode == "keyframes":
        if is_mask_track(geometry):
            return [
                {
                    "frame_index": int(kf.get("frame_index", 0)),
                    "mask": dict(kf.get("mask") or {}),
                    "source": kf.get("source", "manual"),
                    "occluded": bool(kf.get("occluded", False)),
                    **(
                        {"attributes": kf["attributes"]}
                        if isinstance(kf.get("attributes"), dict)
                        else {}
                    ),
                }
                for kf in keyframes
                if not range_intersects_outside(
                    outside_ranges,
                    int(kf.get("frame_index", 0)),
                    int(kf.get("frame_index", 0)),
                )
            ]
        return [
            {
                "frame_index": int(kf.get("frame_index", 0)),
                **_shape_fields(kf, points=points_track),
                "source": kf.get("source", "manual"),
                "occluded": bool(kf.get("occluded", False)),
            }
            for kf in keyframes
            if not range_intersects_outside(
                outside_ranges,
                int(kf.get("frame_index", 0)),
                int(kf.get("frame_index", 0)),
            )
        ]

    max_keyframe = max((int(kf.get("frame_index", 0)) for kf in keyframes), default=0)
    total = max(int(frame_count or max_keyframe + 1), max_keyframe + 1)
    return [
        resolved
        for frame_index in range(total)
        if (resolved := resolve_track_at_frame(geometry, frame_index))
    ]


def _first_keyframe_frame(geometry: dict) -> int:
    keyframes = sorted_keyframes(geometry)
    if not keyframes:
        return 0
    return int(keyframes[0].get("frame_index", 0))


def derive_track_number(tracks: list[tuple[Any, dict]]) -> dict[Any, int]:
    """v0.10.30 · D-2.1a 确定性派生 track_number, 不持久化。

    输入: task 内所有 active video_track 的 ``(annotation_id, geometry)`` 列表。
    规则: 按首关键帧 ``frame_index`` 升序、并列再按 ``track_id`` 字典序, 返回
    ``{annotation_id: 1..N}``。改采样 / 增删 track 时编号自然重排, 符合 D2。
    """
    ordered = sorted(
        tracks,
        key=lambda item: (
            _first_keyframe_frame(item[1] or {}),
            str((item[1] or {}).get("track_id") or ""),
        ),
    )
    return {annotation_id: index for index, (annotation_id, _) in enumerate(ordered, 1)}


__all__ = [
    "VIDEO_FRAME_MODES",
    "clean_keyframe",
    "clean_outside_range",
    "derive_track_number",
    "effective_outside_ranges",
    "frame_is_outside",
    "is_mask_track",
    "is_polygon_track",
    "is_polyline_track",
    "lerp_bbox",
    "lerp_polygon",
    "lerp_polyline",
    "normalize_outside_ranges",
    "range_intersects_outside",
    "remove_frame_from_outside_ranges",
    "remove_manual_frame_from_outside_ranges",
    "resolve_mask_track_state_at_frame",
    "resolve_track_at_frame",
    "resolved_track_frames",
    "sorted_keyframes",
    "_best_rotation_offset",
    "_clean_frame",
    "_coerce_geometry",
    "_first_keyframe_frame",
    "_is_points_track",
    "_resample_closed_polygon",
    "_resample_open_polyline",
    "_shape_fields",
]
