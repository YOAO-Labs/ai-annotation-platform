"""Vocab 包级单测. 协议 v2 受控词表常量稳定性 + 类型正确."""

from __future__ import annotations

from aap_protocol_v2 import (
    GEOMETRY_VALUES,
    INFRA_VALUES,
    INPUT_VALUES,
    PROMPT_VALUES,
    TASK_VALUES,
)
from aap_protocol_v2.vocab import TASK_DEFAULT_GEOMETRY


def test_task_values_immutable_tuple() -> None:
    assert isinstance(TASK_VALUES, tuple)
    assert "detect" in TASK_VALUES
    assert "obb" in TASK_VALUES
    assert "segment" in TASK_VALUES
    assert "pose" in TASK_VALUES


def test_infra_values_immutable_tuple() -> None:
    assert isinstance(INFRA_VALUES, tuple)
    assert "pytorch" in INFRA_VALUES
    assert "onnx" in INFRA_VALUES
    assert "other" in INFRA_VALUES


def test_geometry_values_cover_yolo_outputs() -> None:
    """yolo det/seg/pose/obb 四 task 输出几何必须在受控词表内."""
    assert "bbox" in GEOMETRY_VALUES
    assert "polygon" in GEOMETRY_VALUES
    assert "keypoint" in GEOMETRY_VALUES
    assert "rotated_bbox" in GEOMETRY_VALUES


def test_prompt_values_include_none_for_batch_only() -> None:
    """yolo 走 supported_prompts=['none'], 必须在词表里."""
    assert "none" in PROMPT_VALUES
    assert "text" in PROMPT_VALUES
    assert "exemplar" in PROMPT_VALUES
    assert "interactive_box" in PROMPT_VALUES
    assert "mask" in PROMPT_VALUES
    assert "scribble" in PROMPT_VALUES
    assert "correction_frame" in PROMPT_VALUES


def test_input_values_cover_native_mask_interaction() -> None:
    assert "mask_prompt" in INPUT_VALUES
    assert "scribble_prompt" in INPUT_VALUES
    assert "video" in INPUT_VALUES


def test_task_default_geometry_consistent() -> None:
    """每个 task 默认几何必须出自 GEOMETRY_VALUES."""
    for task, geoms in TASK_DEFAULT_GEOMETRY.items():
        assert task in TASK_VALUES, f"{task} 不在 TASK_VALUES"
        for g in geoms:
            assert g in GEOMETRY_VALUES, f"{task} 默认几何 {g} 不在 GEOMETRY_VALUES"
