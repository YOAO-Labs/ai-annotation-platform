"""Pure class-name transformation for Project tool bindings."""

from __future__ import annotations

from .configuration_policy import ConfigurationPolicyError


def rename_class_bindings(
    tool_bindings: dict | None,
    *,
    old_name: str,
    new_name: str,
    tool_unit_id: str | None,
) -> dict:
    """Return a shallow-copied binding map with the selected classes renamed."""

    updated_bindings = dict(tool_bindings or {})
    if tool_unit_id is not None:
        if tool_unit_id not in updated_bindings:
            raise ConfigurationPolicyError(404, f"tool_unit_id '{tool_unit_id}' 不存在")
        target_units = [tool_unit_id]
    else:
        target_units = list(updated_bindings.keys())

    found_any = False
    for unit_id in target_units:
        binding = updated_bindings.get(unit_id)
        if not isinstance(binding, dict):
            continue
        classes_list = list(binding.get("classes") or [])
        names = [item.get("name") for item in classes_list if isinstance(item, dict)]
        if old_name not in names:
            continue
        if new_name in names:
            raise ConfigurationPolicyError(
                409, f"类别 '{new_name}' 在工具单位 '{unit_id}' 已存在"
            )
        renamed = [
            {**item, "name": new_name}
            if isinstance(item, dict) and item.get("name") == old_name
            else item
            for item in classes_list
        ]
        updated_bindings[unit_id] = {**binding, "classes": renamed}
        found_any = True

    if not found_any:
        raise ConfigurationPolicyError(404, f"类别 '{old_name}' 不存在")

    return updated_bindings
