"""Closed contracts for trusted point-cloud seed Project capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import uuid
from typing import Any, Literal

from pydantic import (
    Field,
    JsonValue,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes

from .project_maintenance_contract import ProjectMaintenanceUnavailable

PROTOCOL = "aap-workspace-seed-project/v1"
NATIVE_SCHEMA = "aap_workspace_v1"


class _SeedProjectModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def reject_protocol_alias(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class PointCloudSeedProjectCreated(_SeedProjectModel):
    """Receipt for inserting the legacy non-scene point-cloud seed Project."""

    protocol: Literal["aap-workspace-seed-project/v1"] = Field(alias="schema")
    display_id: StrictStr
    project_id: uuid.UUID


class NuScenesSeedDefaultsWrite(_SeedProjectModel):
    """Receipt for reasserting the fixed nuScenes development Project fields."""

    protocol: Literal["aap-workspace-seed-project/v1"] = Field(alias="schema")
    project_id: uuid.UUID
    updated_at: datetime | None = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )

    @field_validator("updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid Workspace seed Project timestamp") from None
        return value

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Workspace seed Project timestamp must be timezone-aware")
        return value


_CREATED_ADAPTER = TypeAdapter(PointCloudSeedProjectCreated)
_DEFAULTS_WRITE_ADAPTER = TypeAdapter(NuScenesSeedDefaultsWrite)
_REPLY_ADAPTER = TypeAdapter(PointCloudSeedProjectCreated | NuScenesSeedDefaultsWrite)


def decode_point_cloud_seed_project_created(
    payload: dict | bytes | str | PointCloudSeedProjectCreated,
    *,
    display_id: str,
    expected_project_id: uuid.UUID,
) -> uuid.UUID:
    """Decode a create receipt and bind both stable and generated identities."""
    if type(display_id) is not str or not isinstance(expected_project_id, uuid.UUID):
        raise ProjectMaintenanceUnavailable()
    try:
        reply = _CREATED_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectMaintenanceUnavailable() from None

    if reply.display_id != display_id or reply.project_id != expected_project_id:
        raise ProjectMaintenanceUnavailable()
    return reply.project_id


def decode_nuscenes_seed_defaults_write(
    payload: dict | bytes | str | NuScenesSeedDefaultsWrite,
    *,
    project_id: uuid.UUID,
) -> datetime | None:
    """Decode a fixed-write receipt; null timestamp means the row was missing."""
    if not isinstance(project_id, uuid.UUID):
        raise ProjectMaintenanceUnavailable()
    try:
        reply = _DEFAULTS_WRITE_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectMaintenanceUnavailable() from None

    if reply.project_id != project_id:
        raise ProjectMaintenanceUnavailable()
    return reply.updated_at


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    arguments: tuple[tuple[str, str, TypeAdapter[Any]], ...]
    result_model: type[ClosedModel]
    transaction_semantics: str


_UUID_SCHEMA = TypeAdapter(uuid.UUID)
_TEXT_SCHEMA = TypeAdapter(StrictStr)
_JSONB_SCHEMA = TypeAdapter(dict[StrictStr, JsonValue])

_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="create_point_cloud_seed_project",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_display_id", "text", _TEXT_SCHEMA),
            ("p_name", "text", _TEXT_SCHEMA),
            ("p_owner_id", "uuid", _UUID_SCHEMA),
            ("p_tool_bindings", "jsonb", _JSONB_SCHEMA),
        ),
        result_model=PointCloudSeedProjectCreated,
        transaction_semantics=(
            "Insert one fixed non-scene Lidar seed Project using the caller-supplied "
            "UUID and display ID; this is not an upsert. The trusted caller owns "
            "authorization, the surrounding workflow transaction, and commit. "
            "Uniqueness, foreign-key, and length errors remain database errors."
        ),
    ),
    _NativeFunction(
        name="write_nuscenes_seed_defaults",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_name", "text", _TEXT_SCHEMA),
            ("p_tool_bindings", "jsonb", _JSONB_SCHEMA),
        ),
        result_model=NuScenesSeedDefaultsWrite,
        transaction_semantics=(
            "Set only name, tool_bindings, scene_mode, and "
            "prefer_same_scene_continuation on an existing Project in the caller's "
            "transaction. Equal values leave updated_at unchanged; a missing row "
            "returns a null updated_at. The trusted caller retains authorization, "
            "workflow ordering, and commit."
        ),
    ),
)


def seed_project_schema() -> dict[str, Any]:
    """Return the reply JSON Schema and fixed native capability descriptor."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PROTOCOL
    schema["x-native-capabilities"] = {
        "protocol": PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": (
                    f"{NATIVE_SCHEMA}.{function.name}"
                    f"({', '.join(argument[1] for argument in function.arguments)})"
                ),
                "arguments": [
                    {
                        "name": name,
                        "postgres_type": postgres_type,
                        "json_schema": adapter.json_schema(),
                    }
                    for name, postgres_type, adapter in function.arguments
                ],
                "result_postgres_type": "jsonb",
                "result_model": function.result_model.__name__,
                "result_schema_ref": f"#/$defs/{function.result_model.__name__}",
                "transaction_semantics": function.transaction_semantics,
            }
            for function in _NATIVE_FUNCTIONS
        ],
        "authorization_boundary": (
            "Trusted seed callers retain authorization and workflow policy; these "
            "fixed maintenance functions add no application authorization."
        ),
        "transaction_boundary": "The caller owns the surrounding transaction and commit.",
    }
    return schema


__all__ = [
    "NATIVE_SCHEMA",
    "PROTOCOL",
    "NuScenesSeedDefaultsWrite",
    "PointCloudSeedProjectCreated",
    "ProjectMaintenanceUnavailable",
    "decode_nuscenes_seed_defaults_write",
    "decode_point_cloud_seed_project_created",
    "seed_project_schema",
]
