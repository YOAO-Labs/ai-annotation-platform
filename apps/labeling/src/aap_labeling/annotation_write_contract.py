"""Closed Labeling commands for manual Annotation writes and create receipts."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Literal, TypeAlias

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

ANNOTATION_WRITE_PROTOCOL = "aap-labeling-annotation-writes/v1"
ANNOTATION_WRITE_FUNCTION = "aap_labeling_v1.annotation_write_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-writes/v1"] = Field(alias="schema")
    operation: StrictStr


class CreateAnnotation(_Command):
    operation: Literal["create"]
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    confidence: float | None
    parent_prediction_id: uuid.UUID | None
    parent_annotation_id: uuid.UUID | None
    video_segment_id: uuid.UUID | None
    lead_time: float | None
    attributes: dict[str, object]
    track_id: StrictStr | None


class UpdateAnnotation(_Command):
    operation: Literal["update"]
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID | None
    expected_version: StrictInt
    geometry: dict[str, object] | None = None
    class_name: StrictStr | None = None
    confidence: float | None = None
    attributes: dict[str, object] | None = None
    attributes_meta: dict[str, object] | None = None
    z_order: StrictInt | None = None
    is_locked: StrictBool | None = None
    is_hidden: StrictBool | None = None
    track_id: StrictStr | None


class BindSceneTrackMember(_Command):
    operation: Literal["bind_scene_track_member"]
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID | None
    expected_version: StrictInt
    scene_id: uuid.UUID
    frame_index: StrictInt
    temporal_role: Literal["keyframe", "derived", "sample"]


class DeleteAnnotation(_Command):
    operation: Literal["delete"]
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID | None
    expected_version: StrictInt


class ReadCreateReceipt(_Command):
    operation: Literal["read_create_receipt"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr
    request_digest: StrictStr


class WriteCreateReceipt(_Command):
    operation: Literal["write_create_receipt"]
    operation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    idempotency_key: StrictStr
    request_digest: StrictStr
    response_json: dict[str, object]


AnnotationWriteCommand: TypeAlias = Annotated[
    CreateAnnotation
    | UpdateAnnotation
    | BindSceneTrackMember
    | DeleteAnnotation
    | ReadCreateReceipt
    | WriteCreateReceipt,
    Field(discriminator="operation"),
]


class AnnotationWriteMutation(_ClosedModel):
    annotation_id: uuid.UUID
    version: StrictInt
    changed: StrictBool
    deleted_children: StrictInt = 0
    deleted_camera_members: StrictInt = 0


class AnnotationWriteReceipt(_ClosedModel):
    found: StrictBool
    response_json: dict[str, object] | None


class AnnotationWriteData(_ClosedModel):
    mutation: AnnotationWriteMutation | None = None
    receipt: AnnotationWriteReceipt | None = None


class AnnotationWriteReply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-writes/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AnnotationWriteCommand
    data: AnnotationWriteData


class AnnotationWriteUnavailable(RuntimeError):
    """The Labeling Annotation write owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(AnnotationWriteCommand)
_REPLY_ADAPTER = TypeAdapter(AnnotationWriteReply)


def _json_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def decode_annotation_write_command(value: object) -> AnnotationWriteCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationWriteUnavailable() from None


def decode_annotation_write_reply(
    request: AnnotationWriteCommand, value: object
) -> AnnotationWriteData:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationWriteUnavailable() from None
    if not reply.ok or reply.request != request:
        raise AnnotationWriteUnavailable()
    if request.operation in {"read_create_receipt", "write_create_receipt"}:
        if reply.data.receipt is None or reply.data.mutation is not None:
            raise AnnotationWriteUnavailable()
    elif reply.data.mutation is None or reply.data.receipt is not None:
        raise AnnotationWriteUnavailable()
    return reply.data


def annotation_write_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANNOTATION_WRITE_PROTOCOL
    schema["x-postgresql-functions"] = [ANNOTATION_WRITE_FUNCTION]
    return schema


def annotation_write_command_json(command: AnnotationWriteCommand) -> str:
    validated = decode_annotation_write_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    )
    return validated.model_dump_json(by_alias=True, exclude_unset=True)
