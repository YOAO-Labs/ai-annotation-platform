"""v0.14.9 · 能力声明协议 v2 — extract_capabilities / derive_modalities 派生单测.

纯函数测试 (无 DB): 覆盖多模型目录解析、infra 继承/覆盖、向后兼容合成隐式单 model、
扁平并集、模态派生。
"""

from aap_protocol_v2.lifecycle import ManagedLifecycleCapabilities
from pydantic import ValidationError
import pytest

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference import INPUT_VALUES, derive_modalities, extract_capabilities
from app.schemas.ml_backend import BackendCapabilities
from app.services.inference_limits import capability_limits_from_settings


def test_extract_none_returns_none():
    assert extract_capabilities(None) is None
    assert extract_capabilities({}) is None


def test_extract_preserves_only_strict_managed_lifecycle_capability():
    payload = ManagedLifecycleCapabilities().model_dump(mode="json")
    caps = extract_capabilities(
        {
            "name": "managed-backend",
            "managed_lifecycle": payload,
            "supported_prompts": ["none"],
        }
    )

    assert caps["managed_lifecycle"] == payload
    assert not any(
        warning["field"] == "managed_lifecycle" for warning in caps["warnings"]
    )


def test_extract_does_not_upgrade_missing_or_malformed_managed_lifecycle():
    missing = extract_capabilities(
        {"name": "legacy-backend", "supported_prompts": ["none"]}
    )
    assert missing["managed_lifecycle"] is None
    assert not any(
        warning["field"] == "managed_lifecycle" for warning in missing["warnings"]
    )

    canonical = ManagedLifecycleCapabilities().model_dump(mode="json")
    malformed_payloads = [
        {key: value for key, value in canonical.items() if key != "reset_endpoint"},
        {**canonical, "unexpected": True},
        {**canonical, "generation_fencing": 1},
        None,
    ]
    for payload in malformed_payloads:
        caps = extract_capabilities(
            {
                "name": "malformed-backend",
                "managed_lifecycle": payload,
                "supported_prompts": ["none"],
            }
        )
        assert caps["managed_lifecycle"] is None
        assert any(
            warning["field"] == "managed_lifecycle" and warning["value"] == "invalid"
            for warning in caps["warnings"]
        )


def test_backend_capability_schema_cannot_fill_partial_managed_lifecycle():
    capability = ManagedLifecycleCapabilities().model_dump(mode="json")
    parsed = BackendCapabilities.model_validate({"managed_lifecycle": capability})
    assert parsed.managed_lifecycle is not None
    assert parsed.managed_lifecycle.model_dump(mode="json") == capability

    invalid_capabilities = (
        {"protocol_version": "1"},
        {**capability, "generation_fencing": 1},
    )
    for invalid in invalid_capabilities:
        with pytest.raises(ValidationError):
            BackendCapabilities.model_validate({"managed_lifecycle": invalid})


def test_extract_passes_through_output_attribute_schema():
    """协议③ · model 的 output_attribute_schema (含 select options) 透传到能力快照."""
    setup = {
        "name": "onnxtools-backend",
        "infra": "onnx",
        "models": [
            {
                "id": "vehicle-attr",
                "task": "detection",
                "supported_geometric_outputs": ["bbox"],
                "output_attribute_types": ["class"],
                "output_attribute_schema": [
                    {
                        "key": "vehicle_type",
                        "type": "select",
                        "options": [{"value": "car", "label": "小车"}],
                    },
                    {
                        "key": "color",
                        "type": "select",
                        "options": [{"value": "blue", "label": "蓝色"}],
                    },
                ],
            }
        ],
    }
    model = extract_capabilities(setup)["models"][0]
    assert [f["key"] for f in model["output_attribute_schema"]] == [
        "vehicle_type",
        "color",
    ]
    assert model["output_attribute_schema"][0]["options"][0]["value"] == "car"


def test_exemplar_capabilities_passthrough_multi_model():
    """v0.18.23 · 多模型 backend (yolo) 的 exemplar 模型 exemplar_capabilities 透传到能力快照。"""
    setup = {
        "name": "yolo-backend",
        "infra": "pytorch",
        "models": [
            {
                "id": "exemplar-yoloe",
                "task": "interactive_seg",
                "is_interactive": True,
                "supported_prompts": ["exemplar"],
                "supported_geometric_outputs": ["bbox", "polygon"],
                "exemplar_capabilities": {
                    "multi_box": True,
                    "negative_box": False,
                    "text_combination": False,
                    "threshold_refilter": True,
                },
            }
        ],
    }
    model = extract_capabilities(setup)["models"][0]
    assert model["exemplar_capabilities"]["negative_box"] is False
    assert model["exemplar_capabilities"]["text_combination"] is False


def test_exemplar_capabilities_passthrough_legacy_single_model():
    """v0.18.23 · 老 backend (sam3) 顶层 exemplar_capabilities 经合成单 model 透传。"""
    setup = {
        "name": "sam3-backend",
        "is_interactive": True,
        "supported_prompts": ["exemplar", "text"],
        "supported_geometric_outputs": ["polygon"],
        "exemplar_capabilities": {"multi_box": True, "negative_box": True},
    }
    model = extract_capabilities(setup)["models"][0]
    assert model["exemplar_capabilities"]["negative_box"] is True


def test_exemplar_capabilities_absent_is_none():
    """缺字段 = None (前端按全支持向后兼容)。"""
    setup = {
        "name": "yolo-backend",
        "infra": "pytorch",
        "models": [
            {
                "id": "detect",
                "task": "detection",
                "supported_geometric_outputs": ["bbox"],
            }
        ],
    }
    model = extract_capabilities(setup)["models"][0]
    assert model["exemplar_capabilities"] is None


def test_composition_passthrough_and_default():
    """协议 v2.2 · composition 透传; 缺省 atom（visibility 字段已删,仅留 composition 一根轴）。"""
    setup = {
        "name": "onnxtools-backend",
        "infra": "onnx",
        "models": [
            # 显式 composite（一锅端：内部编排复合）。
            {
                "id": "vehicle-attr",
                "task": "detection",
                "composition": "composite",
                "supported_geometric_outputs": ["bbox"],
            },
            # 显式 atom（纯检测原子）。
            {
                "id": "vehicle-detect",
                "task": "detection",
                "composition": "atom",
                "supported_geometric_outputs": ["bbox"],
            },
            # 不声明 composition → 缺省 atom。
            {
                "id": "vehicle-attr-classify",
                "task": "classification",
                "supported_geometric_outputs": ["none"],
            },
        ],
    }
    models = {m["id"]: m for m in extract_capabilities(setup)["models"]}
    assert models["vehicle-attr"]["composition"] == "composite"
    assert models["vehicle-detect"]["composition"] == "atom"
    # 缺省回落 atom。
    assert models["vehicle-attr-classify"]["composition"] == "atom"
    # visibility 字段已删:不应再透传。
    assert "visibility" not in models["vehicle-attr"]


def test_legacy_backend_composition_defaults_atom():
    """老 backend（无 models[]）合成的隐式单 model 也带 composition=atom。"""
    setup = {
        "name": "grounded-sam2-backend",
        "is_interactive": True,
        "supported_prompts": ["point", "bbox", "text"],
    }
    model = extract_capabilities(setup)["models"][0]
    assert model["composition"] == "atom"


# ── 多模型 backend (YOLO 官仓: 按任务分条目) ──


def _yolo_setup() -> dict:
    return {
        "name": "yolo-ultralytics-backend",
        "version": "0.1.0",
        "model_version": "ultralytics-8.3.x",
        "infra": "pytorch",
        "is_interactive": False,
        "models": [
            {
                "id": "detect",
                "display_name": "YOLO 目标检测",
                "task": "detection",
                "model_family": "yolo",
                "supported_prompts": ["none"],
                "supported_geometric_outputs": ["bbox"],
                "supported_variants": [
                    {"key": "series", "variants": [{"value": "yolo11"}]},
                    {"key": "size", "variants": [{"value": "n"}, {"value": "s"}]},
                ],
                "default_thresholds": {"conf": 0.25, "iou": 0.7},
            },
            {
                "id": "segment",
                "task": "segmentation",
                "model_family": "yolo",
                "supported_geometric_outputs": ["polygon"],
            },
            {
                "id": "pose",
                "task": "keypoint",
                "model_family": "yolo",
                # 不声明几何 → 按 task 默认补全 keypoint
            },
            {
                "id": "obb",
                "task": "obb",
                "model_family": "yolo",
                "supported_geometric_outputs": ["rotated_bbox"],
            },
            {
                "id": "classify",
                "task": "classification",
                "model_family": "yolo",
                "supported_geometric_outputs": ["none"],
                "output_attribute_types": ["class"],
            },
        ],
    }


def test_multi_model_parses_each_entry():
    caps = extract_capabilities(_yolo_setup())
    assert caps is not None
    assert caps["infra"] == "pytorch"
    models = {m["id"]: m for m in caps["models"]}
    assert set(models) == {"detect", "segment", "pose", "obb", "classify"}
    assert models["detect"]["task"] == "detect"
    assert models["detect"]["model_family"] == "yolo"
    # infra 缺省继承 backend
    assert models["detect"]["infra"] == "pytorch"
    # 几何未声明按 task 默认补全
    assert models["pose"]["supported_geometric_outputs"] == ["keypoint"]
    # OCR/cls 属性输出
    assert models["classify"]["output_attribute_types"] == ["class"]


def test_classes_passthrough():
    """v0.14.17 · 闭集类别表 (yolo model.names) 透传到 model 条目; 缺省为 []."""
    setup = {
        "name": "yolo-backend",
        "infra": "pytorch",
        "models": [
            {
                "id": "detect",
                "task": "detection",
                "supported_geometric_outputs": ["bbox"],
                "classes": [
                    {"index": 0, "name": "person"},
                    {"index": 2, "name": "car"},
                ],
            },
            {
                "id": "segment",
                "task": "segmentation",
                "supported_geometric_outputs": ["polygon"],
            },
        ],
    }
    caps = extract_capabilities(setup)
    models = {m["id"]: m for m in caps["models"]}
    assert models["detect"]["classes"] == [
        {"index": 0, "name": "person"},
        {"index": 2, "name": "car"},
    ]
    # 未带 classes 的 model → 空列表 (前端据此回退"不按类筛选").
    assert models["segment"]["classes"] == []


def test_multi_model_flat_union():
    caps = extract_capabilities(_yolo_setup())
    assert caps is not None
    # 扁平并集 (向后兼容消费方): 几何去重合并
    geo = caps["supported_geometric_outputs"]
    assert set(geo) == {"bbox", "polygon", "keypoint", "rotated_bbox", "none"}
    # 纯批量 YOLO → image 模态
    assert caps["modalities"] == ["image"]


def test_model_infra_override():
    setup = {
        "name": "onnx-zoo",
        "infra": "onnx",
        "models": [
            {
                "id": "yolo",
                "task": "detection",
                "supported_geometric_outputs": ["bbox"],
            },
            {
                "id": "ppocr",
                "task": "ocr",
                "infra": "paddle",  # 覆盖 backend 默认
                "supported_geometric_outputs": ["polygon"],
                "output_attribute_types": ["text", "language"],
            },
        ],
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    models = {m["id"]: m for m in caps["models"]}
    assert models["yolo"]["infra"] == "onnx"  # 继承
    assert models["ppocr"]["infra"] == "paddle"  # 覆盖
    assert "text" in models["ppocr"]["output_attribute_types"]


# ── 向后兼容: 老 backend 无 models[] → 合成隐式单 model ──


def test_supported_inputs_explicit_passthrough():
    """显式声明 supported_inputs 原样透传, 不被合成覆盖。"""
    setup = {
        "name": "bk",
        "infra": "onnx",
        "models": [
            {
                "id": "m",
                "task": "detection",
                "supported_inputs": ["full_image"],
                "supported_geometric_outputs": ["bbox"],
            }
        ],
    }
    m = extract_capabilities(setup)["models"][0]
    assert m["supported_inputs"] == ["full_image"]
    assert m["default_input_type"] == "full_image"


def test_default_input_type_explicit_passthrough_and_fallback():
    """default_input_type 显式透传; 未声明时按 supported_inputs[0] 兜底。"""
    setup = {
        "name": "bk",
        "infra": "onnx",
        "models": [
            {
                "id": "source",
                "task": "detection",
                "supported_inputs": ["crop", "full_image"],
                "default_input_type": "full_image",
                "supported_geometric_outputs": ["bbox"],
            },
            {
                "id": "roi",
                "task": "classification",
                "supported_inputs": ["crop"],
            },
        ],
    }
    models = {m["id"]: m for m in extract_capabilities(setup)["models"]}
    assert models["source"]["default_input_type"] == "full_image"
    assert models["roi"]["default_input_type"] == "crop"


def test_supported_inputs_synthesized_for_plain_detector():
    """纯检测器 (无交互 prompt) 缺字段 → 合成 [full_image, crop] (可作 crop 下游)。"""
    setup = {
        "name": "bk",
        "infra": "onnx",
        "models": [
            {"id": "det", "task": "detection", "supported_geometric_outputs": ["bbox"]}
        ],
    }
    m = extract_capabilities(setup)["models"][0]
    assert m["supported_inputs"] == ["full_image", "crop"]
    assert m["default_input_type"] == "full_image"
    assert "video" in INPUT_VALUES
    assert "video" not in m["supported_inputs"]


def test_supported_inputs_synthesized_for_box_seg():
    """box-prompt seg (supported_prompts=[bbox]) → [bbox_prompt, full_image], 不含 crop。"""
    setup = {
        "name": "bk",
        "infra": "pytorch",
        "models": [
            {
                "id": "boxseg",
                "task": "interactive_seg",
                "supported_prompts": ["bbox"],
                "supported_geometric_outputs": ["polygon"],
            }
        ],
    }
    m = extract_capabilities(setup)["models"][0]
    assert m["supported_inputs"] == ["bbox_prompt", "full_image"]


def test_supported_inputs_synthesized_for_native_mask_prompts():
    """旧 backend 缺 inputs 时，原生 Mask 相关 prompt 合成受控输入形态。"""
    setup = {
        "name": "mask-seg",
        "infra": "pytorch",
        "models": [
            {
                "id": "mask-seg",
                "task": "interactive_seg",
                "supported_prompts": ["mask", "scribble", "correction_frame"],
                "supported_geometric_outputs": ["mask"],
            }
        ],
    }
    m = extract_capabilities(setup)["models"][0]
    assert m["supported_inputs"] == [
        "mask_prompt",
        "scribble_prompt",
        "full_image",
    ]


def test_supported_inputs_union_and_legacy_synth():
    """扁平并集含 supported_inputs; 老 backend (无 models[]) 也合成。"""
    setup = {
        "name": "legacy-det",
        "supported_prompts": ["none"],
        "supported_geometric_outputs": ["bbox"],
    }
    caps = extract_capabilities(setup)
    assert "crop" in caps["supported_inputs"]
    assert caps["models"][0]["supported_inputs"] == ["full_image", "crop"]


def test_legacy_grounded_sam2_synthesizes_single_model():
    # grounded-sam2: 有 prompts + trackers, 无 models[] / infra
    setup = {
        "name": "grounded-sam2",
        "version": "0.10.1",
        "model_version": "grounded-sam2-dinoT-sam2.1tiny",
        "is_interactive": True,
        "supported_prompts": ["point", "bbox", "text"],
        "supported_text_outputs": ["box", "mask", "both"],
        "supported_trackers": ["sam2_video"],
        "supported_variants": [{"key": "sam_variant", "variants": [{"value": "tiny"}]}],
        "params": {"type": "object", "properties": {}},
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    # 合成单 model
    assert len(caps["models"]) == 1
    m = caps["models"][0]
    assert m["id"] == "grounded-sam2"
    assert m["task"] == "tracker"  # 有 trackers 优先判 tracker
    assert m["infra"] == "unknown"  # 老 backend 不报 infra
    # 扁平字段与升级前完全一致 (向后兼容)
    assert caps["is_interactive"] is True
    assert caps["supported_prompts"] == ["point", "bbox", "text"]
    assert caps["supported_trackers"] == ["sam2_video"]
    assert caps["supported_text_outputs"] == ["box", "mask", "both"]
    # image (prompts 非空) + video (trackers 非空) 双修保留
    assert caps["modalities"] == ["image", "video"]


def test_default_variants_passthrough():
    """v0.14.13 · backend 自报的 default_variants 必须透传到 caps['models'][].default_variants."""
    setup = {
        "name": "yolo",
        "infra": "pytorch",
        "models": [
            {
                "id": "detect",
                "task": "detection",
                "supported_variants": [
                    {"key": "series", "variants": [{"value": "yolo11"}]},
                    {"key": "size", "variants": [{"value": "s"}]},
                ],
                "default_variants": {"series": "yolo11", "size": "s"},
            },
            {
                "id": "no-default",
                "task": "detection",
                # 不报 default_variants → 派生层应给空 dict, 不报错
            },
        ],
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    models = {m["id"]: m for m in caps["models"]}
    assert models["detect"]["default_variants"] == {"series": "yolo11", "size": "s"}
    assert models["no-default"]["default_variants"] == {}


def test_legacy_detection_only_backend():
    # echo backend: 无 prompts/trackers/models → detection 单 model, 仅展示
    setup = {"name": "echo-backend", "labels": ["demo"], "is_interactive": False}
    caps = extract_capabilities(setup)
    assert caps is not None
    assert len(caps["models"]) == 1
    assert caps["models"][0]["task"] == "detect"
    assert caps["infra"] == "unknown"
    # 无 prompts/trackers → 扁平为空; per-model 补 image
    assert caps["modalities"] == ["image"]


# ── 模态派生 ──


def test_modality_video_from_tracker_task():
    setup = {
        "name": "tracker-only",
        "infra": "onnx",
        "models": [
            {"id": "t", "task": "tracker", "supported_trackers": ["sam2_video"]}
        ],
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    assert caps["modalities"] == ["video"]


def test_tracker_max_window_is_clamped_to_platform_limit():
    caps = extract_capabilities(
        {
            "name": "sam3",
            "models": [
                {
                    "id": "tracker",
                    "task": "tracker",
                    "supported_trackers": ["sam3_video_interactive"],
                    "max_window_frames": 64,
                }
            ],
        },
        limits=CapabilityLimits(video_tracker_sam3_window_size_frames=16),
    )

    assert caps["models"][0]["max_window_frames"] == 16


def test_api_limit_adapter_passes_current_tracker_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "video_tracker_window_size_frames", 38)
    monkeypatch.setattr(settings, "video_tracker_sam3_window_size_frames", 7)

    assert capability_limits_from_settings() == CapabilityLimits(
        video_tracker_window_size_frames=38,
        video_tracker_sam3_window_size_frames=7,
    )


def test_tracker_non_positive_max_window_is_not_advertised():
    caps = extract_capabilities(
        {
            "name": "broken-tracker",
            "models": [
                {
                    "id": "tracker",
                    "task": "tracker",
                    "supported_trackers": ["sam2_video"],
                    "max_window_frames": 0,
                }
            ],
        }
    )

    assert caps["models"][0]["max_window_frames"] is None


def test_modality_lidar_from_geometry():
    setup = {
        "name": "pc",
        "models": [
            {
                "id": "box3d",
                "task": "detection",
                "supported_geometric_outputs": ["lidar_box_3d"],
            }
        ],
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    assert caps["modalities"] == ["lidar"]


def test_derive_modalities_empty_caps():
    assert derive_modalities(None) == []
    assert derive_modalities({}) == []


# ---------- v0.14.14: warmup_endpoint 透传 ----------


def test_warmup_endpoint_true_passthrough():
    """v0.14.14 协议 §4.4 · backend 自报 warmup_endpoint=true 时 caps 也带 true."""
    setup = {
        "name": "yolo",
        "infra": "pytorch",
        "warmup_endpoint": True,
        "models": [{"id": "detect", "task": "detection"}],
    }
    caps = extract_capabilities(setup)
    assert caps is not None
    assert caps["warmup_endpoint"] is True


def test_warmup_endpoint_default_false():
    """老 backend 缺字段时, warmup_endpoint 默认 False (前端 ⚡ 按钮置灰)."""
    setup = {"name": "echo", "models": [{"id": "d", "task": "detection"}]}
    caps = extract_capabilities(setup)
    assert caps is not None
    assert caps["warmup_endpoint"] is False
