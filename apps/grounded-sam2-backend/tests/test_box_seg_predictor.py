"""框→mask 批量分割原子 predict_boxes 单测。

不加载 GPU, mock SAM predict, 验证:
- 一张图只 set_image 一次 (N 框共享 image embedding);
- 每框结果带正确 parent_box_idx, 供平台 merge 回父框。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from PIL import Image


def _ensure_dino_module_stub():
    fake_dino_mod = types.ModuleType("groundingdino.util.inference")
    fake_dino_mod.predict = MagicMock()
    sys.modules.setdefault("groundingdino", types.ModuleType("groundingdino"))
    sys.modules.setdefault("groundingdino.util", types.ModuleType("groundingdino.util"))
    sys.modules["groundingdino.util.inference"] = fake_dino_mod


@pytest.fixture
def predictor():
    _ensure_dino_module_stub()
    from predictor import GroundedSAM2Predictor

    inst = GroundedSAM2Predictor.__new__(GroundedSAM2Predictor)
    inst.device = "cpu"
    inst.sam_variant = "tiny"
    inst.dino_variant = "T"
    inst._sam_predictor = MagicMock()
    inst.embedding_cache = MagicMock()
    inst.embedding_cache.get = MagicMock(return_value=None)
    inst.embedding_cache.put = MagicMock()
    return inst


def _solid_circle(size: int = 256, r: int = 60) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (size // 2, size // 2), r, 1, thickness=-1)
    return m


def test_predict_boxes_sets_image_once_and_tags_parent_idx(predictor):
    """两框 → set_image 一次、predict 两次, 结果按 parent_box_idx 标回。"""
    mask = _solid_circle()
    predictor._sam_predictor.predict = MagicMock(
        return_value=(mask[None, ...], np.array([0.9]), None)
    )
    img = Image.fromarray(np.zeros((256, 256, 3), dtype=np.uint8))

    boxes = [([0.0, 0.0, 0.5, 0.5], 0), ([0.5, 0.5, 1.0, 1.0], 7)]
    # cache_key=None 绕过 embedding cache, 走 set_image 路径。
    results, hit = predictor.predict_boxes(
        img, boxes, cache_key=None, simplify_tolerance=1.0
    )

    # 一图一次 encode, 两框各跑一次轻量 decoder。
    assert predictor._sam_predictor.set_image.call_count == 1
    assert predictor._sam_predictor.predict.call_count == 2
    assert hit is False
    # 每框至少出一条 polygon, 且 parent_box_idx 与输入对齐。
    parent_idxs = {r["parent_box_idx"] for r in results}
    assert parent_idxs == {0, 7}
    for r in results:
        assert r["type"] == "polygonlabels"
        assert "parent_box_idx" in r


def test_predict_boxes_empty_mask_skipped(predictor):
    """空 mask 不产 polygon, 对应父框无输出(不报错)。"""
    empty = np.zeros((256, 256), dtype=np.uint8)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(empty[None, ...], np.array([0.1]), None)
    )
    img = Image.fromarray(np.zeros((256, 256, 3), dtype=np.uint8))

    results, _ = predictor.predict_boxes(
        img, [([0.0, 0.0, 0.5, 0.5], 3)], cache_key=None, simplify_tolerance=1.0
    )
    assert results == []
