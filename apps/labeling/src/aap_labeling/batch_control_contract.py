"""Closed Labeling batch and inherited Task control commands."""

from __future__ import annotations

import json
import uuid
from datetime import date
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

BATCH_CONTROL_PROTOCOL = "aap-labeling-batch-control/v1"
BATCH_CONTROL_DATA_SCHEMA = "aap-labeling-batch-control-data/v1"
BATCH_CONTROL_FUNCTION = "aap_labeling_v1.batch_control_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    protocol: Literal["aap-labeling-batch-control/v1"] = Field(alias="schema")


class SequenceOrder(_ClosedModel):
    task_id: uuid.UUID
    sequence_order: StrictInt


class CreateBatch(_Command):
    operation: Literal["create_batch"]
    batch_id: uuid.UUID
    project_id: uuid.UUID
    dataset_id: uuid.UUID | None
    display_id: StrictStr
    name: StrictStr
    description: StrictStr
    priority: StrictInt
    deadline: date | None
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    created_by: uuid.UUID | None


class UpdateBatch(_Command):
    operation: Literal["update_batch"]
    batch_id: uuid.UUID
    project_id: uuid.UUID | None
    name_set: StrictBool
    name: StrictStr | None
    description_set: StrictBool
    description: StrictStr | None
    priority_set: StrictBool
    priority: StrictInt | None
    deadline_set: StrictBool
    deadline: date | None
    annotator_set: StrictBool
    annotator_id: uuid.UUID | None
    reviewer_set: StrictBool
    reviewer_id: uuid.UUID | None
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class TransitionBatch(_Command):
    operation: Literal["transition_batch"]
    batch_id: uuid.UUID
    expected_status: StrictStr
    target_status: StrictStr
    actor_id: uuid.UUID | None


class SetBatchAdminLock(_Command):
    operation: Literal["set_batch_admin_lock"]
    batch_id: uuid.UUID
    locked: StrictBool
    locked_at: AwareDatetime | None
    reason: StrictStr | None
    actor_id: uuid.UUID | None


class PrepareBatchCleanup(_Command):
    operation: Literal["prepare_batch_cleanup"]
    batch_id: uuid.UUID
    protect_task_work: StrictBool = False
    expected_non_pending: StrictInt | None = None
    expected_predicted: StrictInt | None = None
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class ResetBatchToDraft(_Command):
    operation: Literal["reset_batch_to_draft"]
    batch_id: uuid.UUID
    expected_status: StrictStr


class DeleteBatch(_Command):
    operation: Literal["delete_batch"]
    batch_id: uuid.UUID
    project_id: uuid.UUID


class ReassignBatchTasks(_Command):
    operation: Literal["reassign_batch_tasks"]
    batch_id: uuid.UUID
    project_id: uuid.UUID
    default_batch_id: uuid.UUID | None
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class AssignTasks(_Command):
    operation: Literal["assign_tasks"]
    batch_id: uuid.UUID
    task_ids: list[uuid.UUID]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class SetSequenceOrders(_Command):
    operation: Literal["set_sequence_orders"]
    sequence_orders: list[SequenceOrder]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class BatchAssignment(_ClosedModel):
    batch_id: uuid.UUID
    expected_annotator_id: uuid.UUID | None
    expected_reviewer_id: uuid.UUID | None
    annotator_set: StrictBool
    annotator_id: uuid.UUID | None
    reviewer_set: StrictBool
    reviewer_id: uuid.UUID | None


class ApplyBatchAssignments(_Command):
    operation: Literal["apply_batch_assignments"]
    project_id: uuid.UUID
    assignments: list[BatchAssignment]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_ids(self):
        if len({item.batch_id for item in self.assignments}) != len(self.assignments):
            raise ValueError("batch IDs must be unique")
        return self


class TaskAssignment(_ClosedModel):
    task_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    expected_status: StrictStr
    expected_assignee_id: uuid.UUID | None
    expected_assignee_is_override: StrictBool
    expected_reviewer_id: uuid.UUID | None
    expected_reviewer_is_override: StrictBool
    expected_reviewer_claimed_at: AwareDatetime | None
    annotator_set: StrictBool
    annotator_id: uuid.UUID | None
    assigned_at: AwareDatetime | None
    reviewer_set: StrictBool
    reviewer_id: uuid.UUID | None


class ApplyTaskAssignments(_Command):
    operation: Literal["apply_task_assignments"]
    assignments: list[TaskAssignment]

    @model_validator(mode="after")
    def check_ids(self):
        if len({item.task_id for item in self.assignments}) != len(self.assignments):
            raise ValueError("task IDs must be unique")
        return self


class BatchStatusItem(_ClosedModel):
    batch_id: uuid.UUID
    expected_status: StrictStr


class BulkBatchStatus(_Command):
    operation: Literal["bulk_archive", "bulk_activate"]
    project_id: uuid.UUID
    batches: list[BatchStatusItem]


class BulkDeleteBatchItem(_ClosedModel):
    batch_id: uuid.UUID


class BulkDeleteBatches(_Command):
    operation: Literal["bulk_delete"]
    project_id: uuid.UUID
    default_batch_id: uuid.UUID | None
    batches: list[BulkDeleteBatchItem]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class BulkReviewBatchItem(_ClosedModel):
    batch_id: uuid.UUID
    expected_status: StrictStr


class BulkApproveBatches(_Command):
    operation: Literal["bulk_approve"]
    project_id: uuid.UUID
    actor_id: uuid.UUID
    batches: list[BulkReviewBatchItem]


class BulkRejectBatches(_Command):
    operation: Literal["bulk_reject"]
    project_id: uuid.UUID
    actor_id: uuid.UUID
    reviewer_id: uuid.UUID
    feedback: StrictStr
    reviewed_at: AwareDatetime
    batches: list[BulkReviewBatchItem]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class RejectBatch(_Command):
    operation: Literal["reject_batch"]
    batch_id: uuid.UUID
    expected_status: StrictStr
    actor_id: uuid.UUID
    reviewer_id: uuid.UUID
    feedback: StrictStr
    reviewed_at: AwareDatetime
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class RecalculateBatchCounters(_Command):
    operation: Literal["recalculate_batch_counters"]
    batch_id: uuid.UUID


class MarkPreannotated(_Command):
    operation: Literal["mark_preannotated"]
    batch_id: uuid.UUID
    expected_status: Literal["active"]


class CheckAutoTransition(_Command):
    operation: Literal["check_auto_transition"]
    batch_id: uuid.UUID
    expected_status: StrictStr


class ReactivatePreannotated(_Command):
    operation: Literal["reactivate_preannotated"]
    batch_id: uuid.UUID
    expected_status: StrictStr


class ClearAnnotationContributors(_Command):
    operation: Literal["clear_annotation_contributors"]
    task_ids: list[uuid.UUID]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


class DeactivatePredictionAnnotations(_Command):
    operation: Literal["deactivate_prediction_annotations"]
    task_ids: list[uuid.UUID]
    tracked_annotation_ids: list[uuid.UUID] = Field(default_factory=list)


class DetachPredictionParents(_Command):
    operation: Literal["detach_prediction_parents"]
    task_ids: list[uuid.UUID] | None = None
    batch_id: uuid.UUID | None = None
    tracked_annotation_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_scope(self):
        if (self.task_ids is None) == (self.batch_id is None):
            raise ValueError("exactly one prediction-detach scope is required")
        if self.task_ids is not None and len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("task IDs must be unique")
        return self


class ClearPredictionProjection(_Command):
    operation: Literal["clear_prediction_projection"]
    task_ids: list[uuid.UUID]
    tracked_task_ids: list[uuid.UUID] = Field(default_factory=list)


BatchControlCommand: TypeAlias = Annotated[
    CreateBatch
    | UpdateBatch
    | TransitionBatch
    | SetBatchAdminLock
    | PrepareBatchCleanup
    | ResetBatchToDraft
    | DeleteBatch
    | ReassignBatchTasks
    | AssignTasks
    | SetSequenceOrders
    | ApplyBatchAssignments
    | ApplyTaskAssignments
    | BulkBatchStatus
    | BulkDeleteBatches
    | BulkApproveBatches
    | BulkRejectBatches
    | RejectBatch
    | RecalculateBatchCounters
    | MarkPreannotated
    | CheckAutoTransition
    | ReactivatePreannotated
    | ClearAnnotationContributors
    | DeactivatePredictionAnnotations
    | DetachPredictionParents
    | ClearPredictionProjection,
    Field(discriminator="operation"),
]


class TaskControlState(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None
    status: StrictStr
    assignee_id: uuid.UUID | None
    assignee_is_override: StrictBool
    assigned_at: AwareDatetime | None
    reviewer_id: uuid.UUID | None
    reviewer_is_override: StrictBool
    reviewer_claimed_at: AwareDatetime | None
    review_contributor_ids: object | None
    review_submitter_id: uuid.UUID | None
    annotation_contributor_ids: object | None
    total_annotations: StrictInt
    total_predictions: StrictInt
    is_labeled: StrictBool
    sequence_order: StrictInt | None
    updated_at: AwareDatetime


class AnnotationControlState(_ClosedModel):
    id: uuid.UUID
    is_active: StrictBool
    parent_prediction_id: uuid.UUID | None
    updated_at: AwareDatetime


class BatchControlState(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    dataset_id: uuid.UUID | None
    display_id: StrictStr
    name: StrictStr
    description: StrictStr
    status: StrictStr
    priority: StrictInt
    deadline: date | None
    assigned_user_ids: list[StrictStr]
    total_tasks: StrictInt
    completed_tasks: StrictInt
    review_tasks: StrictInt
    approved_tasks: StrictInt
    rejected_tasks: StrictInt
    created_by: uuid.UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    review_feedback: StrictStr | None
    reviewed_at: AwareDatetime | None
    reviewed_by: uuid.UUID | None
    annotator_id: uuid.UUID | None
    reviewer_id: uuid.UUID | None
    admin_locked: StrictBool
    admin_lock_reason: StrictStr | None
    admin_locked_at: AwareDatetime | None
    admin_locked_by: uuid.UUID | None


class BatchControlResult(_ClosedModel):
    batch_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    batch_ids: list[uuid.UUID] = Field(default_factory=list)
    deleted_batch_ids: list[uuid.UUID] = Field(default_factory=list)
    cleared_assignee_task_ids: list[uuid.UUID] = Field(default_factory=list)
    cleared_reviewer_task_ids: list[uuid.UUID] = Field(default_factory=list)
    task_ids: list[uuid.UUID] = Field(default_factory=list)
    batches: list[BatchControlState] = Field(default_factory=list)
    tasks: list[TaskControlState] = Field(default_factory=list)
    annotations: list[AnnotationControlState] = Field(default_factory=list)
    changed: StrictBool = False
    from_status: StrictStr | None = None
    status: StrictStr | None = None
    affected: StrictInt = 0
    tasks_reset: StrictInt = 0
    total_tasks: StrictInt = 0
    completed_tasks: StrictInt = 0
    review_tasks: StrictInt = 0
    predictions: StrictInt = 0
    failed_predictions: StrictInt = 0
    ai_annotations_deactivated: StrictInt = 0


class BatchControlError(_ClosedModel):
    code: StrictStr
    details: dict[str, object] | None = None


class BatchControlReply(_ClosedModel):
    protocol: Literal["aap-labeling-batch-control-data/v1"] = Field(alias="schema")
    ok: StrictBool
    request: BatchControlCommand
    data: BatchControlResult | None = None
    error: BatchControlError | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("Successful batch control reply must contain data")
        elif self.error is None or self.data is not None:
            raise ValueError("Failed batch control reply must contain only an error")
        return self


class BatchControlFailure(Exception):
    def __init__(self, code: str, details: dict[str, object] | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


class BatchControlUnavailable(Exception):
    """The Labeling batch-control owner returned an invalid private reply."""


_COMMAND_ADAPTER = TypeAdapter(BatchControlCommand)
_REPLY_ADAPTER = TypeAdapter(BatchControlReply)


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


def decode_batch_control_command(value: object) -> BatchControlCommand:
    try:
        return _COMMAND_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise BatchControlUnavailable() from None


def decode_batch_control_reply(
    request: BatchControlCommand, value: object
) -> BatchControlResult:
    try:
        reply = _REPLY_ADAPTER.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise BatchControlUnavailable() from None
    if reply.request != request:
        raise BatchControlUnavailable()
    if not reply.ok:
        assert reply.error is not None
        raise BatchControlFailure(reply.error.code, reply.error.details)
    assert reply.data is not None
    return reply.data


def batch_control_schema() -> dict[str, object]:
    schema = _REPLY_ADAPTER.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = BATCH_CONTROL_PROTOCOL
    schema["x-postgresql-functions"] = [BATCH_CONTROL_FUNCTION]
    return schema


def batch_control_command_json(request: BatchControlCommand) -> str:
    validated = decode_batch_control_command(
        request.model_dump_json(by_alias=True, exclude_none=False)
    )
    return validated.model_dump_json(by_alias=True, exclude_none=False)
