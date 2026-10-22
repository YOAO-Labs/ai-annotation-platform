"""Closed contract for the existing Project maintenance SQL capabilities."""

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

PROTOCOL = "aap-workspace-project-maintenance/v1"
NATIVE_SCHEMA = "aap_workspace_v1"


class _ProjectMaintenanceModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def reject_protocol_alias(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class ImportProjectReference(_ProjectMaintenanceModel):
    """Lookup/create result; a null project ID means the lookup found no row."""

    protocol: Literal["aap-workspace-project-maintenance/v1"] = Field(alias="schema")
    display_id: StrictStr
    project_id: uuid.UUID | None


class ImportProjectCreated(ImportProjectReference):
    """Create result; the caller-supplied Project UUID must be present."""

    project_id: uuid.UUID


class PointQualityConfigWrite(_ProjectMaintenanceModel):
    """Result of the caller's point-cloud quality configuration write."""

    protocol: Literal["aap-workspace-project-maintenance/v1"] = Field(alias="schema")
    project_id: uuid.UUID
    updated_at: datetime = Field(
        json_schema_extra={"x-timezone-aware": True, "x-source-timezone": "UTC"}
    )

    @field_validator("updated_at", mode="before")
    @classmethod
    def parse_timestamp_text(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid Project maintenance timestamp") from None
        return value

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Project maintenance timestamp must be timezone-aware")
        return value


_REFERENCE_ADAPTER = TypeAdapter(ImportProjectReference)
_CREATED_ADAPTER = TypeAdapter(ImportProjectCreated)
_QUALITY_WRITE_ADAPTER = TypeAdapter(PointQualityConfigWrite)
_REPLY_ADAPTER = TypeAdapter(
    ImportProjectReference | ImportProjectCreated | PointQualityConfigWrite
)


class ProjectMaintenanceUnavailable(RuntimeError):
    """The native Workspace Project maintenance capability is unavailable."""

    def __init__(self):
        super().__init__("Workspace Project maintenance unavailable")


def decode_import_project_reference(
    payload: dict | bytes | str | ImportProjectReference,
    *,
    display_id: str,
    expected_project_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Decode a lookup/create result and bind it to the requested Project identity."""
    if type(display_id) is not str or (
        expected_project_id is not None
        and not isinstance(expected_project_id, uuid.UUID)
    ):
        raise ProjectMaintenanceUnavailable()
    try:
        encoded = _json_bytes(payload)
        adapter = (
            _CREATED_ADAPTER if expected_project_id is not None else _REFERENCE_ADAPTER
        )
        reply = adapter.validate_json(encoded)
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise ProjectMaintenanceUnavailable() from None

    if reply.display_id != display_id:
        raise ProjectMaintenanceUnavailable()
    if expected_project_id is not None and reply.project_id != expected_project_id:
        raise ProjectMaintenanceUnavailable()
    return reply.project_id


def decode_point_quality_config_write(
    payload: dict | bytes | str | PointQualityConfigWrite,
    *,
    project_id: uuid.UUID,
) -> datetime:
    """Decode a write result and require it to name the requested Project."""
    if not isinstance(project_id, uuid.UUID):
        raise ProjectMaintenanceUnavailable()
    try:
        reply = _QUALITY_WRITE_ADAPTER.validate_json(_json_bytes(payload))
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
        name="read_import_project",
        arguments=(("p_display_id", "text", _TEXT_SCHEMA),),
        result_model=ImportProjectReference,
        transaction_semantics=(
            "Read-only lookup inside the caller's transaction; the caller owns "
            "authorization and transaction commit. A missing row returns a null "
            "project_id."
        ),
    ),
    _NativeFunction(
        name="create_import_project",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_display_id", "text", _TEXT_SCHEMA),
            ("p_name", "text", _TEXT_SCHEMA),
            ("p_owner_id", "uuid", _UUID_SCHEMA),
            ("p_tool_bindings", "jsonb", _JSONB_SCHEMA),
        ),
        result_model=ImportProjectCreated,
        transaction_semantics=(
            "Insert one Project with the caller-supplied UUID and return its "
            "reference; this is not an upsert. The caller owns authorization, "
            "the surrounding workflow transaction, and commit. Uniqueness, "
            "foreign-key, and length errors remain database errors."
        ),
    ),
    _NativeFunction(
        name="write_point_cloud_quality_config",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_config", "jsonb", _JSONB_SCHEMA),
        ),
        result_model=PointQualityConfigWrite,
        transaction_semantics=(
            "Update the Project configuration in the caller's existing "
            "transaction. The caller owns authorization, Project FOR UPDATE, "
            "evaluation, Audit, and commit. An equal JSONB value leaves "
            "updated_at unchanged; a missing Project raises no_data_found. "
            "No compare-and-swap or lock mode is added by this capability."
        ),
    ),
)


def project_maintenance_schema() -> dict[str, Any]:
    """Return the response JSON Schema and frozen native function descriptor."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = PROTOCOL
    schema["x-native-capabilities"] = {
        "protocol": PROTOCOL,
        "postgres_schema": NATIVE_SCHEMA,
        "functions": [
            {
                "name": function.name,
                "signature": f"{NATIVE_SCHEMA}.{function.name}({', '.join(argument[1] for argument in function.arguments)})",
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
            "Existing application callers retain authorization and workflow "
            "evaluation; these functions add no application authorization."
        ),
        "transaction_boundary": "The caller owns the surrounding transaction and commit.",
    }
    return schema


__all__ = [
    "NATIVE_SCHEMA",
    "PROTOCOL",
    "ImportProjectCreated",
    "ImportProjectReference",
    "PointQualityConfigWrite",
    "ProjectMaintenanceUnavailable",
    "decode_import_project_reference",
    "decode_point_quality_config_write",
    "project_maintenance_schema",
]
