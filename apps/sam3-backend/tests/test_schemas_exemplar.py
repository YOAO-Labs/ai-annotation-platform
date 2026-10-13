"""协议 schemas 中 exemplar 类型的校验单测.

确认 Context.type='exemplar' 必须带 bbox=[x1,y1,x2,y2], 否则 pydantic 校验失败.
apps/api 路由层会再做项目挂载校验; 这层是 backend 自身的入口防御.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas import Context, Exemplar


def test_exemplar_requires_bbox():
    with pytest.raises(ValidationError) as exc:
        Context(type="exemplar")
    assert "context.exemplars" in str(exc.value) or "context.bbox" in str(exc.value)


def test_exemplar_requires_bbox_length_4():
    with pytest.raises(ValidationError):
        Context(type="exemplar", bbox=[0.1, 0.2, 0.3])


def test_exemplar_with_valid_bbox():
    ctx = Context(type="exemplar", bbox=[0.2, 0.2, 0.45, 0.55])
    assert ctx.type == "exemplar"
    assert ctx.bbox == [0.2, 0.2, 0.45, 0.55]


def test_text_still_works_without_bbox():
    """exemplar 的强校验不应影响其他 prompt 类型."""
    ctx = Context(type="text", text="person")
    assert ctx.type == "text"
    assert ctx.bbox is None


def test_point_requires_points():
    """point 走 inst 单实例交互, 必须带 points."""
    ctx = Context(type="point", points=[[0.5, 0.5]], labels=[1])
    assert ctx.type == "point"
    with pytest.raises(ValidationError) as exc:
        Context(type="point")
    assert "context.points" in str(exc.value)


def test_interactive_box_requires_bbox():
    """interactive_box (单框单 mask) 必须带 bbox=[x1,y1,x2,y2]."""
    ctx = Context(type="interactive_box", bbox=[0.1, 0.1, 0.4, 0.4])
    assert ctx.type == "interactive_box"
    with pytest.raises(ValidationError) as exc:
        Context(type="interactive_box")
    assert "context.bbox" in str(exc.value)


def test_supported_types():
    """supported_prompts: point / interactive_box / polygon / text / exemplar."""
    Context(type="polygon")  # 无额外校验
    Context(type="text")
    Context(type="point", points=[[0.5, 0.5]], labels=[1])
    Context(type="interactive_box", bbox=[0, 0, 1, 1])
    Context(type="exemplar", bbox=[0, 0, 1, 1])


def test_bbox_prompt_retired():
    """type=bbox 已退出交互 prompt 命名空间, 落 ValidationError."""
    with pytest.raises(ValidationError):
        Context(type="bbox", bbox=[0, 0, 1, 1])  # type: ignore[arg-type]


def test_invalid_type_rejected():
    with pytest.raises(ValidationError):
        Context(type="video_tracker")  # type: ignore[arg-type]  # sam3-backend 不接 video


def test_multimask_output_field():
    """point / interactive_box 候选开关, 缺省 False."""
    assert Context(type="point", points=[[0.5, 0.5]]).multimask_output is False
    ctx = Context(type="point", points=[[0.5, 0.5]], multimask_output=True)
    assert ctx.multimask_output is True


def test_native_mask_output_requires_prompt_revision():
    with pytest.raises(ValidationError, match="prompt_revision"):
        Context(
            type="point",
            points=[[0.5, 0.5]],
            output_geometry="mask",
        )
    context = Context(
        type="point",
        points=[[0.5, 0.5]],
        output_geometry="mask",
        prompt_revision="revision-1",
    )
    assert context.output_geometry == "mask"


def test_score_threshold_field_present():
    """score_threshold (text / exemplar 路径)."""
    ctx = Context(type="text", text="cat", score_threshold=0.7)
    assert ctx.score_threshold == 0.7


# ---------- 多正负框 exemplars ----------


def test_exemplar_with_multi_box_exemplars():
    """exemplars[] 多正负框累加; 缺省 label 视为正框。"""
    ctx = Context(
        type="exemplar",
        exemplars=[
            {"bbox": [0.1, 0.1, 0.2, 0.2], "label": True},
            {"bbox": [0.5, 0.5, 0.6, 0.6], "label": False},
            {"bbox": [0.7, 0.7, 0.8, 0.8]},
        ],
    )
    assert ctx.exemplars is not None and len(ctx.exemplars) == 3
    assert ctx.exemplars[0].label is True
    assert ctx.exemplars[1].label is False
    assert ctx.exemplars[2].label is True  # 缺省正框


def test_exemplar_item_bbox_length_validated():
    with pytest.raises(ValidationError):
        Context(type="exemplar", exemplars=[{"bbox": [0.1, 0.2, 0.3]}])


def test_exemplar_with_text_combination():
    """text 概念 + exemplars 几何框可同时传 (组合)。"""
    ctx = Context(
        type="exemplar",
        text="car",
        exemplars=[{"bbox": [0.1, 0.1, 0.3, 0.3], "label": False}],
    )
    assert ctx.text == "car"
    assert ctx.exemplars is not None and len(ctx.exemplars) == 1


def test_exemplar_single_bbox_still_valid_without_exemplars():
    """旧单框路径 (无 exemplars, 带 bbox) 回归不破。"""
    ctx = Context(type="exemplar", bbox=[0.2, 0.2, 0.45, 0.55])
    assert ctx.exemplars is None
    assert ctx.bbox == [0.2, 0.2, 0.45, 0.55]


def test_exemplar_model_direct():
    ex = Exemplar(bbox=[0.0, 0.0, 1.0, 1.0])
    assert ex.label is True
    with pytest.raises(ValidationError):
        Exemplar(bbox=[0.0, 0.0])
