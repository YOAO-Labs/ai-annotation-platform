"""Closed JSON contracts for Labeling discussion, feedback and event writes."""

from __future__ import annotations

import uuid
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
)

from .annotation_write_contract import _json_bytes

DISCUSSION_PROTOCOL = "aap-labeling-discussion/v1"
DISCUSSION_FUNCTION = "aap_labeling_v1.discussion_command(jsonb)"
TASK_EVENT_PROTOCOL = "aap-labeling-task-events/v1"
TASK_EVENT_FUNCTION = "aap_labeling_v1.task_event_command(jsonb)"


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _DiscussionCommand(_ClosedModel):
    schema_: Literal["aap-labeling-discussion/v1"] = Field(alias="schema")
    operation: StrictStr


FeedbackKind = Literal["issue", "comment", "reject", "bug"]
FeedbackAnchorType = Literal["project", "task", "annotation", "pixel", "point_cloud"]
FeedbackStatus = Literal["open", "resolved", "wont_fix"]


class CreateAnnotationComment(_DiscussionCommand):
    operation: Literal["create_comment"]
    comment_id: uuid.UUID
    annotation_id: uuid.UUID
    project_id: uuid.UUID
    author_id: uuid.UUID
    body: StrictStr = Field(max_length=4000)
    mentions: list[dict[str, object]]
    attachments: list[dict[str, object]]
    canvas_drawing: dict[str, object] | None
    anchor: dict[str, object] | None


class LockAnnotationComment(_DiscussionCommand):
    operation: Literal["lock_comment"]
    comment_id: uuid.UUID


class UpdateAnnotationComment(_DiscussionCommand):
    operation: Literal["update_comment"]
    comment_id: uuid.UUID
    body: StrictStr | None = Field(default=None, max_length=4000)
    is_resolved: StrictBool | None = None


class SoftDeleteAnnotationComment(_DiscussionCommand):
    operation: Literal["soft_delete_comment"]
    comment_id: uuid.UUID


class ClearAnnotationCommentAttachments(_DiscussionCommand):
    operation: Literal["clear_comment_attachments"]
    comment_id: uuid.UUID


class DeleteCommentsForTasks(_DiscussionCommand):
    operation: Literal["delete_comments_for_tasks"]
    task_ids: list[uuid.UUID]


class CreateAnnotationFeedback(_DiscussionCommand):
    operation: Literal["create_feedback"]
    feedback_id: uuid.UUID
    author_id: uuid.UUID
    kind: FeedbackKind
    anchor_type: FeedbackAnchorType
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    annotation_id: uuid.UUID | None
    anchor_position: dict[str, object] | None
    severity: StrictStr | None = Field(max_length=16)
    title: StrictStr | None = Field(max_length=500)
    body: StrictStr
    attachments: list[dict[str, object]]
    thread_parent_id: uuid.UUID | None
    canvas_drawing: dict[str, object] | None
    mentions: list[dict[str, object]]


class PatchAnnotationFeedback(_DiscussionCommand):
    operation: Literal["patch_feedback"]
    feedback_id: uuid.UUID
    actor_id: uuid.UUID
    status: FeedbackStatus | None = None
    resolved_at: AwareDatetime | None = None
    severity: StrictStr | None = Field(default=None, max_length=16)
    title: StrictStr | None = Field(default=None, max_length=500)
    body: StrictStr | None = None
    mentions: list[dict[str, object]] | None = None


class SoftDeleteAnnotationFeedback(_DiscussionCommand):
    operation: Literal["soft_delete_feedback"]
    feedback_id: uuid.UUID


class LockAnnotationFeedbackLineage(_DiscussionCommand):
    operation: Literal["lock_feedback_lineage"]
    feedback_ids: list[uuid.UUID] = Field(min_length=1)


class MirrorBugReportFeedback(_DiscussionCommand):
    operation: Literal["mirror_bug_report"]
    feedback_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    author_id: uuid.UUID
    severity: StrictStr | None = Field(max_length=16)
    title: StrictStr | None = Field(max_length=500)
    body: StrictStr


class MirrorAnnotationCommentFeedback(_DiscussionCommand):
    operation: Literal["mirror_annotation_comment"]
    feedback_id: uuid.UUID
    comment_id: uuid.UUID
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    author_id: uuid.UUID
    body: StrictStr
    attachments: list[dict[str, object]]
    mentions: list[dict[str, object]]


class MirrorTaskRejectFeedback(_DiscussionCommand):
    operation: Literal["mirror_task_reject"]
    feedback_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    reviewer_id: uuid.UUID
    severity: StrictStr | None = Field(max_length=16)
    body: StrictStr


DiscussionCommand: TypeAlias = Annotated[
    CreateAnnotationComment
    | LockAnnotationComment
    | UpdateAnnotationComment
    | SoftDeleteAnnotationComment
    | ClearAnnotationCommentAttachments
    | DeleteCommentsForTasks
    | CreateAnnotationFeedback
    | PatchAnnotationFeedback
    | LockAnnotationFeedbackLineage
    | SoftDeleteAnnotationFeedback
    | MirrorBugReportFeedback
    | MirrorAnnotationCommentFeedback
    | MirrorTaskRejectFeedback,
    Field(discriminator="operation"),
]


class _TaskEventCommand(_ClosedModel):
    schema_: Literal["aap-labeling-task-events/v1"] = Field(alias="schema")
    operation: StrictStr


class TaskEventInsertRow(_ClosedModel):
    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    kind: Literal["annotate", "review"]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: StrictInt = Field(ge=0, le=14_400_000)
    annotation_count: StrictInt = Field(ge=0, le=1_000_000)
    was_rejected: StrictBool
    collector_version: StrictStr | None = Field(max_length=32)
    collection_source: Literal["session", "legacy"]
    collection_coverage: Literal["qualified", "partial", "unverified_collection"]


class ReadTaskEvents(_TaskEventCommand):
    operation: Literal["read_ids"]
    event_ids: list[uuid.UUID]


class InsertTaskEvents(_TaskEventCommand):
    operation: Literal["insert"]
    rows: list[TaskEventInsertRow] = Field(max_length=200)


TaskEventCommand: TypeAlias = Annotated[
    ReadTaskEvents | InsertTaskEvents, Field(discriminator="operation")
]


class AnnotationCommentRow(_ClosedModel):
    id: uuid.UUID
    annotation_id: uuid.UUID
    project_id: uuid.UUID | None
    author_id: uuid.UUID
    body: StrictStr
    is_resolved: StrictBool
    is_active: StrictBool
    mentions: list[dict[str, object]]
    attachments: list[dict[str, object]]
    canvas_drawing: object | None
    anchor: object | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AnnotationFeedbackRow(_ClosedModel):
    id: uuid.UUID
    kind: StrictStr
    anchor_type: StrictStr
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    annotation_id: uuid.UUID | None
    anchor_position: object | None
    status: StrictStr
    severity: StrictStr | None
    title: StrictStr | None
    body: StrictStr
    author_id: uuid.UUID
    attachments: list[dict[str, object]]
    mentions: list[dict[str, object]]
    canvas_drawing: object | None
    thread_parent_id: uuid.UUID | None
    is_active: StrictBool
    resolved_at: AwareDatetime | None
    resolved_by_id: uuid.UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TaskEventRow(TaskEventInsertRow):
    created_at: AwareDatetime
    reject_reason_type: StrictStr | None = None


class DiscussionData(_ClosedModel):
    comment: AnnotationCommentRow | None = None
    feedback: AnnotationFeedbackRow | None = None
    feedbacks: list[AnnotationFeedbackRow] | None = None
    event_rows: list[TaskEventRow] | None = None
    inserted: StrictInt | None = None
    conflicts: list[uuid.UUID] | None = None
    deleted_comments: StrictInt | None = None


class _DiscussionReply(_ClosedModel):
    schema_: Literal["aap-labeling-discussion/v1"] = Field(alias="schema")
    ok: StrictBool
    request: DiscussionCommand
    data: DiscussionData


class _TaskEventData(_ClosedModel):
    event_rows: list[TaskEventRow] | None = None
    inserted: StrictInt | None = None
    conflicts: list[uuid.UUID] | None = None


class _TaskEventReply(_ClosedModel):
    schema_: Literal["aap-labeling-task-events/v1"] = Field(alias="schema")
    ok: StrictBool
    request: TaskEventCommand
    data: _TaskEventData


class LabelingDiscussionUnavailable(RuntimeError):
    """The Labeling discussion owner returned invalid or mismatched data."""


class LabelingTaskEventsUnavailable(RuntimeError):
    """The Labeling task-event owner returned invalid or mismatched data."""


_DISCUSSION_COMMAND = TypeAdapter(DiscussionCommand)
_DISCUSSION_REPLY = TypeAdapter(_DiscussionReply)
_TASK_EVENT_COMMAND = TypeAdapter(TaskEventCommand)
_TASK_EVENT_REPLY = TypeAdapter(_TaskEventReply)


def decode_discussion_command(value: object) -> DiscussionCommand:
    try:
        return _DISCUSSION_COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingDiscussionUnavailable() from None


def discussion_command_json(command: DiscussionCommand) -> str:
    return decode_discussion_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def decode_discussion_reply(
    request: DiscussionCommand, value: object
) -> DiscussionData:
    try:
        reply = _DISCUSSION_REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingDiscussionUnavailable() from None
    if not reply.ok or reply.request != request:
        raise LabelingDiscussionUnavailable()
    expected = request.comment_id if hasattr(request, "comment_id") else None
    if isinstance(request, CreateAnnotationComment):
        if reply.data.comment is None or reply.data.comment.id != expected:
            raise LabelingDiscussionUnavailable()
    elif isinstance(
        request,
        (LockAnnotationComment, UpdateAnnotationComment, SoftDeleteAnnotationComment),
    ):
        if reply.data.comment is None or reply.data.comment.id != expected:
            raise LabelingDiscussionUnavailable()
    elif isinstance(request, ClearAnnotationCommentAttachments):
        if reply.data.comment is not None:
            raise LabelingDiscussionUnavailable()
    elif isinstance(request, DeleteCommentsForTasks):
        if reply.data.deleted_comments is None or reply.data.comment is not None:
            raise LabelingDiscussionUnavailable()
    elif isinstance(request, CreateAnnotationFeedback):
        if reply.data.feedback is None or reply.data.feedback.id != request.feedback_id:
            raise LabelingDiscussionUnavailable()
    elif isinstance(request, LockAnnotationFeedbackLineage):
        if reply.data.feedbacks is None or {
            row.id for row in reply.data.feedbacks
        } != set(request.feedback_ids):
            raise LabelingDiscussionUnavailable()
    elif isinstance(request, PatchAnnotationFeedback | SoftDeleteAnnotationFeedback):
        if reply.data.feedback is None or reply.data.feedback.id != request.feedback_id:
            raise LabelingDiscussionUnavailable()
    elif isinstance(
        request,
        MirrorBugReportFeedback
        | MirrorAnnotationCommentFeedback
        | MirrorTaskRejectFeedback,
    ):
        if reply.data.feedback is None or reply.data.feedback.id != request.feedback_id:
            raise LabelingDiscussionUnavailable()
    return reply.data


def decode_task_event_command(value: object) -> TaskEventCommand:
    try:
        return _TASK_EVENT_COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingTaskEventsUnavailable() from None


def task_event_command_json(command: TaskEventCommand) -> str:
    return decode_task_event_command(
        command.model_dump_json(by_alias=True, exclude_unset=True)
    ).model_dump_json(by_alias=True, exclude_unset=True)


def decode_task_event_reply(request: TaskEventCommand, value: object) -> _TaskEventData:
    try:
        reply = _TASK_EVENT_REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise LabelingTaskEventsUnavailable() from None
    if not reply.ok or reply.request != request:
        raise LabelingTaskEventsUnavailable()
    if isinstance(request, ReadTaskEvents):
        if reply.data.event_rows is None or reply.data.inserted is not None:
            raise LabelingTaskEventsUnavailable()
        row_ids = [row.id for row in reply.data.event_rows]
        if len(row_ids) != len(set(row_ids)) or set(row_ids) - set(request.event_ids):
            raise LabelingTaskEventsUnavailable()
    else:
        requested_ids = {row.id for row in request.rows}
        if (
            reply.data.inserted is None
            or reply.data.event_rows is not None
            or reply.data.conflicts is None
            or reply.data.inserted > len(requested_ids)
            or set(reply.data.conflicts) - requested_ids
        ):
            raise LabelingTaskEventsUnavailable()
    return reply.data


def discussion_schema() -> dict[str, object]:
    schema = _DISCUSSION_REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = DISCUSSION_PROTOCOL
    schema["x-postgresql-functions"] = [DISCUSSION_FUNCTION]
    schema["x-postgresql-relations"] = [
        "aap_labeling_v1.annotation_comment_facts",
        "aap_labeling_v1.annotation_feedback_facts",
        "aap_labeling_v1.task_event_facts",
        "public.v_annotation_feedback_unified",
    ]
    return schema


def task_event_schema() -> dict[str, object]:
    schema = _TASK_EVENT_REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TASK_EVENT_PROTOCOL
    schema["x-postgresql-functions"] = [TASK_EVENT_FUNCTION]
    schema["x-postgresql-relations"] = ["aap_labeling_v1.task_event_facts"]
    return schema
