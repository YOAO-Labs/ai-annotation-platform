"""tool_bindings helper tests.

覆盖 aap_workspace_contract.projections 的 derive_* (读时派生投影) /
aap_workspace.configuration_policy 的配置归一化与工具单位查询 /
services/prediction.py 的 derive_tool_unit_from_ls_type / derive_tool_unit_from_result.

这些 helper 是 tool_bindings 单源真值的派生 / 反向派生 / 工具单位查表核心,
任何回归都会让导出 reader / 新 wizard / accept_prediction 至少一个分支错位.
"""

from __future__ import annotations

import pytest

from app.services.prediction import (
    derive_tool_unit_from_ls_type,
    derive_tool_unit_from_result,
)
from aap_workspace_contract import (
    derive_attribute_schema,
    derive_classes_config,
    derive_classes_list,
    resolve_class_visual,
)
from aap_workspace.configuration_policy import (
    coalesce_legacy_into_tool_bindings,
    derive_tool_unit_for_type_key,
    lookup_classes_for_tool_unit,
)


# ── derive_tool_unit_from_ls_type / _from_result ───────────────────────────


@pytest.mark.parametrize(
    "ls_type, expected",
    [
        ("rectanglelabels", "bbox"),
        ("polylinelabels", "polyline"),
        ("polygonlabels", "region"),
        ("brushlabels", "region"),
        ("multi_polygon", "region"),
        ("keypointlabels", "keypoint"),
        ("unknown_kind", "bbox"),
        (None, "bbox"),
    ],
)
def test_derive_tool_unit_from_ls_type(ls_type, expected):
    assert derive_tool_unit_from_ls_type(ls_type) == expected


def test_derive_tool_unit_from_rotated_rectangle_value():
    assert (
        derive_tool_unit_from_ls_type("rectanglelabels", {"rotation": 0})
        == "rotated_bbox"
    )


def test_derive_tool_unit_from_result_empty_or_invalid():
    assert derive_tool_unit_from_result(None) == "bbox"
    assert derive_tool_unit_from_result([]) == "bbox"
    assert derive_tool_unit_from_result(["not-a-dict"]) == "bbox"


def test_derive_tool_unit_from_result_picks_first():
    result = [
        {"type": "polygonlabels", "value": {}},
        {"type": "rectanglelabels", "value": {}},
    ]
    assert derive_tool_unit_from_result(result) == "region"


def test_derive_tool_unit_from_result_uses_shape_value():
    result = [{"type": "rectanglelabels", "value": {"rotation": 30}}]
    assert derive_tool_unit_from_result(result) == "rotated_bbox"


# ── derive_tool_unit_for_type_key ──────────────────────────────────────────


@pytest.mark.parametrize(
    "type_key, expected",
    [
        ("image-seg", "region"),
        ("image-det", "bbox"),
        ("video-track", "bbox"),
        ("image-kp", "bbox"),
        ("lidar", "bbox"),
        (None, "bbox"),
        ("", "bbox"),
    ],
)
def test_derive_tool_unit_for_type_key(type_key, expected):
    assert derive_tool_unit_for_type_key(type_key) == expected


# ── derive_classes_config ───────────────────────────────────────────


def test_derive_classes_config_empty():
    assert derive_classes_config(None) == {}
    assert derive_classes_config({}) == {}


def test_derive_classes_config_skips_disabled_binding():
    tb = {
        "bbox": {
            "enabled": False,
            "classes": [{"name": "ignored", "color": "#fff"}],
        },
        "region": {
            "enabled": True,
            "classes": [{"name": "kept", "color": "#000"}],
        },
    }
    out = derive_classes_config(tb)
    assert "ignored" not in out
    assert out["kept"] == {"color": "#000"}


def test_derive_classes_config_first_occurrence_wins():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "person", "color": "#aaa", "alias": "p"}],
        },
        "region": {
            "enabled": True,
            "classes": [{"name": "person", "color": "#bbb", "alias": "x"}],
        },
    }
    out = derive_classes_config(tb)
    # 同名类强隔离 union: 取最先出现的 binding 配色 / alias.
    assert out["person"]["color"] == "#aaa"
    assert out["person"]["alias"] == "p"


def test_derive_classes_config_drops_empty_fields():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "x", "color": None, "alias": "", "order": 0},
                {"name": "y", "color": "#fff"},
            ],
        }
    }
    out = derive_classes_config(tb)
    assert "color" not in out["x"]
    assert "alias" not in out["x"]
    assert out["x"]["order"] == 0
    assert out["y"] == {"color": "#fff"}


# ── resolve_class_visual / alias_to 软关联 (v0.17.15) ──────────────────


def _alias_tb():
    """bbox.person 显式红 + alias; region.pedestrian 空值 alias_to bbox.person."""
    return {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "person", "color": "#ff0000", "alias": "person"}],
        },
        "region": {
            "enabled": True,
            "classes": [
                {
                    "name": "pedestrian",
                    "alias_to": {"tool_unit_id": "bbox", "class_name": "person"},
                }
            ],
        },
    }


def test_resolve_inherits_color_and_alias_from_target():
    tb = _alias_tb()
    cls = tb["region"]["classes"][0]
    color, alias = resolve_class_visual(tb, cls)
    assert color == "#ff0000"
    assert alias == "person"


def test_resolve_explicit_value_overrides_inheritance():
    tb = _alias_tb()
    # 本类显式蓝色 → 覆盖继承; alias 仍空 → 继承.
    tb["region"]["classes"][0]["color"] = "#0000ff"
    color, alias = resolve_class_visual(tb, tb["region"]["classes"][0])
    assert color == "#0000ff"
    assert alias == "person"


def test_resolve_dangling_target_falls_back_to_own():
    tb = _alias_tb()
    # 目标类不存在 → 降级用自身值 (此处自身 color 空).
    tb["region"]["classes"][0]["alias_to"] = {
        "tool_unit_id": "bbox",
        "class_name": "ghost",
    }
    tb["region"]["classes"][0]["color"] = "#123456"
    color, alias = resolve_class_visual(tb, tb["region"]["classes"][0])
    assert color == "#123456"
    assert alias is None


def test_resolve_cycle_terminates():
    # A→B→A 环: 不死循环, 各自降级到自身已知值.
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [
                {
                    "name": "a",
                    "color": "#aaaaaa",
                    "alias_to": {"tool_unit_id": "region", "class_name": "b"},
                }
            ],
        },
        "region": {
            "enabled": True,
            "classes": [
                {
                    "name": "b",
                    "alias_to": {"tool_unit_id": "bbox", "class_name": "a"},
                }
            ],
        },
    }
    color, alias = resolve_class_visual(tb, tb["region"]["classes"][0])
    assert color == "#aaaaaa"  # b 继承 a 的显式色; a 回指 b 时遇环停住
    assert alias is None


def test_resolve_multi_hop_chain():
    # c → b → a, a 有显式色; c 应跨两跳继承.
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "a", "color": "#0a0a0a"}],
        },
        "region": {
            "enabled": True,
            "classes": [
                {
                    "name": "b",
                    "alias_to": {"tool_unit_id": "bbox", "class_name": "a"},
                }
            ],
        },
        "polyline": {
            "enabled": True,
            "classes": [
                {
                    "name": "c",
                    "alias_to": {"tool_unit_id": "region", "class_name": "b"},
                }
            ],
        },
    }
    color, _ = resolve_class_visual(tb, tb["polyline"]["classes"][0])
    assert color == "#0a0a0a"


def test_derive_classes_config_resolves_alias_to():
    # 扁平视图里, 不同名继承类应带上继承后的 color/alias.
    out = derive_classes_config(_alias_tb())
    assert out["person"]["color"] == "#ff0000"
    assert out["pedestrian"]["color"] == "#ff0000"
    assert out["pedestrian"]["alias"] == "person"


# ── derive_classes_list ─────────────────────────────────────────────


def test_derive_classes_list_sorts_by_order():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "b", "order": 2},
                {"name": "a", "order": 0},
                {"name": "c", "order": 1},
            ],
        }
    }
    assert derive_classes_list(tb) == ["a", "c", "b"]


def test_derive_classes_list_missing_order_pushed_to_end():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "no_order"},
                {"name": "ordered", "order": 5},
            ],
        }
    }
    out = derive_classes_list(tb)
    assert out == ["ordered", "no_order"]


def test_derive_classes_list_skips_dup_across_units():
    tb = {
        "bbox": {"enabled": True, "classes": [{"name": "person", "order": 0}]},
        "region": {
            "enabled": True,
            "classes": [{"name": "person", "order": 9}],
        },
    }
    out = derive_classes_list(tb)
    assert out == ["person"]


# ── derive_attribute_schema ─────────────────────────────────────────


def test_derive_attribute_schema_union_first_wins():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [],
            "attribute_schema": {
                "fields": [
                    {"key": "occluded", "type": "boolean"},
                ]
            },
        },
        "region": {
            "enabled": True,
            "classes": [],
            "attribute_schema": {
                "fields": [
                    {"key": "occluded", "type": "select"},  # 重复 key 被丢弃
                    {"key": "color", "type": "string"},
                ]
            },
        },
    }
    out = derive_attribute_schema(tb)
    keys = [f["key"] for f in out["fields"]]
    assert keys == ["occluded", "color"]
    # 首次出现的版本保留
    assert out["fields"][0]["type"] == "boolean"


def test_derive_attribute_schema_empty_input():
    assert derive_attribute_schema(None) == {"fields": []}
    assert derive_attribute_schema({}) == {"fields": []}


# ── lookup_classes_for_tool_unit ───────────────────────────────────────────


def test_lookup_classes_for_tool_unit_disabled_returns_empty():
    tb = {
        "bbox": {
            "enabled": False,
            "classes": [{"name": "x"}],
        }
    }
    assert lookup_classes_for_tool_unit(tb, "bbox") == []


def test_lookup_classes_for_tool_unit_returns_names():
    tb = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "a"}, {"name": "b"}],
        },
        "region": {
            "enabled": True,
            "classes": [{"name": "c"}],
        },
    }
    assert lookup_classes_for_tool_unit(tb, "bbox") == ["a", "b"]
    assert lookup_classes_for_tool_unit(tb, "region") == ["c"]


def test_lookup_classes_for_tool_unit_missing_unit():
    tb = {"bbox": {"enabled": True, "classes": []}}
    assert lookup_classes_for_tool_unit(tb, "region") == []
    assert lookup_classes_for_tool_unit(None, "bbox") == []


# ── coalesce_legacy_into_tool_bindings ─────────────────────────────────────


def test_coalesce_skips_when_tool_bindings_already_set():
    payload = {
        "tool_bindings": {
            "bbox": {"enabled": True, "classes": [{"name": "preserved"}]}
        },
        "classes": ["should_not_overwrite"],
    }
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    assert payload["tool_bindings"]["bbox"]["classes"][0]["name"] == "preserved"


def test_coalesce_skips_when_no_legacy_keys():
    payload = {"name": "x"}
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    assert "tool_bindings" not in payload


def test_coalesce_empty_dict_tool_bindings_is_treated_as_unset():
    # 模板 fixture 默认 tool_bindings={}, 应触发反向派生.
    payload = {"tool_bindings": {}, "classes": ["car"]}
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    assert payload["tool_bindings"]["bbox"]["classes"] == [{"name": "car", "order": 0}]


def test_coalesce_classes_list_priority_over_cfg_map():
    payload = {
        "classes": ["explicit"],
        "classes_config": {"old_key": {"color": "#aaa"}},
    }
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    names = [c["name"] for c in payload["tool_bindings"]["bbox"]["classes"]]
    assert names == ["explicit"]


def test_coalesce_cfg_map_supplies_color_for_class_name():
    payload = {
        "classes": ["car"],
        "classes_config": {"car": {"color": "#fff", "alias": "auto"}},
    }
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    car = payload["tool_bindings"]["bbox"]["classes"][0]
    assert car["color"] == "#fff"
    assert car["alias"] == "auto"


def test_coalesce_image_seg_picks_region_unit():
    payload = {"classes": ["road"]}
    coalesce_legacy_into_tool_bindings(payload, None, "image-seg")
    assert "region" in payload["tool_bindings"]
    assert "bbox" not in payload["tool_bindings"]


def test_coalesce_preserves_other_units_from_existing():
    existing = {
        "keypoint": {
            "enabled": True,
            "classes": [{"name": "kp_kept", "order": 0}],
        }
    }
    payload = {"classes": ["car"]}
    coalesce_legacy_into_tool_bindings(payload, existing, "image-det")
    # bbox 被反向派生; 其它已配置单位 (keypoint) 保留.
    assert payload["tool_bindings"]["bbox"]["classes"][0]["name"] == "car"
    assert payload["tool_bindings"]["keypoint"]["classes"][0]["name"] == "kp_kept"


def test_coalesce_backfill_color_from_prev_when_cfg_missing():
    """用户 PATCH attribute_schema 但没改 classes 时, 应保留原 bbox 配色."""
    existing = {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "color": "#abc", "order": 0}],
        }
    }
    payload = {
        "classes": ["car"],
        "attribute_schema": {"fields": []},
    }
    coalesce_legacy_into_tool_bindings(payload, existing, "image-det")
    car = payload["tool_bindings"]["bbox"]["classes"][0]
    assert car["color"] == "#abc"


def test_coalesce_empty_class_name_skipped():
    payload = {"classes": ["", "valid"]}
    coalesce_legacy_into_tool_bindings(payload, None, "image-det")
    names = [c["name"] for c in payload["tool_bindings"]["bbox"]["classes"]]
    assert names == ["valid"]
