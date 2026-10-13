"""SAM3Predictor 行为单测 (vendor-aligned).

绕开 __init__ 真实加载 (无 GPU + 不要触发 build_sam3_image_model 真实拉权重),
手工挂 mock Sam3Processor; 验证:
  - bbox 归一化 xyxy → 归一化 cxcywh 转换正确
  - text / bbox / exemplar 三种 prompt 都返回 polygonlabels
  - text "box" 输出模式跳过 mask → polygon, 给 rectanglelabels
  - text "both" 模式同 instance 配对 (rect+poly 交错)
  - score_threshold 单次覆盖会写到 processor.confidence_threshold
  - cache miss 时调 set_image, cache hit 时跳过
  - 0 mask 时返回空 list, 不抛错
  - reset_all_prompts 在 prompt 前后都会被调 (防 backbone_out 污染)
"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pytest
import torch
from PIL import Image


@pytest.fixture
def fake_image():
    return Image.fromarray(np.full((480, 640, 3), 128, dtype=np.uint8))


@pytest.fixture
def predictor_with_mocks(monkeypatch):
    """构造一个不加载真实模型的 SAM3Predictor 实例."""
    # 注入伪 sam3 模块, 让 predictor.py 顶部 import 不挂.
    fake_sam3 = types.ModuleType("sam3")
    fake_sam3.build_sam3_image_model = MagicMock(return_value=MagicMock())
    sys.modules["sam3"] = fake_sam3
    fake_model_mod = types.ModuleType("sam3.model")
    sys.modules.setdefault("sam3.model", fake_model_mod)
    fake_processor_mod = types.ModuleType("sam3.model.sam3_image_processor")
    fake_processor_mod.Sam3Processor = MagicMock(return_value=MagicMock())
    sys.modules["sam3.model.sam3_image_processor"] = fake_processor_mod

    from predictor import SAM3Predictor  # noqa: PLC0415

    inst = SAM3Predictor.__new__(SAM3Predictor)
    inst.device = "cpu"
    inst.checkpoint_dir = "/tmp"
    inst.score_threshold = 0.5
    inst._model = MagicMock()
    inst._mask_input_size = (288, 288)
    inst._processor = MagicMock()
    inst._processor.confidence_threshold = 0.5
    inst.embedding_cache = MagicMock()
    inst.embedding_cache.get = MagicMock(return_value=None)
    inst.embedding_cache.put = MagicMock()
    return inst


# ---------- helpers ----------


def _fake_state_after_set_image(w: int = 640, h: int = 480) -> dict:
    """模拟 Sam3Processor.set_image 写入 state 的 keys."""
    return {
        "backbone_out": {"vision_features": MagicMock(name="vision_features")},
        "original_width": w,
        "original_height": h,
    }


def _populate_state_with_outputs(state: dict, num: int) -> None:
    """模拟 _forward_grounding 写入 state 的 boxes/masks/scores."""
    state["boxes"] = torch.tensor(
        [[100.0, 100.0, 300.0, 300.0]] * num, dtype=torch.float32
    )
    mask = torch.zeros((480, 640), dtype=torch.bool)
    mask[100:300, 100:300] = True
    masks = torch.stack([mask] * num).unsqueeze(1)  # (N, 1, H, W)
    state["masks"] = masks
    state["scores"] = torch.tensor([0.95, 0.88, 0.72][:num], dtype=torch.float32)


# ---------- predict_exemplar ----------


def test_exemplar_returns_polygonlabels(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 2)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    results, hit = inst.predict_exemplar(
        fake_image, exemplar_bbox=[0.2, 0.2, 0.45, 0.55], cache_key="k1"
    )

    assert hit is False
    assert len(results) == 2
    for r in results:
        assert r["type"] == "polygonlabels"
        assert r["value"]["polygonlabels"] == ["object"]
        for pt in r["value"]["points"]:
            assert 0.0 <= pt[0] <= 1.0 and 0.0 <= pt[1] <= 1.0


def test_exemplar_empty_when_no_match(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    # add_geometric_prompt 不写 masks → 空结果
    inst._processor.add_geometric_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_exemplar(
        fake_image, exemplar_bbox=[0.0, 0.0, 0.1, 0.1], cache_key="k_empty"
    )
    assert results == []


def test_exemplar_bbox_converted_to_cxcywh(predictor_with_mocks, fake_image):
    """归一化 xyxy [0.2, 0.2, 0.45, 0.55] → cxcywh [0.325, 0.375, 0.25, 0.35]."""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.add_geometric_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    inst.predict_exemplar(
        fake_image, exemplar_bbox=[0.2, 0.2, 0.45, 0.55], cache_key="k3"
    )

    call = inst._processor.add_geometric_prompt.call_args
    # 调用形式: add_geometric_prompt(box, True, state)
    box_arg = call.args[0]
    assert box_arg == pytest.approx([0.325, 0.375, 0.25, 0.35])
    assert call.args[1] is True


def test_exemplar_score_threshold_override(predictor_with_mocks, fake_image):
    """per-request score_threshold 写到 processor.confidence_threshold."""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.add_geometric_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    inst.predict_exemplar(
        fake_image,
        exemplar_bbox=[0.1, 0.1, 0.2, 0.2],
        cache_key="k4",
        score_threshold=0.85,
    )

    assert inst._processor.confidence_threshold == 0.85


# ---------- predict_exemplars (多正负框 + text 组合) ----------


def test_exemplars_multi_box_accumulated(predictor_with_mocks, fake_image):
    """多框顺序累加: add_geometric_prompt 每框各调一次, 正/负 label 透传。"""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 1)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.set_text_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_exemplars(
        fake_image,
        [
            {"bbox": [0.1, 0.1, 0.2, 0.2], "label": True},
            {"bbox": [0.5, 0.5, 0.6, 0.6], "label": False},
        ],
        cache_key="kex1",
    )

    assert len(results) == 1
    assert inst._processor.add_geometric_prompt.call_count == 2
    calls = inst._processor.add_geometric_prompt.call_args_list
    # 第 1 框正 (True), 第 2 框负 (False); box 是归一化 cxcywh。
    assert calls[0].args[1] is True
    assert calls[1].args[1] is False
    assert calls[0].args[0] == pytest.approx([0.15, 0.15, 0.1, 0.1])
    # text 未传 → 不调 set_text_prompt。
    inst._processor.set_text_prompt.assert_not_called()


def test_exemplars_with_text_combination(predictor_with_mocks, fake_image):
    """text 概念 + 几何框组合: 先 set_text_prompt 再叠框; label 用 text。"""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.set_text_prompt = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 2)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_exemplars(
        fake_image,
        [{"bbox": [0.1, 0.1, 0.3, 0.3], "label": False}],
        text="car",
        cache_key="kex2",
    )

    inst._processor.set_text_prompt.assert_called_once_with("car", state)
    assert len(results) == 2
    # text 组合时 label 用 text 短语而非 "object"。
    for r in results:
        assert r["value"]["polygonlabels"] == ["car"]


def test_exemplars_score_threshold_override(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.add_geometric_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    inst.predict_exemplars(
        fake_image,
        [{"bbox": [0.1, 0.1, 0.2, 0.2], "label": True}],
        cache_key="kex3",
        score_threshold=0.9,
    )
    assert inst._processor.confidence_threshold == 0.9


def test_predict_exemplar_delegates_to_exemplars(predictor_with_mocks, fake_image):
    """单框薄封装: predict_exemplar → predict_exemplars 单元素正框。"""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 1)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_exemplar(
        fake_image, exemplar_bbox=[0.2, 0.2, 0.45, 0.55], cache_key="kex4"
    )
    assert len(results) == 1
    inst._processor.add_geometric_prompt.assert_called_once()
    assert inst._processor.add_geometric_prompt.call_args.args[1] is True


# ---------- predict_bbox (与 exemplar 同底层) ----------


def test_bbox_routes_to_same_geometric_call(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 1)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_bbox(
        fake_image, bbox=[0.2, 0.2, 0.45, 0.55], cache_key="kb1"
    )

    assert len(results) == 1
    assert results[0]["type"] == "polygonlabels"
    inst._processor.add_geometric_prompt.assert_called_once()


# ---------- predict_text ----------


def test_text_mask_mode_returns_polygons(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def set_text(prompt, st):
        _populate_state_with_outputs(st, 2)
        return st

    inst._processor.set_text_prompt = MagicMock(side_effect=set_text)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_text(fake_image, "person", cache_key="kt1")

    assert all(r["type"] == "polygonlabels" for r in results)
    assert len(results) == 2
    for r in results:
        assert r["value"]["polygonlabels"] == ["person"]


def test_text_box_mode_skips_simplify(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def set_text(prompt, st):
        _populate_state_with_outputs(st, 2)
        return st

    inst._processor.set_text_prompt = MagicMock(side_effect=set_text)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_text(fake_image, "person", output="box", cache_key="kt2")

    assert len(results) == 2
    for r in results:
        assert r["type"] == "rectanglelabels"
        v = r["value"]
        assert {"x", "y", "width", "height", "rectanglelabels"}.issubset(v.keys())
        assert all(0.0 <= v[k] <= 1.0 for k in ("x", "y", "width", "height"))


def test_text_both_mode_pairs_rect_and_polygon(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def set_text(prompt, st):
        _populate_state_with_outputs(st, 2)
        return st

    inst._processor.set_text_prompt = MagicMock(side_effect=set_text)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_text(fake_image, "person", output="both", cache_key="kt3")

    assert len(results) == 4
    assert results[0]["type"] == "rectanglelabels"
    assert results[1]["type"] == "polygonlabels"
    assert results[2]["type"] == "rectanglelabels"
    assert results[3]["type"] == "polygonlabels"


def test_text_returns_empty_when_pcs_finds_nothing(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.set_text_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_text(fake_image, "unicorn", output="box", cache_key="kt4")

    assert results == []


# ---------- cache 行为 ----------


def test_cache_miss_calls_set_image(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)
    inst._processor.add_geometric_prompt = MagicMock(return_value=state)
    inst._processor.reset_all_prompts = MagicMock()

    inst.predict_bbox(fake_image, bbox=[0.1, 0.1, 0.2, 0.2], cache_key="cm1")

    inst._processor.set_image.assert_called_once()
    inst.embedding_cache.put.assert_called_once()


def test_cache_hit_skips_set_image(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    # 设置 cache 返回一个有效 entry
    from embedding_cache import CacheEntry  # noqa: PLC0415

    inst.embedding_cache.get = MagicMock(
        return_value=CacheEntry(
            features={"vision_features": MagicMock()},
            orig_hw=(480, 640),
            is_batch=False,
            wh=(640, 480),
        )
    )
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 1)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    # image=None: 命中时 _prime_state 不应调 set_image, 不需要 image
    results, hit = inst.predict_bbox(None, bbox=[0.1, 0.1, 0.2, 0.2], cache_key="ch1")

    assert hit is True
    inst._processor.set_image.assert_not_called()
    inst.embedding_cache.put.assert_not_called()
    assert len(results) == 1


# ---------- reset_all_prompts 调用 ----------


def test_reset_called_before_and_after_prompt(predictor_with_mocks, fake_image):
    """防 backbone_out 被前一次 text prompt 污染 (state["language_features"] 等)."""
    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, st):
        _populate_state_with_outputs(st, 1)
        return st

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    inst.predict_bbox(fake_image, bbox=[0.1, 0.1, 0.2, 0.2], cache_key="kr1")

    # reset 应被调 2 次: prompt 前 (清除 stale) + prompt 后 (cleanup)
    assert inst._processor.reset_all_prompts.call_count == 2


# ---------- predict_interactive (SAM-style point / interactive_box) ----------


def _fake_inst_output(num: int):
    """模拟 model.predict_inst 返回 (masks CxHxW float 0/1, iou C, low_res Cx256x256)."""
    mask = np.zeros((480, 640), dtype=np.float32)
    mask[100:300, 100:300] = 1.0
    masks = np.stack([mask] * num)  # (C, H, W)
    ious = np.array([0.7, 0.95, 0.6][:num], dtype=np.float32)
    low_res = np.zeros((num, 256, 256), dtype=np.float32)
    return masks, ious, low_res


def _interactive_mask_prompt(width: int = 640, height: int = 480) -> dict:
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


def test_interactive_point_returns_polygon(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    results, hit, mask_next = inst.predict_interactive(
        fake_image, points=[[0.5, 0.5]], labels=[1], cache_key="ip1"
    )
    assert hit is False
    assert len(results) == 1
    assert results[0]["type"] == "polygonlabels"
    assert results[0]["value"]["polygonlabels"] == ["object"]
    # 单点单 mask (multimask=False) 回灌 low-res logits.
    assert isinstance(mask_next, str) and mask_next


def test_interactive_point_pixel_scaling(predictor_with_mocks, fake_image):
    """归一化点 → 像素 (640x480): [0.5,0.5] → [320,240]; 默认 multimask=False."""
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    inst.predict_interactive(
        fake_image, points=[[0.5, 0.5]], labels=[1], cache_key="ip2"
    )

    kw = inst._model.predict_inst.call_args.kwargs
    assert kw["point_coords"].tolist() == [[320.0, 240.0]]
    assert kw["point_labels"].tolist() == [1]
    assert kw["multimask_output"] is False


def test_interactive_box_pixel_scaling(predictor_with_mocks, fake_image):
    """归一化 xyxy [0.1,0.2,0.5,0.6] → 像素 [64,96,320,288]."""
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    inst.predict_interactive(fake_image, box=[0.1, 0.2, 0.5, 0.6], cache_key="ib1")

    kw = inst._model.predict_inst.call_args.kwargs
    assert kw["box"].tolist() == [64.0, 96.0, 320.0, 288.0]


def test_interactive_mask_input_decoded_and_passed(predictor_with_mocks, fake_image):
    """context.mask_input 解码后适配 SAM3 的 288×288 prompt 输入。"""
    from aap_protocol_v2 import encode_low_res_mask

    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    encoded = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    inst.predict_interactive(
        fake_image,
        points=[[0.5, 0.5], [0.6, 0.6]],
        labels=[1, 1],
        mask_input=encoded,
        cache_key="imi1",
    )
    kw = inst._model.predict_inst.call_args.kwargs
    assert kw["mask_input"].shape == (1, 288, 288)


def test_scribble_consumer_preserves_positive_negative_and_mask_seed(
    predictor_with_mocks,
    fake_image,
):
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    inst.predict_interactive(
        fake_image,
        scribbles=[
            {"polarity": 1, "points": [[0.1, 0.1], [0.4, 0.4]], "width": 0.01},
            {"polarity": 0, "points": [[0.8, 0.8], [0.6, 0.6]], "width": 0.01},
        ],
        mask_prompt=_interactive_mask_prompt(),
        cache_key="scribble-1",
    )

    kwargs = inst._model.predict_inst.call_args.kwargs
    assert set(kwargs["point_labels"].tolist()) == {0, 1}
    assert kwargs["mask_input"].shape == (1, 288, 288)
    assert set(np.unique(kwargs["mask_input"])) == {-16.0, 16.0}


def test_mask_prompt_only_is_consumed_as_dense_prompt(
    predictor_with_mocks,
    fake_image,
):
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    results, _, mask_next = inst.predict_interactive(
        fake_image,
        mask_prompt=_interactive_mask_prompt(),
        cache_key="mask-only-1",
    )

    kwargs = inst._model.predict_inst.call_args.kwargs
    assert "point_coords" not in kwargs
    assert "point_labels" not in kwargs
    assert kwargs["mask_input"].shape == (1, 288, 288)
    assert results
    assert mask_next is not None


def test_interactive_multimask_sorted_by_iou(predictor_with_mocks, fake_image):
    """multimask 3 候选按 iou 降序; 首条 score 最高 (0.95)."""
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(3))

    results, _, mask_next = inst.predict_interactive(
        fake_image,
        points=[[0.5, 0.5]],
        labels=[1],
        multimask_output=True,
        cache_key="im1",
    )
    assert len(results) == 3
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(0.95)
    # 多候选阶段 index 歧义 → 不回灌 mask_input_next.
    assert mask_next is None


def test_interactive_empty_when_no_mask(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    empty = (
        np.zeros((0, 480, 640), dtype=np.float32),
        np.zeros((0,), dtype=np.float32),
        np.zeros((0, 256, 256), dtype=np.float32),
    )
    inst._model.predict_inst = MagicMock(return_value=empty)

    results, _, _ = inst.predict_interactive(
        fake_image, points=[[0.5, 0.5]], labels=[1], cache_key="ie1"
    )
    assert results == []


def test_interactive_native_mask_preserves_pixels(predictor_with_mocks, fake_image):
    from mask_utils import decode_coco_rle

    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    masks, ious, low_res = _fake_inst_output(1)
    masks[0, 101, 101] = 0
    masks[0, 350, 500] = 1
    inst._model.predict_inst = MagicMock(return_value=(masks, ious, low_res))

    results, _, mask_next = inst.predict_interactive(
        fake_image,
        points=[[0.5, 0.5]],
        labels=[1],
        output_geometry="mask",
        prompt_revision="sam3-revision",
        cache_key="native-1",
    )

    assert mask_next is not None
    assert len(results) == 1
    candidate = results[0]
    assert candidate["type"] == "mask"
    assert candidate["value"]["rle"]["size"] == [480, 640]
    assert len(candidate["value"]["preview"]["points"]) >= 4
    assert list(decode_coco_rle(candidate["value"]["rle"])) == (
        (masks[0] > 0).reshape(-1).astype(int).tolist()
    )


def test_interactive_box_native_mask_output(predictor_with_mocks, fake_image):
    inst = predictor_with_mocks
    inst._processor.set_image = MagicMock(return_value=_fake_state_after_set_image())
    inst._model.predict_inst = MagicMock(return_value=_fake_inst_output(1))

    results, _, _ = inst.predict_interactive(
        fake_image,
        box=[0.1, 0.2, 0.5, 0.6],
        output_geometry="mask",
        prompt_revision="sam3-box-revision",
        cache_key="native-box",
    )

    assert len(results) == 1
    assert results[0]["type"] == "mask"
    assert results[0]["value"]["rle"]["size"] == [480, 640]
    assert len(results[0]["value"]["preview"]["points"]) >= 4


@pytest.mark.parametrize("output", ["mask", "both"])
def test_exemplars_native_mask_output(predictor_with_mocks, fake_image, output):
    from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id
    from mask_utils import decode_coco_rle

    inst = predictor_with_mocks
    state = _fake_state_after_set_image()
    inst._processor.set_image = MagicMock(return_value=state)

    def add_geo(box, label, current_state):
        _populate_state_with_outputs(current_state, 3)
        current_state["masks"][1] = False
        current_state["masks"][2, 0, 150, 150] = False
        return current_state

    inst._processor.add_geometric_prompt = MagicMock(side_effect=add_geo)
    inst._processor.reset_all_prompts = MagicMock()

    results, _ = inst.predict_exemplars(
        fake_image,
        [{"bbox": [0.2, 0.2, 0.45, 0.55], "label": True}],
        output=output,
        output_geometry="mask",
        prompt_revision="sam3-exemplar-revision",
        cache_key="native-exemplar",
    )

    expected_types = (
        ["rectanglelabels", "mask"] * 2 if output == "both" else ["mask"] * 2
    )
    assert [result["type"] for result in results] == expected_types
    for index, candidate in enumerate(results):
        if candidate["type"] != "mask":
            continue
        rle = CocoRlePayload.model_validate(candidate["value"]["rle"])
        assert rle.size == [480, 640]
        assert candidate["candidate_id"] == native_mask_candidate_id(
            rle, prompt_revision="sam3-exemplar-revision", candidate_index=index
        )
        pixels = np.array(decode_coco_rle(candidate["value"]["rle"])).reshape(480, 640)
        assert pixels.sum() == (39999 if index == len(results) - 1 else 40000)
        if output == "both":
            box = results[index - 1]
            assert box["score"] == candidate["score"]
            assert box["value"]["rectanglelabels"] == candidate["value"]["masklabels"]
            assert box["value"]["x"] == pytest.approx(100 / 640)


def test_result_box_clips_endpoints_before_computing_size(predictor_with_mocks):
    box = predictor_with_mocks._box_to_rect_label(
        [100, -10, 700, 500], 640, 480, "object", 0.9
    )
    assert box["value"]["x"] == pytest.approx(100 / 640)
    assert box["value"]["y"] == 0
    assert box["value"]["width"] == pytest.approx(540 / 640)
    assert box["value"]["height"] == 1
