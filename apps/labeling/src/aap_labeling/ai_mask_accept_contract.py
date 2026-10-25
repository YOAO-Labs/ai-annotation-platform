"""Closed idempotency and Annotation refinement commands for AI Mask accept."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

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
)

from .annotation_write_contract import _json_bytes

AI_MASK_ACCEPT_PROTOCOL = "aap-labeling-ai-mask-accept/v1"
AI_MASK_ACCEPT_FUNCTION = "aap_labeling_v1.ai_mask_accept_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-ai-mask-accept/v1"] = Field(alias="schema")
    operation: StrictStr


class FindAiMaskDecision(_Command):
    operation: Literal["find_decision"]
    task_id: uuid.UUID
    idempotency_key: StrictStr


class RecordAiMaskDecision(_Command):
    operation: Literal["record_decision"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    idempotency_key: StrictStr
    request_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: StrictStr
    content_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prediction_id: uuid.UUID
    prediction_created_at: AwareDatetime
    annotation_id: uuid.UUID
    source_annotation_id: uuid.UUID | None
    source_version: StrictInt | None
    result_version: StrictInt
    actor_id: uuid.UUID
    response_json: dict[str, object]


class RefineAiMaskAnnotation(_Command):
    operation: Literal["refine_annotation"]
    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    expected_batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_review_round_id: uuid.UUID | None
    annotation_id: uuid.UUID
    expected_version: StrictInt = Field(ge=1)
    video_segment_id: uuid.UUID | None
    annotation_type: Literal["raster_mask", "video_track_mask"]
    prediction_id: uuid.UUID
    class_name: StrictStr
    geometry: dict[str, object]
    track_id: StrictStr | None
    confidence: float | None


class ExpireAiMaskDecisions(_Command):
    operation: Literal["expire_decisions"]
    now: AwareDatetime | None


AiMaskAcceptCommand = Annotated[
    FindAiMaskDecision
    | RecordAiMaskDecision
    | RefineAiMaskAnnotation
    | ExpireAiMaskDecisions,
    Field(discriminator="operation"),
]


class AiMaskDecisionData(_ClosedModel):
    found: StrictBool
    request_digest: StrictStr | None
    candidate_id: StrictStr | None
    content_digest: StrictStr | None
    prediction_id: uuid.UUID | None
    prediction_created_at: AwareDatetime | None
    annotation_id: uuid.UUID | None
    source_annotation_id: uuid.UUID | None
    source_version: StrictInt | None
    result_version: StrictInt | None
    actor_id: uuid.UUID | None
    response_json: dict[str, object] | None
    expires_at: AwareDatetime | None


class AiMaskAcceptData(_ClosedModel):
    decision: AiMaskDecisionData | None
    annotation_id: uuid.UUID | None
    version: StrictInt | None
    updated_at: AwareDatetime | None
    affected: StrictInt


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-ai-mask-accept/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AiMaskAcceptCommand
    data: AiMaskAcceptData


class AiMaskAcceptUnavailable(RuntimeError):
    """The AI Mask accept owner returned an invalid or mismatched result."""


_COMMAND = TypeAdapter(AiMaskAcceptCommand)
_REPLY = TypeAdapter(_Reply)


def decode_ai_mask_accept_command(value: object) -> AiMaskAcceptCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AiMaskAcceptUnavailable() from None


def ai_mask_accept_command_json(command: AiMaskAcceptCommand) -> str:
    return decode_ai_mask_accept_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def decode_ai_mask_accept_reply(request: AiMaskAcceptCommand, value: object):
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AiMaskAcceptUnavailable() from None
    if not reply.ok or reply.request != request:
        raise AiMaskAcceptUnavailable()
    if isinstance(request, FindAiMaskDecision) and reply.data.decision is None:
        raise AiMaskAcceptUnavailable()
    if isinstance(request, RefineAiMaskAnnotation) and (
        reply.data.annotation_id != request.annotation_id
        or reply.data.version != request.expected_version + 1
        or reply.data.updated_at is None
    ):
        raise AiMaskAcceptUnavailable()
    return reply.data


def ai_mask_accept_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = AI_MASK_ACCEPT_PROTOCOL
    schema["x-postgresql-functions"] = [AI_MASK_ACCEPT_FUNCTION]
    return schema
