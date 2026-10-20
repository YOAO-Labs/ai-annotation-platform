"""Typed column encoding for the fixed Workspace configuration write surface."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

ResourceKind = Literal["project", "template"]

PROJECT_CONFIG_COLUMNS = frozenset(
    {
        "name",
        "type_label",
        "type_key",
        "data_type",
        "status",
        "ai_enabled",
        "due_date",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "ai_interactive_enabled",
        "raster_mask_native_editing_enabled",
        "mask_qc_config",
        "point_cloud_quality_config",
        "rendering_config",
        "annotation_guide",
        "guide_assets",
        "video_sampling",
        "video_collaboration",
        "scene_mode",
        "prefer_same_scene_continuation",
        "scene_continuation_window_min",
        "default_variants",
        "preannotate_pipeline",
        "tool_bindings",
        "label_config",
    }
)
TEMPLATE_CONFIG_COLUMNS = frozenset(
    {
        "name",
        "description",
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "label_config",
        "ai_enabled",
        "sampling",
        "maximum_annotations",
        "show_overlap_first",
        "iou_dedup_threshold",
        "box_threshold",
        "text_threshold",
        "rendering_config",
        "annotation_guide",
        "scope",
        "organization_id",
        "source_project_id",
    }
)
_DIRTY_COLUMN_UNION = PROJECT_CONFIG_COLUMNS | TEMPLATE_CONFIG_COLUMNS

_TEXT_COLUMNS = frozenset(
    {
        "name",
        "type_label",
        "type_key",
        "data_type",
        "status",
        "sampling",
        "annotation_guide",
        "description",
        "scope",
    }
)
_BOOLEAN_COLUMNS = frozenset(
    {
        "ai_enabled",
        "show_overlap_first",
        "ai_interactive_enabled",
        "raster_mask_native_editing_enabled",
        "scene_mode",
        "prefer_same_scene_continuation",
    }
)
_INTEGER_COLUMNS = frozenset({"maximum_annotations", "scene_continuation_window_min"})
_FLOAT_COLUMNS = frozenset({"iou_dedup_threshold", "box_threshold", "text_threshold"})
_DATE_COLUMNS = frozenset({"due_date"})
_UUID_COLUMNS = frozenset({"organization_id", "source_project_id"})
_PROJECT_JSON_COLUMNS = frozenset(
    {
        "mask_qc_config",
        "point_cloud_quality_config",
        "guide_assets",
        "video_sampling",
        "video_collaboration",
        "default_variants",
        "preannotate_pipeline",
    }
)
_JSON_COLUMNS = (
    frozenset(
        {
            "tool_bindings",
            "label_config",
            "rendering_config",
        }
    )
    | _PROJECT_JSON_COLUMNS
)


class ConfigurationColumnError(ValueError):
    """A caller attempted to write a column outside the fixed codec."""


def _json_ready(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _json_ready(value.model_dump(mode="python"))
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ConfigurationColumnError("JSON object keys must be strings")
            result[key] = _json_ready(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ConfigurationColumnError("Invalid JSON configuration value")


def _json_text(value: Any) -> str:
    try:
        return json.dumps(
            _json_ready(value),
            ensure_ascii=True,
            allow_nan=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ConfigurationColumnError("Invalid JSON configuration value") from None


def _uuid_text(value: Any, column: str) -> str:
    try:
        if isinstance(value, uuid.UUID):
            parsed = value
        elif type(value) is str:
            parsed = uuid.UUID(value)
        else:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConfigurationColumnError(f"Invalid UUID value for {column}") from None
    return str(parsed)


def _column_text(column: str, value: Any) -> str | None:
    if value is None:
        return "null" if column in _JSON_COLUMNS else None
    if column in _JSON_COLUMNS:
        return _json_text(value)
    if column in _TEXT_COLUMNS:
        if type(value) is not str:
            raise ConfigurationColumnError(f"Expected text value for {column}")
        return value
    if column in _BOOLEAN_COLUMNS:
        if type(value) is not bool:
            raise ConfigurationColumnError(f"Expected boolean value for {column}")
        return "true" if value else "false"
    if column in _INTEGER_COLUMNS:
        if type(value) is not int:
            raise ConfigurationColumnError(f"Expected integer value for {column}")
        return str(value)
    if column in _FLOAT_COLUMNS:
        if type(value) not in (int, float):
            raise ConfigurationColumnError(f"Expected numeric value for {column}")
        return str(value)
    if column in _DATE_COLUMNS:
        if isinstance(value, datetime):
            raise ConfigurationColumnError(f"Expected date value for {column}")
        if isinstance(value, date):
            return value.isoformat()
        if type(value) is str:
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        raise ConfigurationColumnError(f"Expected date value for {column}")
    if column in _UUID_COLUMNS:
        return _uuid_text(value, column)
    raise ConfigurationColumnError(f"Unsupported configuration column: {column}")


def encode_config_columns(
    resource_kind: ResourceKind,
    payload: Mapping[str, Any],
) -> tuple[list[str], list[str | None]]:
    """Encode only the fixed config columns as PostgreSQL text parameters.

    JSON columns use compact ASCII JSON text so JSONB parsing happens at the SQL
    write stage. Scalar text is passed unchanged, preserving PostgreSQL's text
    codec behavior for characters such as NUL.
    """
    if resource_kind == "project":
        allowed = PROJECT_CONFIG_COLUMNS
    elif resource_kind == "template":
        allowed = TEMPLATE_CONFIG_COLUMNS
    else:
        raise ConfigurationColumnError("Unknown configuration resource kind")
    if not isinstance(payload, Mapping):
        raise ConfigurationColumnError("Configuration payload must be a mapping")

    names: list[str] = []
    values: list[str | None] = []
    for column, value in payload.items():
        if type(column) is not str or column not in allowed:
            raise ConfigurationColumnError(
                f"Unsupported {resource_kind} configuration column: {column}"
            )
        names.append(column)
        values.append(_column_text(column, value))
    return names, values


def dirty_config_columns(
    current: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[str]:
    """Return changed whitelisted fields using legacy Python equality."""
    if not isinstance(current, Mapping) or not isinstance(payload, Mapping):
        raise ConfigurationColumnError("Current facts and payload must be mappings")
    dirty: list[str] = []
    for column, value in payload.items():
        if type(column) is not str or column not in _DIRTY_COLUMN_UNION:
            raise ConfigurationColumnError(
                f"Unsupported configuration column: {column}"
            )
        if column not in current or current[column] != value:
            dirty.append(column)
    return dirty
