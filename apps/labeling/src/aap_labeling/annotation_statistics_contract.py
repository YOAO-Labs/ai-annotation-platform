"""Closed, language-neutral commands for Annotation-derived Task statistics."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

ANNOTATION_STATISTICS_PROTOCOL = "aap-labeling-annotation-statistics/v1"
ANNOTATION_STATISTICS_DATA_SCHEMA = "aap-labeling-annotation-statistics-data/v1"
ANNOTATION_STATISTICS_FUNCTION = "aap_labeling_v1.annotation_statistics_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AnnotationStatisticsCommand(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-statistics/v1"] = Field(alias="schema")
    operation: Literal["recompute_task_statistics"]
    task_id: uuid.UUID


class AnnotationStatisticsSnapshot(_ClosedModel):
    task_found: StrictBool
    total_annotations: StrictInt = Field(ge=0)
    is_labeled: StrictBool
    status: StrictStr | None
    batch_id: uuid.UUID | None
    status_changed: StrictBool
    updated_at: AwareDatetime | None

    @model_validator(mode="after")
    def check_projection(self):
        if self.is_labeled != (self.total_annotations > 0):
            raise ValueError("is_labeled must match total_annotations")
        if self.task_found:
            if self.status is None or self.updated_at is None:
                raise ValueError("An existing Task requires status and updated_at")
        elif (
            self.status is not None
            or self.batch_id is not None
            or self.status_changed
            or self.updated_at is not None
        ):
            raise ValueError("A missing Task cannot contain persisted fields")
        return self


class AnnotationStatisticsReply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-statistics-data/v1"] = Field(
        alias="schema"
    )
    ok: Literal[True]
    request: AnnotationStatisticsCommand
    data: AnnotationStatisticsSnapshot


class AnnotationStatisticsUnavailable(RuntimeError):
    """The Labeling annotation-statistics owner returned an invalid reply."""


_COMMAND_ADAPTER = TypeAdapter(AnnotationStatisticsCommand)
_REPLY_ADAPTER = TypeAdapter(AnnotationStatisticsReply)


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


def annotation_statistics_command(
    task_id: uuid.UUID,
) -> AnnotationStatisticsCommand:
    try:
        return AnnotationStatisticsCommand(
            schema=ANNOTATION_STATISTICS_PROTOCOL,
            operation="recompute_task_statistics",
            task_id=task_id,
        )
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise AnnotationStatisticsUnavailable() from None


def decode_annotation_statistics_command(value: object) -> AnnotationStatisticsCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationStatisticsUnavailable() from None


def decode_annotation_statistics_reply(
    request: AnnotationStatisticsCommand, value: object
) -> AnnotationStatisticsSnapshot:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationStatisticsUnavailable() from None
    if reply.request != request:
        raise AnnotationStatisticsUnavailable()
    return reply.data


def annotation_statistics_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANNOTATION_STATISTICS_PROTOCOL
    schema["x-postgresql-functions"] = [ANNOTATION_STATISTICS_FUNCTION]
    return schema


def annotation_statistics_command_json(
    request: AnnotationStatisticsCommand,
) -> str:
    validated = decode_annotation_statistics_command(
        request.model_dump_json(by_alias=True, exclude_none=False)
    )
    return validated.model_dump_json(by_alias=True, exclude_none=False)


def annotation_status_after_recompute(status: str, total_annotations: int) -> str:
    """Keep only the legacy pending/in-progress transitions tied to annotation count."""
    if total_annotations > 0 and status == "pending":
        return "in_progress"
    if total_annotations == 0 and status == "in_progress":
        return "pending"
    return status


__all__ = [
    "ANNOTATION_STATISTICS_DATA_SCHEMA",
    "ANNOTATION_STATISTICS_FUNCTION",
    "ANNOTATION_STATISTICS_PROTOCOL",
    "AnnotationStatisticsCommand",
    "AnnotationStatisticsSnapshot",
    "AnnotationStatisticsUnavailable",
    "annotation_statistics_command",
    "annotation_statistics_command_json",
    "annotation_statistics_schema",
    "annotation_status_after_recompute",
    "decode_annotation_statistics_command",
    "decode_annotation_statistics_reply",
]
