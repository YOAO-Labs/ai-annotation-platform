"""predict_text() 三分支行为单测 (无 GPU, mock dino + sam).

box 模式必须跳过 SAM image embedding + cache.put + sam.predict (节省 GPU 时间是这个分支
的核心动机). mask 模式保持当前行为. both 模式同 instance 配对返回 rect + poly.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def fake_image():
    return Image.fromarray(np.full((480, 640, 3), 128, dtype=np.uint8))


@pytest.fixture
def predictor_with_mocks(monkeypatch):
    """构造一个不加载真实 GPU 模型的 GroundedSAM2Predictor 实例.

    思路: 不调用 __init__ 的真实加载, 直接绕过去手工挂上 mock 子组件.
    """
    # 注入一个伪 groundingdino.util.inference 模块, 让 predict_text() 内的
    # `from groundingdino.util.inference import predict as dino_predict` 取到 mock.
    fake_dino_mod = types.ModuleType("groundingdino.util.inference")
    fake_dino_mod.predict = MagicMock()
    sys.modules.setdefault("groundingdino", types.ModuleType("groundingdino"))
    sys.modules.setdefault("groundingdino.util", types.ModuleType("groundingdino.util"))
    sys.modules["groundingdino.util.inference"] = fake_dino_mod

    from predictor import (
        GroundedSAM2Predictor,
    )  # 现在 import 安全 (不会触发 vendor 加载)

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
    # _dino_image_tensor / _to_numpy / _cxcywh_to_xyxy / _mask_to_polygon /
    # _restore_sam / _snapshot_sam 都是 instance method, 用 patch.object 比 monkeypatch 简洁.
    monkeypatch.setattr(
        inst, "_dino_image_tensor", MagicMock(return_value=MagicMock()), raising=False
    )
    return inst, fake_dino_mod


def _two_boxes_dino_return():
    """模拟 DINO 返回 2 个 box (cxcywh 归一化) + 2 phrases."""
    import torch

    boxes_norm = torch.tensor([[0.5, 0.5, 0.4, 0.4], [0.2, 0.3, 0.1, 0.15]])
    logits = torch.tensor([0.9, 0.85])
    phrases = ["person", "person"]
    return boxes_norm, logits, phrases


def test_box_mode_skips_sam_calls_and_cache_writes(predictor_with_mocks, fake_image):
    """box 模式: 不应调用 set_image / predict / cache.put."""
    inst, fake_dino = predictor_with_mocks
    fake_dino.predict.return_value = _two_boxes_dino_return()

    results, cache_hit = inst.predict_text(
        fake_image, "person", output="box", cache_key="k1"
    )

    # 关键断言: SAM 一行没动, cache 也没写.
    inst._sam_predictor.set_image.assert_not_called()
    inst._sam_predictor.predict.assert_not_called()
    inst.embedding_cache.put.assert_not_called()
    assert cache_hit is False, "box 路径恒为 False (不读不写 cache)"
    assert len(results) == 2
    for r in results:
        assert r["type"] == "rectanglelabels"
        v = r["value"]
        assert {"x", "y", "width", "height", "rectanglelabels"}.issubset(v.keys())
        assert all(0.0 <= v[k] <= 1.0 for k in ("x", "y", "width", "height"))
        assert v["rectanglelabels"] == ["person"]


def test_mask_mode_returns_polygons_default(predictor_with_mocks, fake_image):
    """未传 output (默认 mask) 时返回 polygonlabels, 调用 SAM."""
    inst, fake_dino = predictor_with_mocks
    fake_dino.predict.return_value = _two_boxes_dino_return()
    # SAM 返回 2 个 mask, shape (2, H, W), 每个非空.
    mask = np.zeros((480, 640), dtype=np.uint8)
    mask[100:300, 100:300] = 1
    inst._sam_predictor.predict.return_value = (
        np.stack([mask, mask]),
        np.array([0.95, 0.92]),
        None,
    )

    results, _ = inst.predict_text(fake_image, "person", cache_key="k2")

    inst._sam_predictor.set_image.assert_called_once()
    assert all(r["type"] == "polygonlabels" for r in results)
    assert len(results) == 2


def test_both_mode_returns_paired_rect_and_polygon(predictor_with_mocks, fake_image):
    """both: 返回数组长度 = 2 × DINO box 数, 顺序 [rect, poly, rect, poly]."""
    inst, fake_dino = predictor_with_mocks
    fake_dino.predict.return_value = _two_boxes_dino_return()
    mask = np.zeros((480, 640), dtype=np.uint8)
    mask[100:300, 100:300] = 1
    inst._sam_predictor.predict.return_value = (
        np.stack([mask, mask]),
        np.array([0.95, 0.92]),
        None,
    )

    results, _ = inst.predict_text(fake_image, "person", output="both", cache_key="k3")

    assert len(results) == 4, "2 boxes × 2 (rect+poly) = 4"
    # 严格交错顺序: rect, poly, rect, poly
    assert results[0]["type"] == "rectanglelabels"
    assert results[1]["type"] == "polygonlabels"
    assert results[2]["type"] == "rectanglelabels"
    assert results[3]["type"] == "polygonlabels"


def test_box_mode_returns_empty_when_dino_finds_nothing(
    predictor_with_mocks, fake_image
):
    """DINO 0 box → 直接返回空 list, 不抛错."""
    inst, fake_dino = predictor_with_mocks
    import torch

    fake_dino.predict.return_value = (torch.empty((0, 4)), torch.empty(0), [])

    results, cache_hit = inst.predict_text(
        fake_image, "unicorn", output="box", cache_key="k4"
    )
    assert results == []
    assert cache_hit is False
    inst._sam_predictor.set_image.assert_not_called()
