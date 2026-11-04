"""Closed Labeling command for task-scoped bulk Annotation updates."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

ANNOTATION_BULK_PROTOCOL = "aap-labeling-annotation-bulk/v1"
ANNOTATION_BULK_FUNCTION = "aap_labeling_v1.annotation_bulk_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AnnotationBulkPatch(_ClosedModel):
    class_name: StrictStr | None = None
    attributes: dict[str, object] | None = None
    z_order: StrictInt | None = None
    is_locked: StrictBool | None = None
    is_hidden: StrictBool | None = None


class UpdateAnnotations(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-bulk/v1"] = Field(alias="schema")
    operation: Literal["update"]
    actor_id: uuid.UUID | None
    task_id: uuid.UUID | None
    project_id: uuid.UUID | None
    ids: list[uuid.UUID]
    patch: AnnotationBulkPatch


AnnotationBulkCommand = Annotated[
    UpdateAnnotations,
    Field(discriminator="operation"),
]


class AnnotationBulkRow(_ClosedModel):
    id: uuid.UUID
    class_name: StrictStr
    attributes: object
    attributes_meta: object
    z_order: StrictInt
    is_locked: StrictBool
    is_hidden: StrictBool
    version: StrictInt
    updated_at: datetime


class AnnotationBulkTrack(_ClosedModel):
    id: uuid.UUID
    class_name: StrictStr
    revision: StrictInt
    updated_at: datetime


class AnnotationBulkResult(_ClosedModel):
    annotations: list[AnnotationBulkRow]
    camera_members: list[AnnotationBulkRow]
    tracks: list[AnnotationBulkTrack]


class AnnotationBulkReply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-bulk/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AnnotationBulkCommand
    data: AnnotationBulkResult


class AnnotationBulkUnavailable(RuntimeError):
    """The Labeling Annotation bulk owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(AnnotationBulkCommand)
_REPLY_ADAPTER = TypeAdapter(AnnotationBulkReply)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def decode_annotation_bulk_command(value: object) -> AnnotationBulkCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationBulkUnavailable() from None


def decode_annotation_bulk_reply(
    request: AnnotationBulkCommand, value: object
) -> AnnotationBulkResult:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationBulkUnavailable() from None
    annotation_ids = [row.id for row in reply.data.annotations]
    if (
        not reply.ok
        or reply.request != request
        or len(annotation_ids) != len(request.ids)
        or len(set(annotation_ids)) != len(annotation_ids)
        or set(annotation_ids) != set(request.ids)
    ):
        raise AnnotationBulkUnavailable()
    return reply.data


def annotation_bulk_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANNOTATION_BULK_PROTOCOL
    schema["x-postgresql-functions"] = [ANNOTATION_BULK_FUNCTION]
    return schema


def annotation_bulk_command_json(command: AnnotationBulkCommand) -> str:
    validated = decode_annotation_bulk_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    )
    return validated.model_dump_json(by_alias=True, exclude_unset=True)


__all__ = [
    "ANNOTATION_BULK_FUNCTION",
    "ANNOTATION_BULK_PROTOCOL",
    "AnnotationBulkCommand",
    "AnnotationBulkPatch",
    "AnnotationBulkReply",
    "AnnotationBulkResult",
    "AnnotationBulkRow",
    "AnnotationBulkTrack",
    "AnnotationBulkUnavailable",
    "UpdateAnnotations",
    "annotation_bulk_command_json",
    "annotation_bulk_schema",
    "decode_annotation_bulk_command",
    "decode_annotation_bulk_reply",
]
