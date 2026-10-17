"""v0.10.22 · Project 业务 helper.

tool_bindings JSONB 是类别 / 属性配置的唯一存储真值 (ADR-0026). 旧扁平列
classes / classes_config / attribute_schema 已于 v0.10.22 从 DB 删除; 下面的
derive_* 纯函数把 tool_bindings 拍平成扁平投影, 仅供**读时**使用 (响应序列化 /
导出 / 聚合) —— 不再回写任何列, 不构成第二份真值.
"""

from __future__ import annotations

from typing import Any


# v0.17.15 · alias_to 软关联解析最大跳数 (环 / 超深 backstop).
_ALIAS_RESOLVE_MAX_DEPTH = 4


def _find_class(tool_bindings: dict[str, Any], unit_id: Any, name: Any) -> dict | None:
    """在指定 unit 内按 name 找类条目; 不存在返回 None."""
    binding = tool_bindings.get(unit_id) if isinstance(unit_id, str) else None
    if not isinstance(binding, dict):
        return None
    for c in binding.get("classes") or []:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    return None


def resolve_class_visual(
    tool_bindings: dict[str, Any],
    cls: dict,
    *,
    _visited: set[tuple] | None = None,
    _depth: int = 0,
) -> tuple[str | None, str | None]:
    """v0.17.15 · 沿 alias_to 链解析一条类的有效 (color, alias).

    本类显式 color/alias 优先 (可选叠加); 缺失的那项沿链继承目标值.
    环 (visited) / 悬空 (目标不存在) / 超深 (>_ALIAS_RESOLVE_MAX_DEPTH) → 停在当前已知值降级.
    纯读时派生, 不改 tool_bindings.
    """
    color = cls.get("color")
    alias = cls.get("alias")
    if color and alias:
        return color, alias
    ref = cls.get("alias_to")
    if not isinstance(ref, dict) or _depth >= _ALIAS_RESOLVE_MAX_DEPTH:
        return color, alias
    key = (ref.get("tool_unit_id"), ref.get("class_name"))
    if None in key:
        return color, alias
    visited = _visited if _visited is not None else set()
    if key in visited:  # 环: 降级
        return color, alias
    visited.add(key)
    # 注: 按 (unit, name) 定位 target 时不校验目标 unit 的 enabled —— alias_to 是显式
    # 视觉继承指针, 语义为"引用该类定义的 color/alias", 与目标是否参与标注解耦; 故
    # enabled 类可继承自 disabled unit 同名类。这与 derive_classes_config 顶层只投影
    # enabled binding 的口径不同, 系当前有意取舍; 若改为严格隔离(继承也只认 enabled),
    # 需前后端(resolveClassVisual)同步加校验并补测试。
    target = _find_class(tool_bindings, key[0], key[1])
    if target is None:  # 悬空: 降级用自身值
        return color, alias
    t_color, t_alias = resolve_class_visual(
        tool_bindings, target, _visited=visited, _depth=_depth + 1
    )
    return (color or t_color), (alias or t_alias)


def derive_classes_config(
    tool_bindings: dict[str, Any] | None,
) -> dict[str, dict]:
    """
    把 tool_bindings 嵌套结构 union 合并成扁平 classes_config:
        { class_name: { color?, order?, alias? } }

    跨工具单位同名类按"最先出现的 enabled binding"为准 (强隔离下其它工具同名
    类不会被读到, 这是有意的: 扁平投影不区分工具, 仅作合并展示视图).
    color/alias 经 alias_to 软关联链解析后填入 (v0.17.15, 见 resolve_class_visual).
    """
    if not tool_bindings:
        return {}

    out: dict[str, dict] = {}
    for binding in tool_bindings.values():
        if not isinstance(binding, dict) or not binding.get("enabled"):
            continue
        for cls in binding.get("classes") or []:
            if not isinstance(cls, dict):
                continue
            name = cls.get("name")
            if not name or name in out:
                continue
            color, alias = resolve_class_visual(tool_bindings, cls)
            entry: dict = {}
            if color:
                entry["color"] = color
            if cls.get("order") is not None:
                entry["order"] = cls["order"]
            if alias:
                entry["alias"] = alias
            out[name] = entry
    return out


def derive_classes_list(
    tool_bindings: dict[str, Any] | None,
) -> list[str]:
    """扁平 classes (list[str]) 派生 — 按 order 升序, name 升序兜底."""
    if not tool_bindings:
        return []
    seen: dict[str, int | None] = {}
    for binding in tool_bindings.values():
        if not isinstance(binding, dict) or not binding.get("enabled"):
            continue
        for cls in binding.get("classes") or []:
            if not isinstance(cls, dict):
                continue
            name = cls.get("name")
            if not name or name in seen:
                continue
            seen[name] = cls.get("order")
    return sorted(seen.keys(), key=lambda n: (seen[n] is None, seen[n] or 0, n))


def derive_attribute_schema(
    tool_bindings: dict[str, Any] | None,
) -> dict:
    """
    attribute_schema 派生: union 所有 enabled binding 的 fields, key 重复时
    以最先出现为准. 读到的是合并视图; 工具维度精确语义由 tool_bindings 自身提供.
    """
    if not tool_bindings:
        return {"fields": []}

    seen_keys: set[str] = set()
    merged: list[dict] = []
    for binding in tool_bindings.values():
        if not isinstance(binding, dict) or not binding.get("enabled"):
            continue
        schema = binding.get("attribute_schema") or {}
        for field in schema.get("fields") or []:
            if not isinstance(field, dict):
                continue
            key = field.get("key")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(field)
    return {"fields": merged}


def derive_attribute_keys(tool_bindings: dict[str, Any] | None) -> set[str]:
    """Return user-defined annotation attribute keys from enabled tool bindings."""
    schema = derive_attribute_schema(tool_bindings)
    return {
        field["key"]
        for field in schema.get("fields") or []
        if isinstance(field, dict) and field.get("key")
    }


def is_system_attribute_key(key: str) -> bool:
    """System metadata stored in annotations.attributes, not user schema data."""
    return key.startswith("_")


def sanitize_annotation_attributes(
    attributes: dict[str, Any] | None,
    allowed_keys: set[str],
) -> dict[str, Any]:
    """Keep only current schema-backed user attributes for export payloads."""
    if not isinstance(attributes, dict):
        return {}
    return {key: value for key, value in attributes.items() if key in allowed_keys}


def orphan_user_attribute_keys(
    attributes: dict[str, Any] | None,
    allowed_keys: set[str],
) -> list[str]:
    """User attribute keys that no longer exist in the current schema."""
    if not isinstance(attributes, dict):
        return []
    return [
        key
        for key in attributes
        if key not in allowed_keys and not is_system_attribute_key(key)
    ]


def prune_orphan_user_attributes(
    attributes: dict[str, Any] | None,
    allowed_keys: set[str],
) -> dict[str, Any]:
    """Drop orphan user attributes while preserving system metadata keys."""
    if not isinstance(attributes, dict):
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key in allowed_keys or is_system_attribute_key(key)
    }


def derive_tool_unit_for_type_key(type_key: str | None) -> str:
    """v0.10.17 · 旧 type_key → 默认 tool_unit_id 推断 (与 migration 0072 同规则).

    image-seg → region; 其它 → bbox 占位.
    """
    return "region" if type_key == "image-seg" else "bbox"


def coalesce_legacy_into_tool_bindings(
    payload: dict[str, Any],
    existing_tool_bindings: dict[str, Any] | None,
    type_key: str | None,
) -> None:
    """v0.10.17 兼容层 · 旧客户端只传 classes / classes_config / attribute_schema 时,
    反向派生进 payload["tool_bindings"], 保证 tool_bindings 是单源真值.

    - 若 payload 显式包含 tool_bindings: 跳过 (新客户端直接走)
    - 若 payload 既无 tool_bindings 又无任何 legacy 字段改动: 跳过
    - 否则: 按 type_key 选默认 unit (bbox / region), 把扁平字段合并进该 unit, 其它 unit
      保留 existing_tool_bindings 中的值. classes_config 优先于 classes (前者携带颜色 / 排序);
      仅给 classes list[str] 时只重建 name + order.
    """
    # v0.10.17 · 空 dict 等价于"未给", 触发反向派生 (模板 fixture 写空 dict 默认值,
    # 创建项目时应走 legacy classes / classes_config 派生路径).
    if payload.get("tool_bindings"):
        return
    has_legacy = any(
        k in payload for k in ("classes", "classes_config", "attribute_schema")
    )
    if not has_legacy:
        return

    unit = derive_tool_unit_for_type_key(type_key)
    merged = dict(existing_tool_bindings or {})
    prev_unit = merged.get(unit) or {}
    prev_classes_list = list(prev_unit.get("classes") or [])

    cfg_map = payload.get("classes_config")
    classes_strs = payload.get("classes")

    # 决定 class 名字列表 (来源优先级: classes list[str] > classes_config keys >
    # existing unit classes); 颜色 / order / alias 从 cfg_map 配套字段查 (若有).
    names: list[str]
    if classes_strs is not None:
        names = list(classes_strs)
    elif cfg_map:  # 非空 dict
        names = list(cfg_map.keys())
    else:
        names = [c.get("name") for c in prev_classes_list if isinstance(c, dict)]

    classes_list: list[dict] = []
    for i, name in enumerate(names):
        if not name:
            continue
        entry: dict = {"name": name}
        cfg = (cfg_map or {}).get(name)
        if isinstance(cfg, dict):
            if cfg.get("color"):
                entry["color"] = cfg["color"]
            if cfg.get("order") is not None:
                entry["order"] = cfg["order"]
            if cfg.get("alias"):
                entry["alias"] = cfg["alias"]
        else:
            # 没 cfg 时, 尝试从 prev_classes_list 取颜色 / alias (PATCH attribute_schema
            # 时保留原配色; 但若用户显式给 classes 改名, 颜色按 name 匹配回查)
            for prev in prev_classes_list:
                if isinstance(prev, dict) and prev.get("name") == name:
                    if prev.get("color"):
                        entry["color"] = prev["color"]
                    if prev.get("order") is not None:
                        entry["order"] = prev["order"]
                    if prev.get("alias"):
                        entry["alias"] = prev["alias"]
                    break
        entry.setdefault("order", i)
        classes_list.append(entry)

    attribute_schema = payload.get("attribute_schema")
    if attribute_schema is None:
        attribute_schema = prev_unit.get("attribute_schema") or {"fields": []}

    merged[unit] = {
        "enabled": True,
        "classes": classes_list,
        "attribute_schema": attribute_schema,
    }
    payload["tool_bindings"] = merged


# v0.10.28 · 媒体维度 data_type ↔ 兼容 type_key 互推.
# data_type 只到 image/video/lidar 粒度, 无法区分 video-track vs video-mm;
# 派生 type_key 仅在新建项目只给 data_type 时兜底一个默认子类型, 保旧分流不破.
_DATA_TYPE_TO_LEGACY_TYPE_KEY = {
    "image": "image-det",
    "video": "video-track",
    "lidar": "lidar",
}


def data_type_from_type_key(type_key: str | None) -> str:
    """把 type_key 推导到媒体维度 data_type (与 alembic 0082 回填同规则)."""
    if type_key and type_key.startswith("video"):
        return "video"
    if type_key == "lidar":
        return "lidar"
    return "image"


def legacy_type_key_from_data_type(data_type: str | None) -> str:
    """新建项目只给 data_type 时, 派生一个兼容 type_key 默认值."""
    return _DATA_TYPE_TO_LEGACY_TYPE_KEY.get(data_type or "image", "image-det")


def assert_project_kind_consistent(type_key: str | None, data_type: str | None) -> None:
    """断言 type_key 与 data_type 在媒体维度一致, 不一致抛 422.

    前端用 `type_key === "lidar"` 入 3D Stage, 后端 manifest 用
    `data_type == "lidar"` 放行点云端点; 两侧落库不一致就会出现「前端进了
    3D 台, 后端拒提供点云 manifest」之类的撕裂. 创建 / 更新接口必经此关.

    其中一个缺失时不抛 (上游 cross-fill 会补), 两侧都给且不一致才抛.
    """
    if not type_key or not data_type:
        return
    derived = data_type_from_type_key(type_key)
    if derived != data_type:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail=(
                f"type_key={type_key!r} implies data_type={derived!r}, "
                f"got data_type={data_type!r} (媒体维度必须一致)"
            ),
        )


def resolve_project_kind(
    type_key: str | None,
    data_type: str | None,
    *,
    inherited_type_key: str | None = None,
    inherited_data_type: str | None = None,
) -> tuple[str, str]:
    """Resolve a create request's project kind before legacy class conversion."""
    type_key = type_key or None
    data_type = data_type or None
    if type_key and data_type:
        assert_project_kind_consistent(type_key, data_type)
    elif type_key:
        data_type = data_type_from_type_key(type_key)
    elif data_type:
        type_key = legacy_type_key_from_data_type(data_type)
    else:
        type_key = inherited_type_key or None
        data_type = inherited_data_type or None
        if type_key and data_type:
            assert_project_kind_consistent(type_key, data_type)
        elif type_key:
            data_type = data_type_from_type_key(type_key)
        elif data_type:
            type_key = legacy_type_key_from_data_type(data_type)
        else:
            data_type = "image"
            type_key = legacy_type_key_from_data_type(data_type)

    assert type_key is not None and data_type is not None
    return type_key, data_type


def lookup_classes_for_tool_unit(
    tool_bindings: dict[str, Any] | None,
    tool_unit_id: str,
) -> list[str]:
    """给定 tool_unit_id, 返回该工具单位下可用的类名集合 (空 list = 未配置, 放行)."""
    if not tool_bindings:
        return []
    binding = tool_bindings.get(tool_unit_id)
    if not isinstance(binding, dict) or not binding.get("enabled"):
        return []
    return [
        cls["name"]
        for cls in (binding.get("classes") or [])
        if isinstance(cls, dict) and cls.get("name")
    ]
