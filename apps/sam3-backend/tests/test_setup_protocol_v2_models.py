"""v0.14.11 · SAM 3 协议 v2 多模型目录测试.

验证 /setup 的 models[] 暴露 detection / segmentation / interactive_seg /
tracker 四条能力。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(scope="module")
def setup_fn():
    """干净加载 main.py 的 setup, mock 掉 sam3 / torch GPU 依赖。"""
    fake_sam3_mod = types.ModuleType("sam3")
    fake_sam3_mod.build_sam3_image_model = MagicMock(return_value=MagicMock())
    sys.modules.setdefault("sam3", fake_sam3_mod)
    sys.modules.pop("main", None)
    import main as m  # noqa: PLC0415

    return m.setup


def test_setup_top_level_infra_is_pytorch(setup_fn):
    data = setup_fn()
    assert data["infra"] == "pytorch"


def test_setup_protocol_version_v22(setup_fn):
    data = setup_fn()
    assert data["protocol_version"] == "2.2"
    assert data["compat_protocol_versions"] == ["2.1", "2.0"]


def test_setup_exposes_five_models(setup_fn):
    data = setup_fn()
    assert isinstance(data["models"], list)
    assert len(data["models"]) == 5


def test_setup_models_cover_protocol_tasks(setup_fn):
    data = setup_fn()
    tasks = {m["task"] for m in data["models"]}
    assert tasks == {"detect", "segment", "interactive_seg", "tracker"}


def test_setup_each_model_carries_infra_pytorch(setup_fn):
    data = setup_fn()
    for m in data["models"]:
        assert m["infra"] == "pytorch"
        assert m["model_family"] == "sam3"


def test_setup_models_declare_supported_inputs(setup_fn):
    """v0.18.16 · 各 model 显式声明 supported_inputs (一等输入契约)。"""
    by_id = {m["id"]: m for m in setup_fn()["models"]}
    assert by_id["sam3-detection"]["supported_inputs"] == ["full_image", "crop"]
    assert by_id["sam3-segmentation"]["supported_inputs"] == ["full_image", "crop"]
    assert by_id["sam3-interactive-seg"]["supported_inputs"] == [
        "point_prompt",
        "bbox_prompt",
        "mask_prompt",
        "scribble_prompt",
        "full_image",
    ]
    assert by_id["sam3-video-tracker"]["supported_inputs"] == [
        "video",
        "bbox_prompt",
    ]
    assert by_id["sam3-video-interactive-tracker"]["supported_inputs"] == [
        "video",
        "point_prompt",
        "bbox_prompt",
        "mask_prompt",
    ]


def test_setup_models_declare_output_attribute_types(setup_fn):
    """v0.18.16 · 检测/分割自报类别; 交互分割不自产属性 (留空)。score 不入属性。"""
    by_id = {m["id"]: m for m in setup_fn()["models"]}
    assert by_id["sam3-detection"]["output_attribute_types"] == ["class"]
    assert by_id["sam3-segmentation"]["output_attribute_types"] == ["class"]
    assert "output_attribute_types" not in by_id["sam3-interactive-seg"]


def test_setup_models_declare_resource_profile(setup_fn):
    """v0.18.16 · 资源画像: 批量模型 batchable=True, 交互分割逐次 batchable=False (不填 vram)。"""
    by_id = {m["id"]: m for m in setup_fn()["models"]}
    assert by_id["sam3-detection"]["resource_profile"] == {
        "device": "gpu",
        "batchable": True,
    }
    assert by_id["sam3-segmentation"]["resource_profile"] == {
        "device": "gpu",
        "batchable": True,
    }
    assert by_id["sam3-interactive-seg"]["resource_profile"] == {
        "device": "gpu",
        "batchable": False,
    }


def test_detection_model_text_to_bbox(setup_fn):
    data = setup_fn()
    det = next(m for m in data["models"] if m["task"] == "detect")
    assert det["supported_prompts"] == ["text"]
    assert det["supported_geometric_outputs"] == ["bbox"]
    assert det["is_interactive"] is False


def test_segmentation_model_text_to_polygon(setup_fn):
    data = setup_fn()
    seg = next(m for m in data["models"] if m["task"] == "segment")
    assert seg["supported_prompts"] == ["text"]
    assert seg["supported_geometric_outputs"] == ["bbox", "polygon"]


def test_tracker_models_declare_each_sam3_video_mode(setup_fn):
    by_id = {m["id"]: m for m in setup_fn()["models"]}
    tracker = by_id["sam3-video-tracker"]
    assert tracker["supported_trackers"] == ["sam3_video"]
    assert tracker["text_driven_trackers"] == ["sam3_video"]
    assert by_id["sam3-video-interactive-tracker"]["supported_trackers"] == [
        "sam3_video_interactive"
    ]


def test_interactive_seg_model_prompts(setup_fn):
    """v0.18.17 · 交互分割含 SAM-style point/interactive_box (inst) + exemplar (PCS)."""
    data = setup_fn()
    inter = next(m for m in data["models"] if m["task"] == "interactive_seg")
    assert inter["supported_prompts"] == [
        "point",
        "interactive_box",
        "mask",
        "scribble",
        "exemplar",
    ]
    assert inter["supported_geometric_outputs"] == ["polygon", "mask"]
    assert inter["is_interactive"] is True


def test_image_mask_and_scribble_consumers_are_advertised(
    setup_fn,
):
    """SAM3 image advertises Mask/scribble only after consumer coverage."""
    inter = next(m for m in setup_fn()["models"] if m["task"] == "interactive_seg")
    assert {"mask", "scribble"}.issubset(inter["supported_prompts"])
    assert {"mask_prompt", "scribble_prompt"}.issubset(inter["supported_inputs"])
    assert "correction_frame" not in inter["supported_prompts"]
    assert inter["supported_geometric_outputs"] == ["polygon", "mask"]


def test_only_pvs_advertises_mask_correction_seed(setup_fn):
    by_id = {m["id"]: m for m in setup_fn()["models"]}
    tracker = by_id["sam3-video-tracker"]
    assert {"mask", "scribble", "correction_frame"}.isdisjoint(
        tracker["supported_prompts"]
    )
    assert {"mask_prompt", "scribble_prompt"}.isdisjoint(tracker["supported_inputs"])
    assert tracker["supported_inputs"] == ["video", "bbox_prompt"]
    assert tracker["supported_geometric_outputs"] == ["bbox", "polygon", "mask"]
    assert tracker["max_window_frames"] > 0
    pvs = by_id["sam3-video-interactive-tracker"]
    assert "correction_frame" in pvs["supported_prompts"]
    assert "mask_prompt" in pvs["supported_inputs"]
    assert pvs["supported_geometric_outputs"] == ["bbox", "polygon", "mask"]
    assert pvs["max_window_frames"] > 0


def test_models_carry_composition_dimension(setup_fn):
    """v0.18.12 · composition 维度: 检测/交互分割=atom, 文本分割(内置 检测→分割)=composite."""
    data = setup_fn()
    comp = {m["task"]: m.get("composition") for m in data["models"]}
    assert comp["detect"] == "atom"
    assert comp["segment"] == "composite"
    assert comp["interactive_seg"] == "atom"


def test_top_level_back_compat_fields_unchanged(setup_fn):
    """v0.18.17 · 顶层 supported_prompts: point/interactive_box/text/exemplar (bbox 已退役)."""
    data = setup_fn()
    assert set(data["supported_prompts"]) == {
        "point",
        "interactive_box",
        "mask",
        "scribble",
        "text",
        "exemplar",
    }


def test_setup_params_schema_platform_roles(setup_fn):
    props = setup_fn()["params"]["properties"]
    assert props["score_threshold"]["x-platform-role"] == "confidence"
    assert props["simplify_tolerance"]["x-platform-role"] == "simplifyTolerance"
    assert props["model_variant"]["x-platform-role"] == "modelVariant"
