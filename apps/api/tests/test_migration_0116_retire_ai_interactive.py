"""0116 迁移的 tool_bindings 合并纯函数单测。

merge_ai_interactive_binding 把退役的 ai_interactive 单位折叠进 region/bbox。因为 dev 库
已无任何含该 key 的项目 (迁移在 dev 上是空跑), 合并正确性只能靠这里的纯函数单测兜住:
- 同名 class 冲突时保留目标单位的、跳过来源的;
- classes / attributes 折叠进已存在的 region+bbox (共享调色板);
- 目标单位都不存在时回落到 disabled bbox;
- 无 ai_interactive key 时原样返回。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_0116():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0116_retire_ai_interactive_tool_unit.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0116", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_merge_keeps_target_unit_class_on_name_conflict():
    mod = _load_migration_0116()
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "color": "#0ea5e9", "order": 0}],
            "attribute_schema": {"fields": []},
        },
        "region": {
            "enabled": True,
            "classes": [{"name": "car", "color": "#22c55e", "order": 0}],
            "attribute_schema": {"fields": []},
        },
        "ai_interactive": {
            "enabled": True,
            "classes": [
                {"name": "car", "color": "#a855f7", "order": 0},
                {"name": "prompt", "color": "#a855f7", "order": 1},
            ],
            "attribute_schema": {
                "fields": [{"key": "note", "type": "text", "label": "Note"}]
            },
        },
    }
    out = mod.merge_ai_interactive_binding(tb)

    assert "ai_interactive" not in out
    bbox_by_name = {c["name"]: c["color"] for c in out["bbox"]["classes"]}
    assert bbox_by_name == {"car": "#0ea5e9", "prompt": "#a855f7"}
    region_by_name = {c["name"]: c["color"] for c in out["region"]["classes"]}
    assert region_by_name == {"car": "#22c55e", "prompt": "#a855f7"}
    assert [f["key"] for f in out["bbox"]["attribute_schema"]["fields"]] == ["note"]
    assert [f["key"] for f in out["region"]["attribute_schema"]["fields"]] == ["note"]


def test_merge_attribute_key_conflict_keeps_target():
    mod = _load_migration_0116()
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [],
            "attribute_schema": {
                "fields": [{"key": "occluded", "type": "boolean", "label": "kept"}]
            },
        },
        "ai_interactive": {
            "enabled": True,
            "classes": [],
            "attribute_schema": {
                "fields": [{"key": "occluded", "type": "select", "label": "dropped"}]
            },
        },
    }
    out = mod.merge_ai_interactive_binding(tb)
    fields = out["bbox"]["attribute_schema"]["fields"]
    assert len(fields) == 1
    assert fields[0]["label"] == "kept"


def test_merge_fallback_bbox_when_no_geometry_unit():
    mod = _load_migration_0116()
    tb = {
        "ai_interactive": {
            "enabled": True,
            "classes": [{"name": "prompt", "order": 0}],
            "attribute_schema": {"fields": []},
        }
    }
    out = mod.merge_ai_interactive_binding(tb)
    assert "ai_interactive" not in out
    assert out["bbox"]["enabled"] is False
    assert [c["name"] for c in out["bbox"]["classes"]] == ["prompt"]


def test_merge_noop_without_ai_interactive_key():
    mod = _load_migration_0116()
    tb = {"bbox": {"enabled": True, "classes": [{"name": "car"}]}}
    out = mod.merge_ai_interactive_binding(tb)
    assert out is tb


def test_merge_does_not_mutate_input():
    mod = _load_migration_0116()
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car"}],
            "attribute_schema": {"fields": []},
        },
        "ai_interactive": {
            "enabled": True,
            "classes": [{"name": "prompt"}],
            "attribute_schema": {"fields": []},
        },
    }
    mod.merge_ai_interactive_binding(tb)
    assert [c["name"] for c in tb["bbox"]["classes"]] == ["car"]
    assert "ai_interactive" in tb
