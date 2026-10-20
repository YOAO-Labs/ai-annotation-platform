"""v0.5.5 phase 2 · A.2：sanity 测试。

目的：守住「app 能启动 + 关键 schema 能校验通过」这条最低基线。
后续在此基础上扩展：audit export filter / 用户角色矩阵 / 权限守卫等。
"""

from __future__ import annotations

import pytest


def test_app_imports(app_module):
    """app 顺利 import → 所有 router / schema / depends 启动期无错。"""
    assert app_module is not None
    # 至少注册了若干 router（不同版本下数量会变，做个下限）
    routes = [r.path for r in app_module.routes]
    assert any("/users" in p for p in routes)
    assert any("/audit-logs" in p for p in routes)
    assert any("/projects" in p for p in routes)


def test_project_schema_validates_attribute_hotkey():
    """D.1 后端守卫：attribute_schema 中 hotkey 必须 1-9 + 仅 boolean/select + 全局唯一。"""
    from aap_workspace.configuration_requests import ProjectUpdate
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
    from aap_workspace.configuration_requests import ProjectUpdate
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


def test_project_rendering_config_v0_10_10():
    """v0.10.10 · I17.3 · ProjectUpdate 接受 rendering_config，校验字段范围与 extra=forbid。"""
    from aap_workspace.configuration_requests import ProjectUpdate
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


def test_audit_query_supports_detail_filter():
    """A.3：_build_base_query 支持 detail_key + detail_value 入参（不抛异常）。"""
    from app.api.v1.audit_logs import _build_base_query

    base, count = _build_base_query(
        action=None,
        target_type=None,
        target_id=None,
        actor_id=None,
        from_=None,
        to=None,
        detail_key="role",
        detail_value="super_admin",
    )
    assert base is not None
    assert count is not None


def test_pending_task_statuses_present():
    """B.2：删除前 pending task 检查使用的状态枚举值与 TaskStatus 对齐。"""
    from app.api.v1.users import _PENDING_TASK_STATUSES
    from app.db.enums import TaskStatus

    valid = {ts.value for ts in TaskStatus}
    for s in _PENDING_TASK_STATUSES:
        assert s in valid, f"_PENDING_TASK_STATUSES 中的 {s!r} 不在 TaskStatus 中"
