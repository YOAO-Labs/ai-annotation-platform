"""v0.14.11 · gsam2 协议 v2 多模型目录测试.

验证 /setup 的 models[] 把 gsam2 的 4 条实际能力 (detection / segmentation /
tracker / interactive_seg) 各拆成独立 model 条目, 让平台「协议能力目录」按 task
正确归类。
"""

from __future__ import annotations

from main import _seeds_from_ctx, setup


def _correction_prompt() -> dict:
    return {
        "type": "correction_frame",
        "frame_index": 12,
        "direction": "forward",
        "output_geometry": "mask",
        "mask_prompt": {
            "rle": {
                "encoding": "coco_rle",
                "size": [2, 3],
                "counts": [1, 2, 3],
            },
            "source_annotation_id": "annotation-1",
            "source_version": 3,
            "source_digest": "a" * 64,
        },
    }


def test_setup_top_level_infra_is_pytorch():
    data = setup()
    assert data["infra"] == "pytorch"


def test_setup_protocol_version_v22():
    data = setup()
    assert data["protocol_version"] == "2.2"
    assert data["compat_protocol_versions"] == ["2.1", "2.0"]


def test_setup_exposes_five_models():
    """v0.18.12 起新增 box-seg 原子, 共 5 个 model。"""
    data = setup()
    assert isinstance(data["models"], list)
    assert len(data["models"]) == 5


def test_box_seg_model_is_public_non_interactive_atom():
    """v0.18.12 · 框→mask 批量分割原子: public(无 visibility)、非交互、composition=atom、bbox→polygon。"""
    data = setup()
    box_seg = next(m for m in data["models"] if m["id"] == "grounded-sam2-box-seg")
    assert box_seg["task"] == "segment"
    assert box_seg["is_interactive"] is False
    assert box_seg["supported_prompts"] == ["bbox"]
    assert box_seg["supported_geometric_outputs"] == ["polygon"]
    assert box_seg["composition"] == "atom"
    # public: 不声明 visibility(平台缺省 public), 与 internal 区分。
    assert box_seg.get("visibility") in (None, "public")


def test_composition_annotations():
    """v0.18.12 · 各 model 显式标 composition(原子 vs 内部编排)。"""
    by_id = {m["id"]: m for m in setup()["models"]}
    assert by_id["grounded-sam2-detection"]["composition"] == "atom"
    assert by_id["grounded-sam2-segmentation"]["composition"] == "composite"
    assert by_id["grounded-sam2-interactive-seg"]["composition"] == "atom"
    assert by_id["grounded-sam2-tracker"]["composition"] == "composite"
    assert by_id["grounded-sam2-box-seg"]["composition"] == "atom"


def test_setup_models_cover_protocol_tasks():
    data = setup()
    tasks = {m["task"] for m in data["models"]}
    assert tasks == {"detect", "segment", "interactive_seg", "tracker"}


def test_setup_models_declare_supported_inputs():
    """v0.18.16 · 各 model 显式声明 supported_inputs (一等输入契约)。"""
    by_id = {m["id"]: m for m in setup()["models"]}
    assert by_id["grounded-sam2-detection"]["supported_inputs"] == [
        "full_image",
        "crop",
    ]
    assert by_id["grounded-sam2-segmentation"]["supported_inputs"] == [
        "full_image",
        "crop",
    ]
    assert by_id["grounded-sam2-interactive-seg"]["supported_inputs"] == [
        "bbox_prompt",
        "point_prompt",
        "mask_prompt",
        "scribble_prompt",
        "full_image",
    ]
    assert by_id["grounded-sam2-tracker"]["supported_inputs"] == [
        "video",
        "point_prompt",
        "bbox_prompt",
        "mask_prompt",
    ]
    assert by_id["grounded-sam2-box-seg"]["supported_inputs"] == [
        "bbox_prompt",
        "full_image",
    ]


def test_setup_models_declare_output_attribute_types():
    """v0.18.16 · 文本检测/分割自报类别; 交互/追踪/框→mask 不自产属性 (留空)。score 不入属性。"""
    by_id = {m["id"]: m for m in setup()["models"]}
    assert by_id["grounded-sam2-detection"]["output_attribute_types"] == ["class"]
    assert by_id["grounded-sam2-segmentation"]["output_attribute_types"] == ["class"]
    # 交互分割 / 视频追踪 / 框→mask 细化: 透传或无类别产出, 不声明。
    assert "output_attribute_types" not in by_id["grounded-sam2-interactive-seg"]
    assert "output_attribute_types" not in by_id["grounded-sam2-tracker"]
    assert "output_attribute_types" not in by_id["grounded-sam2-box-seg"]


def test_setup_models_declare_resource_profile():
    """v0.18.16 · 资源画像: 批量模型 batchable=True, 交互/视频追踪逐次 batchable=False (不填 vram)。"""
    by_id = {m["id"]: m for m in setup()["models"]}
    assert by_id["grounded-sam2-detection"]["resource_profile"] == {
        "device": "gpu",
        "batchable": True,
    }
    assert by_id["grounded-sam2-segmentation"]["resource_profile"] == {
        "device": "gpu",
        "batchable": True,
    }
    assert by_id["grounded-sam2-box-seg"]["resource_profile"] == {
        "device": "gpu",
        "batchable": True,
    }
    assert by_id["grounded-sam2-interactive-seg"]["resource_profile"] == {
        "device": "gpu",
        "batchable": False,
    }
    assert by_id["grounded-sam2-tracker"]["resource_profile"] == {
        "device": "gpu",
        "batchable": False,
    }


def test_setup_each_model_carries_infra_pytorch():
    data = setup()
    for m in data["models"]:
        assert m["infra"] == "pytorch"
        assert m["model_family"] == "grounded-sam2"


def test_detection_model_text_to_bbox():
    data = setup()
    det = next(m for m in data["models"] if m["task"] == "detect")
    assert det["supported_prompts"] == ["text"]
    assert det["supported_geometric_outputs"] == ["bbox"]
    assert det["is_interactive"] is False


def test_segmentation_model_text_to_polygon():
    data = setup()
    seg = next(m for m in data["models"] if m["task"] == "segment")
    assert seg["supported_prompts"] == ["text"]
    assert seg["supported_geometric_outputs"] == ["polygon"]


def test_interactive_seg_model_point_box_to_native_mask():
    data = setup()
    inter = next(m for m in data["models"] if m["task"] == "interactive_seg")
    # v0.18.17 · bbox→interactive_box (图像交互单框单 mask).
    assert set(inter["supported_prompts"]) == {
        "point",
        "interactive_box",
        "mask",
        "scribble",
    }
    assert inter["supported_geometric_outputs"] == ["polygon", "mask"]
    assert inter["is_interactive"] is True


def test_image_mask_and_scribble_prompts_are_advertised_only_on_consumer():
    by_id = {m["id"]: m for m in setup()["models"]}
    interactive = by_id["grounded-sam2-interactive-seg"]
    assert {"mask", "scribble"}.issubset(interactive["supported_prompts"])
    assert {"mask_prompt", "scribble_prompt"}.issubset(interactive["supported_inputs"])
    assert "correction_frame" not in interactive["supported_prompts"]
    assert interactive["supported_geometric_outputs"] == ["polygon", "mask"]

    tracker = by_id["grounded-sam2-tracker"]
    assert "correction_frame" in tracker["supported_prompts"]
    assert "point" in tracker["supported_prompts"]
    assert {"mask", "scribble"}.isdisjoint(tracker["supported_prompts"])
    assert "mask_prompt" in tracker["supported_inputs"]
    assert "point_prompt" in tracker["supported_inputs"]
    assert "scribble_prompt" not in tracker["supported_inputs"]
    assert "video" in tracker["supported_inputs"]
    assert tracker["max_window_frames"] > 0


def test_tracker_model_sam2_video():
    data = setup()
    tracker = next(m for m in data["models"] if m["task"] == "tracker")
    assert tracker["supported_trackers"] == ["sam2_video"]
    assert tracker["supported_geometric_outputs"] == ["bbox", "polygon", "mask"]


def test_tracker_preserves_validated_correction_frame_seed():
    prompt = _correction_prompt()

    assert _seeds_from_ctx({"seeds": [{"obj_id": 7, "prompts": [prompt]}]}) == [
        {"obj_id": 7, "prompts": [prompt]}
    ]


def test_top_level_back_compat_fields_unchanged():
    """顶层 supported_prompts / supported_trackers 保留, 供未升级平台合成隐式单 model."""
    data = setup()
    # v0.18.17 · bbox→interactive_box (图像交互); tracker/box-seg 的 bbox 不在顶层 prompts.
    assert set(data["supported_prompts"]) == {
        "point",
        "interactive_box",
        "mask",
        "scribble",
        "text",
    }
    assert data["supported_trackers"] == ["sam2_video"]


def test_setup_params_schema_platform_roles():
    props = setup()["params"]["properties"]
    assert props["box_threshold"]["x-platform-role"] == "confidence"
    assert props["text_threshold"]["x-platform-role"] == "textThreshold"
    assert props["simplify_tolerance"]["x-platform-role"] == "simplifyTolerance"
