"""predictor mask 多连通域 / 空洞输出 shape 单测。

不加载 GPU, mock SAM mask → 验 _rings_to_polygon_label 智能选择三种字面:
- 单连通无 hole → {points, polygonlabels}
- 单连通带 hole → {points, holes, polygonlabels}
- 多连通       → {polygons:[{points, holes?}], polygonlabels}
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest


def _ensure_dino_module_stub():
    fake_dino_mod = types.ModuleType("groundingdino.util.inference")
    fake_dino_mod.predict = MagicMock()
    sys.modules.setdefault("groundingdino", types.ModuleType("groundingdino"))
    sys.modules.setdefault("groundingdino.util", types.ModuleType("groundingdino.util"))
    sys.modules["groundingdino.util.inference"] = fake_dino_mod
    return fake_dino_mod


@pytest.fixture
def predictor():
    _ensure_dino_module_stub()
    from predictor import GroundedSAM2Predictor

    inst = GroundedSAM2Predictor.__new__(GroundedSAM2Predictor)
    inst.device = "cpu"
    inst.box_threshold = 0.35
    inst.text_threshold = 0.25
    inst.sam_variant = "tiny"
    inst.dino_variant = "T"
    inst._dino_model = MagicMock()
    inst._sam_predictor = MagicMock()
    inst.embedding_cache = MagicMock()
    inst.embedding_cache.get = MagicMock(return_value=None)
    inst.embedding_cache.put = MagicMock()
    return inst


def _solid_circle(size: int = 256, r: int = 60) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (size // 2, size // 2), r, 1, thickness=-1)
    return m


def _donut(size: int = 256, outer: int = 80, inner: int = 30) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (size // 2, size // 2), outer, 1, thickness=-1)
    cv2.circle(m, (size // 2, size // 2), inner, 0, thickness=-1)
    return m


def _two_circles(size: int = 256) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (60, 128), 30, 1, thickness=-1)
    cv2.circle(m, (200, 128), 30, 1, thickness=-1)
    return m


def test_single_connected_no_hole_emits_legacy_shape(predictor):
    """单连通无 hole → 保持既有字面 (无 holes / polygons 字段)."""
    mask = _solid_circle(size=256, r=60)
    out = predictor._masks_to_results(
        mask[None, ...], np.array([0.9]), 256, 256, simplify_tolerance=1.0
    )
    assert len(out) == 1
    val = out[0]["value"]
    assert "points" in val
    assert "holes" not in val
    assert "polygons" not in val
    assert val["polygonlabels"] == ["object"]


def test_donut_emits_polygon_with_holes(predictor):
    """单连通带 hole → {points, holes, polygonlabels}, 无 polygons 字段."""
    mask = _donut(size=256, outer=80, inner=30)
    out = predictor._masks_to_results(
        mask[None, ...], np.array([0.9]), 256, 256, simplify_tolerance=1.0
    )
    assert len(out) == 1
    val = out[0]["value"]
    assert "points" in val
    assert "holes" in val
    assert len(val["holes"]) == 1
    assert "polygons" not in val


def test_two_disconnected_emits_multi_polygon(predictor):
    """多连通 → {polygons:[...], polygonlabels}, 无顶层 points / holes."""
    mask = _two_circles(size=256)
    out = predictor._masks_to_results(
        mask[None, ...], np.array([0.9]), 256, 256, simplify_tolerance=1.0
    )
    assert len(out) == 1
    val = out[0]["value"]
    assert "polygons" in val
    assert "points" not in val
    assert "holes" not in val
    assert len(val["polygons"]) == 2
    for p in val["polygons"]:
        assert "points" in p
        # 这两个圆都没 hole, polygons[i] 不写 holes 字段
        assert "holes" not in p


def test_score_propagated_to_top_level(predictor):
    mask = _solid_circle(size=128, r=30)
    out = predictor._masks_to_results(mask[None, ...], np.array([0.77]), 128, 128)
    assert out[0]["score"] == pytest.approx(0.77)


def test_score_omitted_when_none(predictor):
    """SAM scores=None → 输出不带 score 字段 (point/bbox 路径单独 prompt 时常出现)."""
    mask = _solid_circle(size=128, r=30)
    out = predictor._masks_to_results(mask[None, ...], None, 128, 128)
    assert "score" not in out[0]


def test_text_path_mask_mode_with_holes(predictor):
    """predict_text mask 模式甜甜圈 → polygonlabels with holes."""
    import torch

    fake_dino = sys.modules["groundingdino.util.inference"]
    boxes_norm = torch.tensor([[0.5, 0.5, 0.6, 0.6]])
    logits = torch.tensor([0.88])
    phrases = ["donut"]
    fake_dino.predict.return_value = (boxes_norm, logits, phrases)

    mask = _donut(size=256, outer=80, inner=30)
    predictor._sam_predictor.predict.return_value = (
        mask[None, ...],
        np.array([0.95]),
        None,
    )
    predictor._dino_image_tensor = MagicMock(return_value=MagicMock())

    from PIL import Image

    fake_image = Image.fromarray(np.full((256, 256, 3), 128, dtype=np.uint8))
    results, _ = predictor.predict_text(fake_image, "donut", cache_key="t1")

    assert len(results) == 1
    assert results[0]["type"] == "polygonlabels"
    val = results[0]["value"]
    assert "points" in val
    assert "holes" in val
    assert val["polygonlabels"] == ["donut"]
    assert results[0]["score"] == pytest.approx(0.88)
