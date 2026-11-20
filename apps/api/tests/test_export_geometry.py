"""多几何导出写入器纯函数单测（无 DB）。

覆盖 COCO segmentation/keypoints/外接框 + YOLO det/obb/seg 行生成。
"""

from __future__ import annotations

import math

from app.services.exporting.service import (
    _coco_aabb_norm,
    _coco_keypoints,
    _coco_segmentation,
)
from app.services.exporting.packaging import (
    _seg_rings_norm,
    _yolo_target_lines,
)


class _Ann:
    """最小 annotation 替身，仅供 _yolo_target_lines 用。"""

    def __init__(self, class_name, geometry, attributes=None):
        self.class_name = class_name
        self.geometry = geometry
        self.attributes = attributes or {}


# ── COCO 外接框 ──


def test_aabb_bbox():
    assert _coco_aabb_norm(
        {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    ) == (
        0.1,
        0.2,
        0.3,
        0.4,
    )


def test_aabb_polygon():
    g = {"type": "polygon", "points": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.8]]}
    x, y, w, h = _coco_aabb_norm(g)
    assert (round(x, 3), round(y, 3), round(w, 3), round(h, 3)) == (0.1, 0.2, 0.4, 0.6)


def test_aabb_keypoint_skips_unlabeled():
    g = {
        "type": "keypoint",
        "points": [{"x": 0.2, "y": 0.2, "v": 2}, {"x": 0.6, "y": 0.9, "v": 0}],
    }
    # v=0 的点不计入外接框。
    assert _coco_aabb_norm(g) == (0.2, 0.2, 0.0, 0.0)


def test_aabb_rotated_and_polyline_skipped():
    assert (
        _coco_aabb_norm(
            {
                "type": "rotated_bbox",
                "cx": 0.5,
                "cy": 0.5,
                "w": 0.2,
                "h": 0.2,
                "angle": 30,
            }
        )
        is None
    )
    assert (
        _coco_aabb_norm({"type": "polyline", "points": [[0.1, 0.1], [0.9, 0.9]]})
        is None
    )


# ── COCO segmentation ──


def test_coco_segmentation_polygon_pixels():
    g = {"type": "polygon", "points": [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0]]}
    seg = _coco_segmentation(g, 100, 200)
    assert seg == [[0.0, 0.0, 50.0, 0.0, 50.0, 200.0]]


def test_coco_segmentation_multipolygon():
    g = {
        "type": "multi_polygon",
        "polygons": [
            {"type": "polygon", "points": [[0, 0], [0.1, 0], [0.1, 0.1]]},
            {"type": "polygon", "points": [[0.5, 0.5], [0.6, 0.5], [0.6, 0.6]]},
        ],
    }
    seg = _coco_segmentation(g, 10, 10)
    assert len(seg) == 2


def test_coco_segmentation_none_for_bbox():
    assert (
        _coco_segmentation({"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1}, 10, 10)
        is None
    )


# ── COCO keypoints ──


def test_coco_keypoints_flatten_and_count():
    g = {
        "type": "keypoint",
        "points": [{"x": 0.5, "y": 0.5, "v": 2}, {"x": 0.1, "y": 0.2, "v": 0}],
    }
    flat, n = _coco_keypoints(g, 100, 100)
    assert flat == [50.0, 50.0, 2, 10.0, 20.0, 0]
    assert n == 1


# ── YOLO det / obb / seg ──


def test_yolo_det_line():
    anns = [_Ann("car", {"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.2, "h": 0.4})]
    lines, _ = _yolo_target_lines(
        "yolo-det", anns, {"car": 0}, img_w=100, img_h=100, include_attributes=False
    )
    # cx=0.2 cy=0.4 w=0.2 h=0.4
    assert lines == ["0 0.200000 0.400000 0.200000 0.400000"]


def test_yolo_obb_corners_angle_zero():
    anns = [
        _Ann(
            "car",
            {
                "type": "rotated_bbox",
                "cx": 0.5,
                "cy": 0.5,
                "w": 0.2,
                "h": 0.2,
                "angle": 0,
            },
        )
    ]
    lines, _ = _yolo_target_lines(
        "yolo-obb", anns, {"car": 0}, img_w=100, img_h=100, include_attributes=False
    )
    parts = lines[0].split()
    assert parts[0] == "0"
    coords = [float(p) for p in parts[1:]]
    assert len(coords) == 8
    # angle=0 → 轴对齐四角，x ∈ {0.4, 0.6}, y ∈ {0.4, 0.6}
    assert all(
        math.isclose(c, 0.4, abs_tol=1e-6) or math.isclose(c, 0.6, abs_tol=1e-6)
        for c in coords
    )


def test_yolo_obb_skips_non_rotated():
    anns = [_Ann("car", {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2})]
    lines, _ = _yolo_target_lines(
        "yolo-obb", anns, {"car": 0}, img_w=100, img_h=100, include_attributes=False
    )
    assert lines == []


def test_yolo_seg_normalized_polygon():
    anns = [
        _Ann(
            "road", {"type": "polygon", "points": [[0.1, 0.2], [0.3, 0.2], [0.3, 0.5]]}
        )
    ]
    lines, _ = _yolo_target_lines(
        "yolo-seg", anns, {"road": 0}, img_w=100, img_h=100, include_attributes=False
    )
    assert lines == ["0 0.100000 0.200000 0.300000 0.200000 0.300000 0.500000"]


def test_seg_rings_norm_multipolygon_two_rings():
    g = {
        "type": "multi_polygon",
        "polygons": [
            {"type": "polygon", "points": [[0, 0], [0.1, 0], [0.1, 0.1]]},
            {"type": "polygon", "points": [[0.5, 0.5], [0.6, 0.5], [0.6, 0.6]]},
        ],
    }
    assert len(_seg_rings_norm(g)) == 2
