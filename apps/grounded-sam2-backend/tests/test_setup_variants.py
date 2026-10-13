"""Tests for /setup rich variant metadata."""

from __future__ import annotations

from main import setup
from predictor import DINO_CONFIGS, SAM2_CONFIGS


def test_setup_supported_variants_match_runtime_variant_keys():
    data = setup()
    groups = {group["key"]: group for group in data["supported_variants"]}

    sam_values = [item["value"] for item in groups["sam_variant"]["variants"]]
    dino_values = [item["value"] for item in groups["dino_variant"]["variants"]]

    assert sam_values == list(SAM2_CONFIGS)
    assert dino_values == list(DINO_CONFIGS)
    assert sam_values == data["params"]["properties"]["sam_variant"]["enum"]
    assert dino_values == data["params"]["properties"]["dino_variant"]["enum"]


def test_setup_supported_variants_include_display_metadata():
    data = setup()
    groups = {group["key"]: group for group in data["supported_variants"]}

    sam_small = next(
        item for item in groups["sam_variant"]["variants"] if item["value"] == "small"
    )
    dino_t = next(
        item for item in groups["dino_variant"]["variants"] if item["value"] == "T"
    )

    assert sam_small["label"] == "SAM 2.1 Small"
    assert sam_small["tier"] == "balanced"
    assert sam_small["recommended"] is True
    assert dino_t["recommended"] is True


def test_setup_default_variants_per_task_axes():
    """每个 model 的 default_variants 必须严格匹配该 model 的 supported_variants 轴.

    - detection (DINO 路径)         → 仅 dino_variant
    - interactive_seg / tracker     → 仅 sam_variant
    - segmentation (DINO + SAM2)    → sam_variant + dino_variant
    - box-seg (SAM 框→mask 原子)    → 仅 sam_variant
    """
    data = setup()
    # task 不再唯一(segmentation 下有 composite + box-seg 两条), 按 id 取以消歧。
    by_id = {m["id"]: m for m in data["models"]}

    detection = by_id["grounded-sam2-detection"]["default_variants"]
    assert set(detection.keys()) == {"dino_variant"}
    assert detection["dino_variant"] in {"T", "B"}

    seg = by_id["grounded-sam2-segmentation"]["default_variants"]
    assert set(seg.keys()) == {"sam_variant", "dino_variant"}

    iseg = by_id["grounded-sam2-interactive-seg"]["default_variants"]
    assert set(iseg.keys()) == {"sam_variant"}

    tracker = by_id["grounded-sam2-tracker"]["default_variants"]
    assert set(tracker.keys()) == {"sam_variant"}

    box_seg = by_id["grounded-sam2-box-seg"]["default_variants"]
    assert set(box_seg.keys()) == {"sam_variant"}


def test_setup_default_variants_match_env_defaults():
    """default_variants 的值应与 backend startup 的 env 默认对齐 (SAM_VARIANT / DINO_VARIANT)."""
    from main import DINO_VARIANT, SAM_VARIANT

    data = setup()
    # task 不再唯一(segmentation 下有 composite + box-seg), 按 id 取以消歧。
    by_id = {m["id"]: m for m in data["models"]}

    assert (
        by_id["grounded-sam2-detection"]["default_variants"]["dino_variant"]
        == DINO_VARIANT
    )
    assert (
        by_id["grounded-sam2-interactive-seg"]["default_variants"]["sam_variant"]
        == SAM_VARIANT
    )
    assert (
        by_id["grounded-sam2-tracker"]["default_variants"]["sam_variant"] == SAM_VARIANT
    )
    assert (
        by_id["grounded-sam2-box-seg"]["default_variants"]["sam_variant"] == SAM_VARIANT
    )
    seg = by_id["grounded-sam2-segmentation"]["default_variants"]
    assert seg["sam_variant"] == SAM_VARIANT
    assert seg["dino_variant"] == DINO_VARIANT


# ---------- warmup_endpoint 声明 ----------


def test_setup_warmup_endpoint_true():
    """协议 §4.4 · 顶层 warmup_endpoint 必须为 True (gsam2 支持 /warmup)."""
    data = setup()
    assert data["warmup_endpoint"] is True
