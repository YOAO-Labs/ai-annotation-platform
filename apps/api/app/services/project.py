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


def sanitize_annotation_attributes(
    attributes: dict[str, Any] | None,
    allowed_keys: set[str],
) -> dict[str, Any]:
    """Keep only current schema-backed user attributes for export payloads."""
    if not isinstance(attributes, dict):
        return {}
    return {key: value for key, value in attributes.items() if key in allowed_keys}
