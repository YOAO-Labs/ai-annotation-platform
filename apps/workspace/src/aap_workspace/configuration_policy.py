"""Pure configuration normalization, cloning, and template authorization policy."""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from typing import Any
from uuid import UUID


class ConfigurationPolicyError(Exception):
    def __init__(
        self, status_code: int, detail: Any, headers: dict[str, str] | None = None
    ):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


CLONEABLE_PROJECT_FIELDS: tuple[str, ...] = (
    "type_label",
    "type_key",
    "data_type",
    "tool_bindings",
    "ai_enabled",
    "label_config",
    "sampling",
    "maximum_annotations",
    "show_overlap_first",
    "iou_dedup_threshold",
    "box_threshold",
    "text_threshold",
    "rendering_config",
)
TEMPLATE_PAYLOAD_FIELDS = CLONEABLE_PROJECT_FIELDS
_DATA_TYPE_TO_LEGACY_TYPE_KEY = {
    "image": "image-det",
    "video": "video-track",
    "lidar": "lidar",
}
_LIDAR_DATA_TYPES = ("lidar", "point_cloud", "pointcloud")


def merge_from_source(
    payload: dict[str, Any], source: Mapping[str, Any]
) -> dict[str, Any]:
    for field in CLONEABLE_PROJECT_FIELDS:
        if field in payload or field not in source:
            continue
        value = source[field]
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = copy.deepcopy(value)
        payload[field] = value
    return payload


def derive_tool_unit_for_type_key(type_key: str | None) -> str:
    """旧 type_key → 默认 tool_unit_id 推断 (与 migration 0072 同规则)."""
    return "region" if type_key == "image-seg" else "bbox"


def coalesce_legacy_into_tool_bindings(
    payload: dict[str, Any],
    existing_tool_bindings: Mapping[str, Any] | None,
    type_key: str | None,
) -> None:
    """Normalize legacy flat classes/config fields into the selected tool binding."""
    if payload.get("tool_bindings"):
        return
    if not any(
        field in payload for field in ("classes", "classes_config", "attribute_schema")
    ):
        return

    unit = derive_tool_unit_for_type_key(type_key)
    merged = dict(existing_tool_bindings or {})
    previous = merged.get(unit) or {}
    previous_classes = list(previous.get("classes") or [])
    config = payload.get("classes_config")
    class_names = payload.get("classes")
    if class_names is not None:
        names = list(class_names)
    elif config:
        names = list(config.keys())
    else:
        names = [
            item.get("name") for item in previous_classes if isinstance(item, dict)
        ]

    classes: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        if not name:
            continue
        item: dict[str, Any] = {"name": name}
        class_config = (config or {}).get(name)
        if isinstance(class_config, dict):
            if class_config.get("color"):
                item["color"] = class_config["color"]
            if class_config.get("order") is not None:
                item["order"] = class_config["order"]
            if class_config.get("alias"):
                item["alias"] = class_config["alias"]
        else:
            for previous_class in previous_classes:
                if (
                    isinstance(previous_class, dict)
                    and previous_class.get("name") == name
                ):
                    for field in ("color", "alias"):
                        if previous_class.get(field):
                            item[field] = previous_class[field]
                    if previous_class.get("order") is not None:
                        item["order"] = previous_class["order"]
                    break
        item.setdefault("order", index)
        classes.append(item)

    attribute_schema = payload.get("attribute_schema")
    if attribute_schema is None:
        attribute_schema = previous.get("attribute_schema") or {"fields": []}
    merged[unit] = {
        "enabled": True,
        "classes": classes,
        "attribute_schema": attribute_schema,
    }
    payload["tool_bindings"] = merged


def data_type_from_type_key(type_key: str | None) -> str:
    if type_key and type_key.startswith("video"):
        return "video"
    if type_key == "lidar":
        return "lidar"
    return "image"


def legacy_type_key_from_data_type(data_type: str | None) -> str:
    return _DATA_TYPE_TO_LEGACY_TYPE_KEY.get(data_type or "image", "image-det")


def assert_project_kind_consistent(type_key: str | None, data_type: str | None) -> None:
    if not type_key or not data_type:
        return
    derived = data_type_from_type_key(type_key)
    if derived != data_type:
        raise ConfigurationPolicyError(
            422,
            f"type_key={type_key!r} implies data_type={derived!r}, "
            f"got data_type={data_type!r} (媒体维度必须一致)",
        )


def resolve_project_kind(
    type_key: str | None,
    data_type: str | None,
    *,
    inherited_type_key: str | None = None,
    inherited_data_type: str | None = None,
) -> tuple[str, str]:
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


def canonical_media_kind(data_type: str | None) -> str:
    if data_type in _LIDAR_DATA_TYPES:
        return "lidar"
    return data_type or "image"


def scene_mode_allowed(data_type: str | None) -> bool:
    return canonical_media_kind(data_type) in {"image", "lidar"}


def prepare_project_create_payload(
    payload: dict[str, Any],
    *,
    inherited_type_key: str | None = None,
    inherited_data_type: str | None = None,
) -> None:
    payload["type_key"], payload["data_type"] = resolve_project_kind(
        payload.get("type_key"),
        payload.get("data_type"),
        inherited_type_key=inherited_type_key,
        inherited_data_type=inherited_data_type,
    )
    if payload.get("scene_mode"):
        if not scene_mode_allowed(payload.get("data_type")):
            raise ConfigurationPolicyError(422, "scene_mode 仅支持 image/lidar 项目")
        payload.setdefault("prefer_same_scene_continuation", True)
    coalesce_legacy_into_tool_bindings(
        payload, existing_tool_bindings=None, type_key=payload.get("type_key")
    )
    for legacy_field in ("classes", "classes_config", "attribute_schema"):
        payload.pop(legacy_field, None)


def lookup_classes_for_tool_unit(
    tool_bindings: Mapping[str, Any] | None,
    tool_unit_id: str,
) -> list[str]:
    if not tool_bindings:
        return []
    binding = tool_bindings.get(tool_unit_id)
    if not isinstance(binding, Mapping) or not binding.get("enabled"):
        return []
    return [
        item["name"]
        for item in (binding.get("classes") or [])
        if isinstance(item, Mapping) and item.get("name")
    ]


def dump_project_to_template_payload(project: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in TEMPLATE_PAYLOAD_FIELDS:
        if field not in project:
            continue
        value = project[field]
        if isinstance(value, (dict, list)):
            value = copy.deepcopy(value)
        payload[field] = value
    if project.get("annotation_guide") is not None:
        payload["annotation_guide"] = project["annotation_guide"]
    return payload


def merge_template_into_payload(
    template: Mapping[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    for field in TEMPLATE_PAYLOAD_FIELDS:
        if field in payload or field not in template:
            continue
        value = template[field]
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = copy.deepcopy(value)
        payload[field] = value
    if (
        "annotation_guide" not in payload
        and template.get("annotation_guide") is not None
    ):
        payload["annotation_guide"] = template["annotation_guide"]
    return payload


def _actor_id(actor: Mapping[str, Any]) -> Any:
    return actor.get("id")


def _actor_role(actor: Mapping[str, Any]) -> Any:
    return actor.get("role")


def assert_template_visible(
    template: Mapping[str, Any],
    actor: Mapping[str, Any],
    organization_ids: Collection[UUID],
) -> None:
    if _actor_role(actor) == "super_admin":
        return
    if template.get("scope") == "public":
        return
    if template.get("scope") == "private":
        if template.get("created_by") == _actor_id(actor):
            return
    elif template.get("scope") == "organization":
        organization_id = template.get("organization_id")
        if organization_id is None:
            return
        if organization_id in organization_ids:
            return
    raise ConfigurationPolicyError(404, "模板不存在")


def can_edit_template(template: Mapping[str, Any], actor: Mapping[str, Any]) -> bool:
    if _actor_role(actor) == "super_admin":
        return True
    return template.get("created_by") == _actor_id(actor)


def assert_can_create_scope(scope: str, actor: Mapping[str, Any]) -> None:
    if scope == "public" and _actor_role(actor) != "super_admin":
        raise ConfigurationPolicyError(403, "仅超级管理员可创建公共模板")


def assert_template_creation_scope(
    scope: str, organization_id: UUID | None, actor: Mapping[str, Any]
) -> None:
    assert_can_create_scope(scope, actor)
    if scope == "organization" and organization_id is None:
        raise ConfigurationPolicyError(
            400, "organization scope 必须指定 organization_id"
        )
