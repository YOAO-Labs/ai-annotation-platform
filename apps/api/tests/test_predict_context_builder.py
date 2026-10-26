"""Prediction contexts emit canonical tasks separately from prompt modes.

Legacy task_type inputs remain accepted for structured and flat contexts.
Text contexts retain their prompt type and carry an independent visual task.
"""

from __future__ import annotations

from aap_inference.execution_context import build_predict_context, model_label


def _ctx(**kw):
    base = dict(
        prompt=None,
        output_mode="mask",
        params=None,
        model_id=None,
        task_type=None,
        model_variants=None,
    )
    base.update(kw)
    return build_predict_context(**base)


# ── 协议 v2 结构化路径 (YOLO) ──────────────────────────────────────────────


def test_v2_structured_context_for_yolo():
    ctx = _ctx(
        task_type="detection",
        model_id="detect",
        model_variants={"series": "yolo11", "size": "s"},
        params={"conf": 0.4, "iou": 0.6, "max_det": 100},
    )
    assert ctx == {
        "task": "detect",
        "model_variants": {"series": "yolo11", "size": "s"},
        "params": {"conf": 0.4, "iou": 0.6, "max_det": 100},
        "model_id": "detect",
    }


def test_v2_defaults_type_when_task_type_missing():
    ctx = _ctx(model_variants={"series": "yolo11", "size": "s"})
    assert ctx["task"] == "detect"
    assert "type" not in ctx
    assert ctx["model_variants"] == {"series": "yolo11", "size": "s"}
    assert ctx["params"] == {}


def test_v2_class_filter_passthrough():
    ctx = _ctx(
        task_type="detection",
        model_variants={"series": "yolo11", "size": "s"},
        class_filter=[0, 2],
    )
    assert ctx["classes"] == [0, 2]


def test_v2_no_class_filter_omits_classes_key():
    ctx = _ctx(
        task_type="detection",
        model_variants={"series": "yolo11", "size": "s"},
    )
    assert "classes" not in ctx


def test_v2_empty_model_variants_still_passes_class_filter():
    """半就绪态: 几何 backend 已带 class_filter 但 variant 轴未就位 (model_variants={})。

    判定用 `is not None` 而非真值 → 空 dict 仍走 v2 路径, class_filter 不被静默丢弃
    (PR #35 审查 🟡). 此前 `if model_variants:` 把 {} 判 falsy 落入扁平路径, classes 丢失。
    """
    ctx = _ctx(
        task_type="detection",
        model_variants={},
        class_filter=[1, 3],
    )
    assert ctx is not None
    assert ctx["model_variants"] == {}
    assert ctx["classes"] == [1, 3]
    assert ctx["task"] == "detect"
    assert "type" not in ctx


def test_prompt_takes_precedence_text_path():
    """v0.18.12 统一 wire · prompt 非空 → 走文本扁平路径 (即便带 model_variants)。

    gsam2/sam3 现同时发 prompt + model_id + model_variants(sam/dino); 必须落文本路径
    (顶层 params/output/阈值), 而非 v2 嵌套路径 (后端按顶层读 box_threshold)。几何 backend
    不发 prompt, 故不会误入此路径。"""
    ctx = _ctx(
        task_type="segmentation",
        model_id="grounded-sam2-segmentation",
        model_variants={"sam_variant": "tiny", "dino_variant": "T"},
        prompt="car",
        output_mode="both",
        box_threshold=0.3,
        text_threshold=0.25,
    )
    assert ctx["type"] == "text"  # 文本路径, 非几何 task type
    assert ctx["task"] == "segment"
    assert ctx["text"] == "car"
    assert ctx["output"] == "both"
    assert ctx["model_id"] == "grounded-sam2-segmentation"
    assert ctx["model_variants"] == {"sam_variant": "tiny", "dino_variant": "T"}
    assert ctx["box_threshold"] == 0.3  # 顶层阈值 (后端按顶层读)


def test_text_path_without_model_id_back_compat():
    """老 wire 兼容: 文本路径无 model_id/model_variants 时不应出现这两个键。"""
    ctx = _ctx(prompt="car", output_mode="mask")
    assert ctx["type"] == "text"
    assert "model_id" not in ctx
    assert "model_variants" not in ctx


# ── 既有扁平路径 (gsam2 文本 / OCR), 防回归 ─────────────────────────────────


def test_flat_text_path_unchanged():
    ctx = _ctx(
        prompt="car",
        output_mode="box",
        params={"box_threshold": 0.3, "text_threshold": 0.25},
        box_threshold=0.35,
        text_threshold=0.28,
    )
    assert ctx["type"] == "text"
    assert ctx["text"] == "car"
    assert ctx["output"] == "box"
    # 项目级阈值先写, 再被 params 覆盖 (与 v0.14.9 行为一致).
    assert ctx["box_threshold"] == 0.3
    assert ctx["text_threshold"] == 0.25


def test_flat_task_type_override_for_ocr():
    """Legacy OCR task inputs become canonical tasks without prompt modes."""
    ctx = _ctx(prompt="", task_type="ocr", model_id="ocr-model")
    assert ctx["task"] == "ocr"
    assert "type" not in ctx
    assert ctx["model_id"] == "ocr-model"


def test_flat_path_passes_through_params():
    """Flat parameters cannot override the canonical task or prompt mode."""
    ctx = _ctx(
        task_type="classification",
        model_id="vehicle-attr-classify",
        params={"conf": 0.5, "task": "detect", "type": "should-be-ignored"},
    )
    assert ctx["task"] == "classify"
    assert "type" not in ctx
    assert ctx["model_id"] == "vehicle-attr-classify"
    assert ctx["conf"] == 0.5  # 非保留键透传


def test_flat_path_none_params_is_noop():
    """flat 路径 params=None 时不应引发错误, 与既有行为等价。"""
    ctx = _ctx(task_type="ocr", model_id="ocr-model", params=None)
    assert ctx == {"task": "ocr", "model_id": "ocr-model"}


def test_no_prompt_no_task_returns_none():
    assert _ctx() is None


# ── model_label: 与 backend 回传 model_version 一致的展示串 (series+size) ──


def test_model_label_series_size():
    assert model_label({"series": "yolov8", "size": "l"}) == "yolov8l"


def test_model_label_empty_or_none_returns_none():
    # variant 轴未就位 (空 dict) / None: 不展示误导标签
    assert model_label({}) is None
    assert model_label(None) is None


def test_model_label_partial_falls_back_to_concat_values():
    # 缺 size 时退化为拼接所有非空值, 保证非空时有可读串
    assert model_label({"series": "rtdetr"}) == "rtdetr"
