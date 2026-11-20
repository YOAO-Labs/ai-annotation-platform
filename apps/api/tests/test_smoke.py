"""项目更新请求的属性热键、阈值和渲染配置校验。"""

from __future__ import annotations

import pytest


def test_project_schema_validates_attribute_hotkey():
    """D.1 后端守卫：attribute_schema 中 hotkey 必须 1-9 + 仅 boolean/select + 全局唯一。"""
    from yoao_workspace.configuration_requests import ProjectUpdate
    from pydantic import ValidationError

    # ✅ valid：boolean + 数字 hotkey
    ok = ProjectUpdate.model_validate(
        {
            "attribute_schema": {
                "fields": [
                    {
                        "key": "occluded",
                        "label": "遮挡",
                        "type": "boolean",
                        "hotkey": "1",
                    },
                    {
                        "key": "ori",
                        "label": "朝向",
                        "type": "select",
                        "options": [{"value": "n", "label": "北"}],
                        "hotkey": "2",
                    },
                ],
            },
        }
    )
    assert ok.attribute_schema is not None

    # ❌ hotkey 重复
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(
            {
                "attribute_schema": {
                    "fields": [
                        {"key": "a", "label": "A", "type": "boolean", "hotkey": "1"},
                        {"key": "b", "label": "B", "type": "boolean", "hotkey": "1"},
                    ],
                },
            }
        )

    # ❌ hotkey 不是 1-9
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(
            {
                "attribute_schema": {
                    "fields": [
                        {"key": "a", "label": "A", "type": "boolean", "hotkey": "0"},
                    ],
                },
            }
        )

    # ❌ hotkey 用在 text 字段上（不支持）
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(
            {
                "attribute_schema": {
                    "fields": [
                        {"key": "note", "label": "备注", "type": "text", "hotkey": "1"},
                    ],
                },
            }
        )


def test_project_iou_threshold_range():
    """A.4：iou_dedup_threshold 必须在 [0.3, 0.95]。"""
    from yoao_workspace.configuration_requests import ProjectUpdate
    from pydantic import ValidationError

    assert (
        ProjectUpdate.model_validate({"iou_dedup_threshold": 0.7}).iou_dedup_threshold
        == 0.7
    )
    assert (
        ProjectUpdate.model_validate({"iou_dedup_threshold": 0.3}).iou_dedup_threshold
        == 0.3
    )

    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"iou_dedup_threshold": 0.2})
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"iou_dedup_threshold": 1.0})


def test_project_rendering_config_validation():
    """ProjectUpdate 接受 rendering_config，校验字段范围与 extra=forbid。"""
    from yoao_workspace.configuration_requests import ProjectUpdate
    from pydantic import ValidationError

    # 全字段合法
    pu = ProjectUpdate.model_validate(
        {
            "rendering_config": {
                "smoothImage": False,
                "cssImageFilter": "invert(1)",
                "controlPointsSize": 8,
                "snapToGrid": True,
                "box3dDefaultSize": [4.5, 2.0, 1.7],
                "propagateOverwrite": False,
                "trackerDefaultModel": "sam2_video",
            }
        }
    )
    assert pu.rendering_config is not None
    assert pu.rendering_config.smoothImage is False
    assert pu.rendering_config.controlPointsSize == 8
    assert pu.rendering_config.box3dDefaultSize == (4.5, 2.0, 1.7)
    assert pu.rendering_config.propagateOverwrite is False
    assert pu.rendering_config.trackerDefaultModel == "sam2_video"

    # 部分字段：未提供的字段 = None（前端按 None 视作「不覆盖」）
    pu2 = ProjectUpdate.model_validate({"rendering_config": {"smoothImage": True}})
    assert pu2.rendering_config is not None
    assert pu2.rendering_config.smoothImage is True
    assert pu2.rendering_config.cssImageFilter is None

    # 拒绝 extra key（防 typo）
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"rendering_config": {"bogus": 1}})

    # controlPointsSize 越界
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"rendering_config": {"controlPointsSize": 1}})
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"rendering_config": {"controlPointsSize": 21}})

    # cssImageFilter 超长
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(
            {"rendering_config": {"cssImageFilter": "x" * 300}}
        )

    # box3dDefaultSize 必须是三元正数
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"rendering_config": {"box3dDefaultSize": [4, 2]}})
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate(
            {"rendering_config": {"box3dDefaultSize": [4, 0, 1]}}
        )
