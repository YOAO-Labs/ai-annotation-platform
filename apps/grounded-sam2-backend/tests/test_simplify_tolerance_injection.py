"""Context.simplify_tolerance 注入路径 + 顶点 WARN 单测.

不加载 GPU 模型, 用 mock 直接喂 mask 给 _masks_to_results / predict_text mask 路径,
观察 mask_to_polygon 调用时的 tolerance 透传 + 顶点数 > 200 触发 logger.warning.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest


def _ensure_dino_module_stub():
    """注入伪 groundingdino 让 predict_text 能 import 不爆 (无 GPU 测试机)."""
    fake_dino_mod = types.ModuleType("groundingdino.util.inference")
    fake_dino_mod.predict = MagicMock()
    sys.modules.setdefault("groundingdino", types.ModuleType("groundingdino"))
    sys.modules.setdefault("groundingdino.util", types.ModuleType("groundingdino.util"))
    sys.modules["groundingdino.util.inference"] = fake_dino_mod
    return fake_dino_mod


@pytest.fixture
def predictor_with_mocks():
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


def _circle_mask(size: int = 512, radius: int = 200) -> np.ndarray:
    """圆形 mask — simplify 不会塌成 4 顶点, 适合验 tolerance 差异 / WARN 触发."""
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (size // 2, size // 2), radius, 1, thickness=-1)
    return m


def _square_mask(size: int = 128, half: int = 32) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    c = size // 2
    m[c - half : c + half, c - half : c + half] = 1
    return m


def test_higher_tolerance_returns_fewer_vertices(predictor_with_mocks):
    """tolerance 调高后, 同一圆 mask 顶点数应明显减少 (verify mask_to_polygon 透传)."""
    inst = predictor_with_mocks
    mask = _circle_mask(512, 200)

    low = inst._masks_to_results(
        mask[None, ...], np.array([0.9]), 512, 512, simplify_tolerance=0.5
    )
    high = inst._masks_to_results(
        mask[None, ...], np.array([0.9]), 512, 512, simplify_tolerance=2.0
    )

    assert low and high
    n_low = len(low[0]["value"]["points"])
    n_high = len(high[0]["value"]["points"])
    assert n_high < n_low, (
        f"expected fewer verts at high tolerance: {n_high} >= {n_low}"
    )


def test_default_tolerance_used_when_none(predictor_with_mocks):
    """simplify_tolerance=None 走 DEFAULT_SIMPLIFY_TOLERANCE, 不抛错."""
    inst = predictor_with_mocks
    mask = _square_mask(128, 32)

    out = inst._masks_to_results(
        mask[None, ...], np.array([0.9]), 128, 128, simplify_tolerance=None
    )
    assert out and len(out[0]["value"]["points"]) >= 4


def test_vertex_count_warn_logged_for_high_count(predictor_with_mocks, caplog):
    """顶点 > 200 时记 logger.warning (非阻塞, 仅运维信号).

    圆 mask + tolerance=0.0 → ~560 顶点, 触发 WARN.
    """
    inst = predictor_with_mocks
    mask = _circle_mask(512, 200)
    with caplog.at_level(logging.WARNING):
        inst._masks_to_results(
            mask[None, ...], np.array([0.9]), 512, 512, simplify_tolerance=0.0
        )

    warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("polygon vertex count" in r.message for r in warn_msgs), (
        f"expected vertex-count WARN, got: {[r.message for r in warn_msgs]}"
    )


def test_vertex_count_warn_not_logged_for_normal_count(predictor_with_mocks, caplog):
    """顶点数 < 200 时不应触发 WARN (避免噪声). 圆 mask + tolerance=2.0 → ~32 顶点."""
    inst = predictor_with_mocks
    mask = _circle_mask(512, 200)
    with caplog.at_level(logging.WARNING):
        inst._masks_to_results(
            mask[None, ...], np.array([0.9]), 512, 512, simplify_tolerance=2.0
        )

    warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("polygon vertex count" in r.message for r in warn_msgs), (
        f"unexpected WARN for small polygon: {[r.message for r in warn_msgs]}"
    )
