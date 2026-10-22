"""predictor.py 结果映射协议契约测试.

不需要 ultralytics / GPU. mock ultralytics Results 模拟 4 task 输出, 验证
predictor._emit_* 函数把 ultralytics 原始 tensor 映射成符合协议 v2 的 result.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock

import numpy as np
import pytest


def _mk_tensor(arr) -> MagicMock:
    """模拟 ultralytics tensor (.cpu().numpy()).返回真实 np.ndarray."""
    np_arr = np.asarray(arr, dtype=np.float32)
    m = MagicMock()
    m.cpu.return_value.numpy.return_value = np_arr
    return m


def test_emit_detection_basic() -> None:
    from predictor import _emit_detection

    r0 = MagicMock()
    r0.boxes = MagicMock()
    r0.boxes.__len__ = MagicMock(return_value=2)
    r0.boxes.xyxy = _mk_tensor([[10, 20, 110, 220], [30, 40, 130, 240]])
    r0.boxes.conf = _mk_tensor([0.9, 0.7])
    r0.boxes.cls = _mk_tensor([0, 2])
    names = {0: "person", 2: "car"}

    out = _emit_detection(r0, names, img_w=1000, img_h=1000)
    assert len(out) == 2

    first = out[0]
    assert first["type"] == "rectanglelabels"
    # 归一化: x=10/1000*100=1.0, y=2.0, w=10.0, h=20.0
    assert first["value"]["x"] == pytest.approx(1.0)
    assert first["value"]["y"] == pytest.approx(2.0)
    assert first["value"]["width"] == pytest.approx(10.0)
    assert first["value"]["height"] == pytest.approx(20.0)
    assert first["value"]["rectanglelabels"] == ["person"]
    assert first["score"] == pytest.approx(0.9)

    assert out[1]["value"]["rectanglelabels"] == ["car"]


def test_emit_segmentation_polygon_normalization() -> None:
    from predictor import _emit_segmentation

    r0 = MagicMock()
    r0.boxes = MagicMock()
    r0.boxes.conf = _mk_tensor([0.85])
    r0.boxes.cls = _mk_tensor([5])
    r0.masks = MagicMock()
    # 三个顶点 (像素) → polygon, 归一化到 % .
    r0.masks.xy = [np.array([[0, 0], [200, 0], [100, 100]], dtype=np.float32)]
    names = {5: "bus"}

    out = _emit_segmentation(r0, names, img_w=200, img_h=200)
    assert len(out) == 1
    item = out[0]
    assert item["type"] == "polygonlabels"
    pts = item["value"]["points"]
    assert pts == [
        [pytest.approx(0.0), pytest.approx(0.0)],
        [pytest.approx(100.0), pytest.approx(0.0)],
        [pytest.approx(50.0), pytest.approx(50.0)],
    ]
    assert item["value"]["polygonlabels"] == ["bus"]


def test_emit_segmentation_drops_too_few_points() -> None:
    """<3 点的退化 polygon 不应进 result (会让 apps/api 简化器 NaN)."""
    from predictor import _emit_segmentation

    r0 = MagicMock()
    r0.boxes = MagicMock()
    r0.boxes.conf = _mk_tensor([0.5])
    r0.boxes.cls = _mk_tensor([0])
    r0.masks = MagicMock()
    r0.masks.xy = [np.array([[0, 0], [10, 10]], dtype=np.float32)]

    out = _emit_segmentation(r0, {0: "x"}, img_w=100, img_h=100)
    assert out == []


def test_emit_keypoint_visibility_thresholds() -> None:
    from predictor import _emit_keypoint

    r0 = MagicMock()
    r0.boxes = MagicMock()
    r0.boxes.conf = _mk_tensor([0.95])
    r0.boxes.cls = _mk_tensor([0])
    r0.keypoints = MagicMock()
    # 17 点: 不同 v 值测三档可见性 (v>0.5→2, v>0→1, 否则 0).
    kp = np.zeros((1, 17, 3), dtype=np.float32)
    kp[0, 0] = (10, 20, 0.9)  # 可见
    kp[0, 1] = (30, 40, 0.3)  # 遮挡
    kp[0, 2] = (50, 60, 0.0)  # 未标注
    r0.keypoints.data = _mk_tensor(kp)

    out = _emit_keypoint(r0, {0: "person"}, img_w=100, img_h=100)
    assert len(out) == 1
    item = out[0]
    assert item["type"] == "keypointlabels"
    pts = item["value"]["points"]
    assert len(pts) == 17
    assert pts[0]["v"] == 2
    assert pts[1]["v"] == 1
    assert pts[2]["v"] == 0
    # 归一化坐标.
    assert pts[0]["x"] == pytest.approx(10.0)
    assert pts[0]["y"] == pytest.approx(20.0)
    assert item["value"]["keypointlabels"] == ["person"]


def test_emit_obb_rotation_degrees_and_topleft() -> None:
    from predictor import _emit_obb

    r0 = MagicMock()
    r0.obb = MagicMock()
    r0.obb.__len__ = MagicMock(return_value=1)
    # cx=100, cy=100, w=40, h=20, rot=π/4 (45°)
    r0.obb.xywhr = _mk_tensor([[100, 100, 40, 20, math.pi / 4]])
    r0.obb.conf = _mk_tensor([0.8])
    r0.obb.cls = _mk_tensor([3])
    names = {3: "ship"}

    out = _emit_obb(r0, names, img_w=400, img_h=400)
    assert len(out) == 1
    item = out[0]
    assert item["type"] == "rectanglelabels"
    val = item["value"]
    # rotation: 45 度.
    assert val["rotation"] == pytest.approx(45.0)
    # x_topleft = cx - w/2 = 80, 归一化 = 80/400*100 = 20.0
    assert val["x"] == pytest.approx(20.0)
    assert val["y"] == pytest.approx(22.5)  # cy - h/2 = 90 → 90/400*100
    assert val["width"] == pytest.approx(10.0)
    assert val["height"] == pytest.approx(5.0)
    assert val["rectanglelabels"] == ["ship"]


def test_emit_obb_negative_rotation_normalized() -> None:
    """ultralytics 可能出负角, _emit_obb 内部应模 360 归一."""
    from predictor import _emit_obb

    r0 = MagicMock()
    r0.obb = MagicMock()
    r0.obb.__len__ = MagicMock(return_value=1)
    r0.obb.xywhr = _mk_tensor([[200, 200, 60, 30, -math.pi / 6]])  # -30 度
    r0.obb.conf = _mk_tensor([0.5])
    r0.obb.cls = _mk_tensor([0])

    out = _emit_obb(r0, {0: "plane"}, img_w=400, img_h=400)
    rot = out[0]["value"]["rotation"]
    # -30 度 % 360 = 330 度.
    assert rot == pytest.approx(330.0)
