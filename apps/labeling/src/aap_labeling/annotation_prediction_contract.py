"""Closed writes for accepted candidates and secondary-inference annotations."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .annotation_write_contract import _json_bytes

ANNOTATION_PREDICTION_PROTOCOL = "aap-labeling-annotation-prediction/v1"
ANNOTATION_PREDICTION_FUNCTION = "aap_labeling_v1.annotation_prediction_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-prediction/v1"] = Field(alias="schema")
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None


class CreateAcceptedAnnotation(_Command):
    operation: Literal["create_accepted"]
    prediction_id: uuid.UUID
    prediction_created_at: AwareDatetime
    annotation_type: StrictStr
    tool_unit_id: StrictStr
    class_name: StrictStr
    geometry: dict[str, object]
    confidence: FiniteFloat | None
    video_segment_id: uuid.UUID | None
    attributes: dict[str, object]
    attributes_meta: dict[str, object]
    track_id: StrictStr | None
    temporal_role: Literal["keyframe", "sample"]


class ApplySecondaryAttributes(_Command):
    operation: Literal["apply_secondary_attributes"]
    expected_version: StrictInt = Field(ge=1)
    attributes: dict[str, object]
    attributes_meta: dict[str, object]


class MarkSecondaryChild(_Command):
    operation: Literal["mark_secondary_child"]
    expected_version: StrictInt = Field(ge=1)
    parent_annotation_id: uuid.UUID


AnnotationPredictionCommand = Annotated[
    CreateAcceptedAnnotation | ApplySecondaryAttributes | MarkSecondaryChild,
    Field(discriminator="operation"),
]


class AnnotationPredictionData(_ClosedModel):
    annotation_id: uuid.UUID
    version: StrictInt = Field(ge=1)
    source: StrictStr
    updated_at: AwareDatetime


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-prediction/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AnnotationPredictionCommand
    data: AnnotationPredictionData


class AnnotationPredictionUnavailable(RuntimeError):
    """The Annotation prediction owner returned invalid or mismatched data."""


_COMMAND = TypeAdapter(AnnotationPredictionCommand)
_REPLY = TypeAdapter(_Reply)


def decode_annotation_prediction_command(value: object) -> AnnotationPredictionCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationPredictionUnavailable() from None


def annotation_prediction_command_json(command: AnnotationPredictionCommand) -> str:
    return decode_annotation_prediction_command(
        command.model_dump_json(by_alias=True)
    ).model_dump_json(by_alias=True)


def decode_annotation_prediction_reply(
    request: AnnotationPredictionCommand, value: object
) -> AnnotationPredictionData:
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationPredictionUnavailable() from None
    if (
        not reply.ok
        or reply.request != request
        or reply.data.annotation_id != request.annotation_id
    ):
        raise AnnotationPredictionUnavailable()
    if isinstance(request, CreateAcceptedAnnotation) and (
        reply.data.version != 1 or reply.data.source != "prediction_based"
    ):
        raise AnnotationPredictionUnavailable()
    if (
        isinstance(request, ApplySecondaryAttributes)
        and reply.data.version != request.expected_version + 1
    ):
        raise AnnotationPredictionUnavailable()
    if isinstance(request, MarkSecondaryChild) and (
        reply.data.source != "prediction_based"
        or reply.data.version
        not in {request.expected_version, request.expected_version + 1}
    ):
        raise AnnotationPredictionUnavailable()
    return reply.data


def annotation_prediction_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANNOTATION_PREDICTION_PROTOCOL
    schema["x-postgresql-functions"] = [ANNOTATION_PREDICTION_FUNCTION]
    return schema
