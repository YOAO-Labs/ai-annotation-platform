"""Closed Labeling task lifecycle and contributor-evidence commands."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

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

TASK_WORKFLOW_PROTOCOL = "aap-labeling-task-workflow/v1"
TaskStatus = StrictStr
RejectReasonType = Literal["missing", "extra", "wrong_label", "wrong_geometry"]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _TaskScope(_ClosedModel):
    protocol: Literal["aap-labeling-task-workflow/v1"] = Field(alias="schema")
    task_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    actor_id: uuid.UUID
    expected_status: TaskStatus
    expected_review_round_id: uuid.UUID | None


class SubmitTask(_TaskScope):
    operation: Literal["submit"]
    at: AwareDatetime


class CompleteVideoReview(_TaskScope):
    operation: Literal["complete_video_review"]
    at: AwareDatetime


class SkipTask(_TaskScope):
    operation: Literal["skip"]
    at: AwareDatetime
    reason: StrictStr


class WithdrawTask(_TaskScope):
    operation: Literal["withdraw"]


class ReopenTask(_TaskScope):
    operation: Literal["reopen"]
    at: AwareDatetime


class ReopenVideoSegment(_TaskScope):
    operation: Literal["reopen_video_segment"]


class AcceptRejection(_TaskScope):
    operation: Literal["accept_rejection"]


class ClaimReview(_TaskScope):
    operation: Literal["claim_review"]
    at: AwareDatetime


class ApproveReview(_TaskScope):
    operation: Literal["approve_review"]
    at: AwareDatetime


class RejectReview(_TaskScope):
    operation: Literal["reject_review"]
    at: AwareDatetime
    reason_type: RejectReasonType
    reason: StrictStr | None


class RecordAnnotationActor(_TaskScope):
    operation: Literal["record_annotation_actor"]


class FreezeReviewEvidence(_TaskScope):
    operation: Literal["freeze_review_evidence"]
    contributor_ids: list[StrictStr]


class ClearReviewEvidence(_TaskScope):
    operation: Literal["clear_review_evidence"]


class CaptureFirstReviewSnapshot(_TaskScope):
    operation: Literal["capture_first_review_snapshot"]
    contributor_ids: list[StrictStr]


class RecordFirstReviewFact(_TaskScope):
    operation: Literal["record_first_review_fact"]
    at: AwareDatetime
    result: Literal["approved", "rejected"]
    contributor_ids: list[StrictStr]


TaskWorkflowCommand: TypeAlias = Annotated[
    SubmitTask
    | CompleteVideoReview
    | SkipTask
    | WithdrawTask
    | ReopenTask
    | ReopenVideoSegment
    | AcceptRejection
    | ClaimReview
    | ApproveReview
    | RejectReview
    | RecordAnnotationActor
    | FreezeReviewEvidence
    | ClearReviewEvidence
    | CaptureFirstReviewSnapshot
    | RecordFirstReviewFact,
    Field(discriminator="operation"),
]


class TaskWorkflowState(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    status: TaskStatus
    assignee_id: uuid.UUID | None
    assigned_at: datetime | None
    submitted_at: datetime | None
    reviewer_id: uuid.UUID | None
    reviewer_is_override: StrictBool
    reviewer_claimed_at: datetime | None
    reviewed_at: datetime | None
    reject_reason: StrictStr | None
    reject_reason_type: StrictStr | None
    skip_reason: StrictStr | None
    skipped_at: datetime | None
    reopened_count: StrictInt
    last_reopened_at: datetime | None
    review_round_id: uuid.UUID | None
    annotation_contributor_ids: object | None
    review_contributor_ids: object | None
    review_submitter_id: uuid.UUID | None
    first_review_eligible: StrictBool | None
    first_reviewed_at: datetime | None
    first_review_result: StrictStr | None
    first_review_contributor_ids: object | None


class TaskWorkflowData(_ClosedModel):
    task: TaskWorkflowState
    changed: StrictBool
    inherited_assignee_id: uuid.UUID | None = None
    original_reviewer_id: uuid.UUID | None = None


class TaskWorkflowError(_ClosedModel):
    code: StrictStr
    details: dict[str, object] | None = None


class TaskWorkflowReply(_ClosedModel):
    protocol: Literal["aap-labeling-task-workflow/v1"] = Field(alias="schema")
    ok: StrictBool
    request: TaskWorkflowCommand
    data: TaskWorkflowData | None = None
    error: TaskWorkflowError | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("Successful task workflow reply must contain data")
        elif self.error is None or self.data is not None:
            raise ValueError("Failed task workflow reply must contain only an error")
        return self


class TaskWorkflowFailure(Exception):
    def __init__(self, code: str, details: dict[str, object] | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


class TaskWorkflowUnavailable(Exception):
    """The Labeling task workflow owner returned an invalid private reply."""


_COMMAND_ADAPTER = TypeAdapter(TaskWorkflowCommand)
_REPLY_ADAPTER = TypeAdapter(TaskWorkflowReply)


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


def decode_task_workflow_command(value: object) -> TaskWorkflowCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskWorkflowUnavailable() from None


def decode_task_workflow_reply(
    request: TaskWorkflowCommand, value: object
) -> TaskWorkflowData:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskWorkflowUnavailable() from None
    if reply.request != request:
        raise TaskWorkflowUnavailable()
    if not reply.ok:
        assert reply.error is not None
        raise TaskWorkflowFailure(reply.error.code, reply.error.details)
    assert reply.data is not None
    return reply.data


def task_workflow_schema() -> dict[str, object]:
    """Return the closed command and reply schema."""
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TASK_WORKFLOW_PROTOCOL
    schema["x-postgresql-functions"] = ["aap_labeling_v1.task_workflow_command(jsonb)"]
    return schema


def task_workflow_command_json(request: TaskWorkflowCommand) -> str:
    """Serialize one already-validated operation with explicit JSON nulls."""
    validated = decode_task_workflow_command(
        request.model_dump_json(by_alias=True, exclude_none=False)
    )
    return validated.model_dump_json(by_alias=True, exclude_none=False)
