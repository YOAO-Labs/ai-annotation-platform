"""协议契约校验 (extract_capabilities.warnings) + prompt 受控词表 SSOT 单测.

覆盖合法与越界声明的诊断、model_id 溯源、缺省值和交互 prompt 集合。
"""

from yoao_inference.capability_registry import (
    PROMPTS_INTERACTIVE_ROUTE,
    PROMPTS_REQUIRES_INPUT,
)
from yoao_inference import extract_capabilities


def test_warnings_empty_for_valid_setup():
    setup = {
        "name": "yolo-backend",
        "infra": "pytorch",
        "models": [
            {
                "id": "detect",
                "task": "detection",
                "supported_prompts": ["none"],
                "supported_geometric_outputs": ["bbox"],
            }
        ],
    }
    assert extract_capabilities(setup)["warnings"] == []


def test_warnings_flag_unknown_task_prompt_geometry():
    setup = {
        "name": "bad-backend",
        "infra": "pytorch",
        "models": [
            {
                "id": "m1",
                "task": "seg",  # 越界 (应为 segmentation)
                "supported_prompts": ["point", "boxes"],  # boxes 越界
                "supported_geometric_outputs": ["polygon", "circle"],  # circle 越界
            }
        ],
    }
    warnings = extract_capabilities(setup)["warnings"]
    flagged = {(w["field"], w["value"]) for w in warnings}
    assert ("task", "seg") in flagged
    assert ("supported_prompts", "boxes") in flagged
    assert ("supported_geometric_outputs", "circle") in flagged
    # 合法值不报
    assert ("supported_prompts", "point") not in flagged
    assert ("supported_geometric_outputs", "polygon") not in flagged
    # 每条带 model_id 溯源
    assert all(w["model_id"] == "m1" for w in warnings)


def test_absent_task_not_flagged():
    """backend 没声明 task → 规范化为 'unknown' → 不误报 (平台已兜底, 非越界)。"""
    setup = {
        "name": "x",
        "infra": "pytorch",
        "models": [{"id": "m1", "supported_prompts": ["none"]}],
    }
    warnings = extract_capabilities(setup)["warnings"]
    assert all(w["field"] != "task" for w in warnings)


def test_prompts_requires_input_matches_registry_contract():
    """SSOT 派生集合覆盖当前交互与纠错 prompt。"""
    expected = {
        "point",
        "interactive_box",
        "bbox",
        "text",
        "exemplar",
        "scribble",
        "sketch",
        "mask",
        "correction_frame",
    }
    assert set(PROMPTS_REQUIRES_INPUT) == expected


def test_prompts_interactive_route_excludes_text_includes_core():
    assert "text" not in PROMPTS_INTERACTIVE_ROUTE
    assert "correction_frame" not in PROMPTS_INTERACTIVE_ROUTE
    assert {"point", "interactive_box", "exemplar"} <= PROMPTS_INTERACTIVE_ROUTE
