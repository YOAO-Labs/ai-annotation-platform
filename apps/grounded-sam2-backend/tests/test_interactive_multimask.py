"""predict_point / predict_bbox 的 multimask 候选 + 排序单测。

不加载 GPU, mock SAM predict, 验证:
- multimask_output 透传到 _sam_predictor.predict;
- 多候选按 score 降序 (results[0]=top-1, 与 sam3 _build_interactive_results 对齐);
- 单 mask (multimask_output=False) 路径不改顺序。
"""

from __future__ import annotations

import hashlib
import json
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


def _circle(size: int = 256, r: int = 60) -> np.ndarray:
    m = np.zeros((size, size), dtype=np.uint8)
    cv2.circle(m, (size // 2, size // 2), r, 1, thickness=-1)
    return m


def _img() -> Image.Image:
    return Image.fromarray(np.zeros((256, 256, 3), dtype=np.uint8))


def _mask_prompt(width: int, height: int) -> dict:
    from mask_utils import encode_coco_rle

    pixels = [int(x < width // 2) for _y in range(height) for x in range(width)]
    rle = encode_coco_rle(pixels, width, height)
    return {
        "rle": rle,
        "source_annotation_id": "source-1",
        "source_version": 1,
        "source_digest": hashlib.sha256(
            json.dumps(rle, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def test_point_multimask_passthrough_and_sorted(predictor):
    """3 候选 score 乱序 → 透传 multimask=True, 结果按 score 降序。"""
    masks = np.stack([_circle()] * 3)  # (3, H, W)
    scores = np.array([0.5, 0.91, 0.7], dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(return_value=(masks, scores, None))

    results, _, mask_next = predictor.predict_point(
        _img(),
        [[0.5, 0.5]],
        [1],
        multimask_output=True,
        cache_key=None,
        simplify_tolerance=1.0,
    )

    kw = predictor._sam_predictor.predict.call_args.kwargs
    assert kw["multimask_output"] is True
    assert len(results) == 3
    out_scores = [r["score"] for r in results]
    assert out_scores == sorted(out_scores, reverse=True)
    assert out_scores[0] == pytest.approx(0.91)
    # 多候选阶段 index 歧义 → 不回灌 low-res.
    assert mask_next is None


def test_point_single_mask_default(predictor):
    """缺省 multimask=False → 单 mask, 透传 False; 单 mask 阶段回灌 low-res logits。"""
    low_res = np.zeros((1, 256, 256), dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(_circle()[None, ...], np.array([0.8]), low_res)
    )
    results, _, mask_next = predictor.predict_point(
        _img(), [[0.5, 0.5]], [1], cache_key=None, simplify_tolerance=1.0
    )
    assert (
        predictor._sam_predictor.predict.call_args.kwargs["multimask_output"] is False
    )
    assert len(results) == 1
    # multimask=False 单 mask → 编码 low-res 回 mask_input_next.
    assert isinstance(mask_next, str) and mask_next


def test_point_mask_input_decoded_and_passed(predictor):
    """context.mask_input 解码成 (1,256,256) 透传 _sam_predictor.predict。"""
    from aap_protocol_v2 import encode_low_res_mask

    low_res = np.zeros((1, 256, 256), dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(_circle()[None, ...], np.array([0.8]), low_res)
    )
    encoded = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    predictor.predict_point(
        _img(),
        [[0.5, 0.5], [0.6, 0.6]],
        [1, 1],
        mask_input=encoded,
        cache_key=None,
        simplify_tolerance=1.0,
    )
    kw = predictor._sam_predictor.predict.call_args.kwargs
    assert kw["mask_input"].shape == (1, 256, 256)


def test_scribble_consumer_preserves_positive_negative_and_mask_seed(predictor):
    low_res = np.zeros((1, 256, 256), dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(_circle()[None, ...], np.array([0.8]), low_res)
    )
    predictor.predict_point(
        _img(),
        [],
        [],
        scribbles=[
            {"polarity": 1, "points": [[0.1, 0.1], [0.4, 0.4]], "width": 0.01},
            {"polarity": 0, "points": [[0.8, 0.8], [0.6, 0.6]], "width": 0.01},
        ],
        mask_prompt=_mask_prompt(256, 256),
        cache_key=None,
    )

    kwargs = predictor._sam_predictor.predict.call_args.kwargs
    assert set(kwargs["point_labels"].tolist()) == {0, 1}
    assert kwargs["mask_input"].shape == (1, 256, 256)
    assert set(np.unique(kwargs["mask_input"])) == {-16.0, 16.0}


def test_mask_prompt_only_is_consumed_as_dense_prompt(predictor):
    low_res = np.zeros((1, 256, 256), dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(_circle()[None, ...], np.array([0.8]), low_res)
    )

    results, _, mask_next = predictor.predict_mask(
        _img(),
        _mask_prompt(256, 256),
        cache_key=None,
    )

    kwargs = predictor._sam_predictor.predict.call_args.kwargs
    assert kwargs["point_coords"] is None
    assert kwargs["point_labels"] is None
    assert kwargs["mask_input"].shape == (1, 256, 256)
    assert results
    assert mask_next is not None


def test_interactive_box_multimask_sorted(predictor):
    """predict_bbox (interactive_box 路由) 多候选同样按 score 降序。"""
    masks = np.stack([_circle()] * 2)
    scores = np.array([0.6, 0.95], dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(return_value=(masks, scores, None))

    results, _, mask_next = predictor.predict_bbox(
        _img(),
        [0.1, 0.1, 0.4, 0.4],
        multimask_output=True,
        cache_key=None,
        simplify_tolerance=1.0,
    )
    assert predictor._sam_predictor.predict.call_args.kwargs["multimask_output"] is True
    out_scores = [r["score"] for r in results]
    assert out_scores == sorted(out_scores, reverse=True)
    assert out_scores[0] == pytest.approx(0.95)
    # 框单发不回灌.
    assert mask_next is None


def test_interactive_box_native_mask_output(predictor):
    mask = np.zeros((2, 3), dtype=np.uint8)
    mask[0, 1] = 1
    predictor._sam_predictor.predict = MagicMock(
        return_value=(mask[None, ...], np.array([0.92]), None)
    )
    image = Image.fromarray(np.zeros((2, 3, 3), dtype=np.uint8))

    results, _, _ = predictor.predict_bbox(
        image,
        [0.1, 0.1, 0.8, 0.8],
        output_geometry="mask",
        prompt_revision="box-revision",
    )

    assert len(results) == 1
    assert results[0]["type"] == "mask"
    assert results[0]["value"]["rle"]["size"] == [2, 3]
    assert len(results[0]["value"]["preview"]["points"]) == 4


def test_point_native_mask_preserves_non_square_pixels(predictor):
    from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id
    from mask_utils import decode_coco_rle

    mask = np.array([[1, 0, 1], [0, 1, 0]], dtype=np.uint8)
    predictor._sam_predictor.predict = MagicMock(
        return_value=(mask[None, ...], np.array([0.87]), None)
    )
    image = Image.fromarray(np.zeros((2, 3, 3), dtype=np.uint8))
    results, _, mask_next = predictor.predict_point(
        image,
        [[0.5, 0.5]],
        [1],
        output_geometry="mask",
        prompt_revision="revision-1",
    )

    assert mask_next is None
    assert len(results) == 1
    candidate = results[0]
    assert candidate["type"] == "mask"
    assert candidate["value"]["rle"]["size"] == [2, 3]
    assert len(candidate["value"]["preview"]["points"]) >= 3
    assert list(decode_coco_rle(candidate["value"]["rle"])) == mask.reshape(-1).tolist()
    rle = CocoRlePayload.model_validate(candidate["value"]["rle"])
    assert candidate["candidate_id"] == native_mask_candidate_id(
        rle,
        prompt_revision="revision-1",
        candidate_index=0,
    )


def test_point_native_multimask_filters_empty_and_reindexes(predictor):
    masks = np.zeros((3, 2, 3), dtype=np.uint8)
    masks[0, 0, 0] = 1
    masks[2, 1, 2] = 1
    scores = np.array([0.6, 0.9, 0.8], dtype=np.float32)
    predictor._sam_predictor.predict = MagicMock(return_value=(masks, scores, None))
    image = Image.fromarray(np.zeros((2, 3, 3), dtype=np.uint8))

    results, _, _ = predictor.predict_point(
        image,
        [[0.5, 0.5]],
        [1],
        multimask_output=True,
        output_geometry="mask",
        prompt_revision="revision-2",
    )

    assert [entry["score"] for entry in results] == pytest.approx([0.8, 0.6])
    assert len({entry["candidate_id"] for entry in results}) == 2
