"""Closed contracts for trusted media seed Project maintenance operations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import uuid
from typing import Any, Literal

from pydantic import (
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from aap_workspace_contract.wire import ClosedModel, _json_bytes

PROTOCOL = "aap-workspace-media-seed-project/v1"
MEDIA_SEED_PROJECT_PROTOCOL = PROTOCOL
NATIVE_SCHEMA = "aap_workspace_v1"
MediaSeedKind = Literal["coco8", "video", "ocr", "large_image"]

MEDIA_SEED_PROJECT_PROJECTION_FIELDS = (
    "name",
    "type_label",
    "type_key",
    "data_type",
    "owner_id",
    "status",
    "ai_enabled",
    "ai_interactive_enabled",
    "raster_mask_native_editing_enabled",
    "tool_bindings",
    "created_at",
    "updated_at",
    "ml_backend_pool_id",
    "preannotate_pipeline",
)


class _MediaSeedProjectModel(ClosedModel):
    @model_validator(mode="before")
    @classmethod
    def reject_protocol_alias(cls, value):
        if isinstance(value, dict) and "protocol" in value:
            raise ValueError("Use only the declared wire field schema")
        return value


class MediaSeedProjectCreated(_MediaSeedProjectModel):
    """Receipt for inserting one media seed Project."""

    protocol: Literal["aap-workspace-media-seed-project/v1"] = Field(alias="schema")
    kind: MediaSeedKind
    display_id: StrictStr
    project_id: uuid.UUID


class SeedProjectProjectionWrite(_MediaSeedProjectModel):
    """Receipt for applying a closed Project projection patch."""

    protocol: Literal["aap-workspace-media-seed-project/v1"] = Field(alias="schema")
    project_id: uuid.UUID
    found: StrictBool
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
                raise ValueError(
                    "Invalid Workspace media seed Project timestamp"
                ) from None
        return value

    @field_validator("updated_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError(
                "Workspace media seed Project timestamp must be timezone-aware"
            )
        return value

    @model_validator(mode="after")
    def found_matches_timestamp(self):
        if self.found != (self.updated_at is not None):
            raise ValueError("Projection receipt found/timestamp mismatch")
        return self


class MediaSeedProjectUnavailable(RuntimeError):
    """A native media seed Project capability is unavailable or malformed."""

    def __init__(self):
        super().__init__("Workspace media seed Project capability unavailable")


_CREATED_ADAPTER = TypeAdapter(MediaSeedProjectCreated)
_PROJECTION_WRITE_ADAPTER = TypeAdapter(SeedProjectProjectionWrite)
_REPLY_ADAPTER = TypeAdapter(MediaSeedProjectCreated | SeedProjectProjectionWrite)


def decode_media_seed_project_created(
    payload: dict | bytes | str | MediaSeedProjectCreated,
    *,
    kind: MediaSeedKind,
    display_id: str,
    expected_project_id: uuid.UUID,
) -> uuid.UUID:
    """Decode a create receipt and bind it to the requested kind and identities."""
    if (
        type(kind) is not str
        or kind not in ("coco8", "video", "ocr", "large_image")
        or type(display_id) is not str
        or not isinstance(expected_project_id, uuid.UUID)
    ):
        raise MediaSeedProjectUnavailable()
    try:
        reply = _CREATED_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MediaSeedProjectUnavailable() from None

    if (
        reply.kind != kind
        or reply.display_id != display_id
        or reply.project_id != expected_project_id
    ):
        raise MediaSeedProjectUnavailable()
    return reply.project_id


def decode_seed_project_projection_write(
    payload: dict | bytes | str | SeedProjectProjectionWrite,
    *,
    project_id: uuid.UUID,
) -> SeedProjectProjectionWrite:
    """Decode a projection receipt and bind it to the requested Project ID."""
    if not isinstance(project_id, uuid.UUID):
        raise MediaSeedProjectUnavailable()
    try:
        reply = _PROJECTION_WRITE_ADAPTER.validate_json(_json_bytes(payload))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise MediaSeedProjectUnavailable() from None

    if reply.project_id != project_id:
        raise MediaSeedProjectUnavailable()
    return reply


@dataclass(frozen=True)
class _NativeFunction:
    name: str
    arguments: tuple[tuple[str, str, TypeAdapter[Any] | dict[str, Any]], ...]
    result_model: type[ClosedModel]
    transaction_semantics: str


_UUID_SCHEMA = TypeAdapter(uuid.UUID)
_TEXT_SCHEMA = TypeAdapter(StrictStr)
_KIND_SCHEMA = TypeAdapter(MediaSeedKind)
_TOOL_BINDINGS_SCHEMA = TypeAdapter(dict[StrictStr, JsonValue])
_TIMESTAMP_SCHEMA = TypeAdapter(datetime)
_PROJECTION_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "type_label": {"type": "string"},
        "type_key": {"type": "string"},
        "data_type": {"type": "string"},
        "owner_id": {"type": "string", "format": "uuid"},
        "status": {"type": "string"},
        "ai_enabled": {"type": "boolean"},
        "ai_interactive_enabled": {"type": "boolean"},
        "raster_mask_native_editing_enabled": {"type": "boolean"},
        "tool_bindings": {
            "type": "object",
            "additionalProperties": {},
        },
        "created_at": {
            "type": "string",
            "format": "date-time",
            "x-timezone-aware": True,
            "x-source-timezone": "UTC",
        },
        "updated_at": {
            "type": "string",
            "format": "date-time",
            "x-timezone-aware": True,
            "x-source-timezone": "UTC",
        },
        "ml_backend_pool_id": {
            "anyOf": [
                {"type": "string", "format": "uuid"},
                {"type": "null"},
            ]
        },
        "preannotate_pipeline": {
            "anyOf": [
                {"type": "array", "items": {}},
                {"type": "null"},
            ]
        },
    },
}

_NATIVE_FUNCTIONS = (
    _NativeFunction(
        name="create_media_seed_project",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_display_id", "text", _TEXT_SCHEMA),
            ("p_name", "text", _TEXT_SCHEMA),
            ("p_owner_id", "uuid", _UUID_SCHEMA),
            ("p_kind", "text", _KIND_SCHEMA),
            ("p_tool_bindings", "jsonb", _TOOL_BINDINGS_SCHEMA),
        ),
        result_model=MediaSeedProjectCreated,
        transaction_semantics=(
            "Insert one Project with kind-specific fixed media type and AI defaults, "
            "using the caller-supplied UUID, display ID, name, owner, and tool bindings. "
            "This is not an upsert. The trusted caller owns authorization, the "
            "surrounding workflow transaction, and commit. Uniqueness, foreign-key, "
            "and length errors remain database errors."
        ),
    ),
    _NativeFunction(
        name="write_seed_project_projection",
        arguments=(
            ("p_project_id", "uuid", _UUID_SCHEMA),
            ("p_patch", "jsonb", _PROJECTION_PATCH_SCHEMA),
        ),
        result_model=SeedProjectProjectionWrite,
        transaction_semantics=(
            "Update only the declared media seed Project projection fields in the "
            "caller's transaction. The patch must be a non-empty object containing "
            "only declared fields. updated_at uses the database current time unless "
            "the caller explicitly supplies it; created_at may be pinned. A missing "
            "Project returns found=false and updated_at=null. PostgreSQL casts, "
            "constraints, and foreign-key errors propagate. The trusted caller owns "
            "authorization and commit."
        ),
    ),
)


def media_seed_project_schema() -> dict[str, Any]:
    """Return the receipt JSON Schema and fixed Native capability descriptor."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = MEDIA_SEED_PROJECT_PROTOCOL
    schema["x-native-capabilities"] = {
        "protocol": MEDIA_SEED_PROJECT_PROTOCOL,
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
                        "json_schema": (
                            deepcopy(adapter)
                            if isinstance(adapter, dict)
                            else adapter.json_schema()
                        ),
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
        "projection_fields": list(MEDIA_SEED_PROJECT_PROJECTION_FIELDS),
        "authorization_boundary": (
            "Trusted media seed callers retain authorization and workflow policy; "
            "these fixed maintenance functions add no application authorization."
        ),
        "transaction_boundary": "The caller owns the surrounding transaction and commit.",
    }
    return schema


__all__ = [
    "MEDIA_SEED_PROJECT_PROTOCOL",
    "MEDIA_SEED_PROJECT_PROJECTION_FIELDS",
    "MediaSeedKind",
    "MediaSeedProjectCreated",
    "MediaSeedProjectUnavailable",
    "NATIVE_SCHEMA",
    "PROTOCOL",
    "SeedProjectProjectionWrite",
    "decode_media_seed_project_created",
    "decode_seed_project_projection_write",
    "media_seed_project_schema",
]
