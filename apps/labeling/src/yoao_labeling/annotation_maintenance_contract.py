"""Closed draft and schema-cleanup operations owned by Labeling."""

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
from .batch_control_contract import BatchControlState
from .cross_frame_write_contract import CrossFrameAnnotationSnapshot
from .task_workflow_contract import TaskWorkflowState

ANNOTATION_MAINTENANCE_PROTOCOL = "aap-labeling-annotation-maintenance/v1"
ANNOTATION_MAINTENANCE_FUNCTION = (
    "aap_labeling_v1.annotation_maintenance_command(jsonb)"
)


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-maintenance/v1"] = Field(alias="schema")


class ReadDraft(_Command):
    operation: Literal["read_draft"]
    task_id: uuid.UUID
    user_id: uuid.UUID


class SaveDraft(_Command):
    operation: Literal["save_draft"]
    task_id: uuid.UUID
    user_id: uuid.UUID
    draft_id: uuid.UUID
    existing: StrictBool
    write_result: StrictBool
    result: dict[str, object]


class ReadCleanupCandidates(_Command):
    operation: Literal["read_cleanup_candidates"]
    project_id: uuid.UUID


class ObserveCleanupTasks(_Command):
    operation: Literal["observe_cleanup_tasks"]
    task_ids: list[uuid.UUID]


class CleanupTaskReference(_ClosedModel):
    id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID | None


class CleanupAnnotationReference(_ClosedModel):
    id: uuid.UUID
    task_id: uuid.UUID


class CleanupSchema(_Command):
    operation: Literal["cleanup_schema"]
    project_id: uuid.UUID
    class_names: list[StrictStr]
    attribute_keys: list[StrictStr]
    annotations: list[CleanupAnnotationReference]
    tasks: list[CleanupTaskReference]


AnnotationMaintenanceCommand = Annotated[
    ReadDraft | SaveDraft | ReadCleanupCandidates | ObserveCleanupTasks | CleanupSchema,
    Field(discriminator="operation"),
]


class MaintenanceDraft(_ClosedModel):
    id: uuid.UUID
    task_id: uuid.UUID
    annotation_id: uuid.UUID | None
    user_id: uuid.UUID
    result: object
    was_postponed: StrictBool
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CleanupAnnotation(CrossFrameAnnotationSnapshot):
    # Cleanup also accepts legacy non-object JSON without rewriting it.
    geometry: object
    attributes: object
    attributes_meta: object


class CleanupTask(TaskWorkflowState):
    dataset_item_id: uuid.UUID | None
    display_id: StrictStr
    file_name: StrictStr
    file_path: StrictStr
    file_type: StrictStr
    media_source_generation: StrictInt
    media_object_identity: object | None
    media_source_drifted: StrictBool
    tags: object
    assignee_is_override: StrictBool
    is_labeled: StrictBool
    overlap: StrictInt
    total_annotations: StrictInt
    total_predictions: StrictInt
    precomputed_agreement: float | None
    sequence_order: StrictInt | None
    thumbnail_path: StrictStr | None
    blurhash: StrictStr | None
    version: StrictInt
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CleanupBatch(BatchControlState):
    assigned_user_ids: object


class DraftData(_ClosedModel):
    operation: Literal["read_draft", "save_draft"]
    draft: MaintenanceDraft | None


class CleanupCandidates(_ClosedModel):
    operation: Literal["read_cleanup_candidates"]
    annotations: list[CleanupAnnotation]


class ObservedCleanupTasks(_ClosedModel):
    operation: Literal["observe_cleanup_tasks"]
    tasks: list[CleanupTaskReference]


class CleanupReport(_ClosedModel):
    orphan_annotations: StrictInt = Field(ge=0)
    orphan_attribute_keys: dict[StrictStr, Annotated[StrictInt, Field(ge=1)]]


class CleanupData(_ClosedModel):
    operation: Literal["cleanup_schema"]
    annotations: list[CleanupAnnotation]
    tasks: list[CleanupTask]
    batches: list[CleanupBatch]
    affected_task_ids: list[uuid.UUID]
    deactivated_task_ids: list[uuid.UUID]
    report: CleanupReport


AnnotationMaintenanceData = Annotated[
    DraftData | CleanupCandidates | ObservedCleanupTasks | CleanupData,
    Field(discriminator="operation"),
]


class _Reply(_ClosedModel):
    schema_: Literal["aap-labeling-annotation-maintenance/v1"] = Field(alias="schema")
    ok: StrictBool
    request: AnnotationMaintenanceCommand
    data: AnnotationMaintenanceData


class AnnotationMaintenanceUnavailable(RuntimeError):
    """The owner returned an invalid or mismatched maintenance reply."""


_COMMAND = TypeAdapter(AnnotationMaintenanceCommand)
_REPLY = TypeAdapter(_Reply)


def decode_annotation_maintenance_command(
    value: object,
) -> AnnotationMaintenanceCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationMaintenanceUnavailable() from None


def annotation_maintenance_command_json(command: AnnotationMaintenanceCommand) -> str:
    return decode_annotation_maintenance_command(
        command.model_dump_json(by_alias=True)
    ).model_dump_json(by_alias=True)


def _ids(rows) -> set[uuid.UUID]:
    ids = {row.id for row in rows}
    if len(ids) != len(rows):
        raise AnnotationMaintenanceUnavailable()
    return ids


def decode_annotation_maintenance_reply(
    request: AnnotationMaintenanceCommand, value: object
) -> AnnotationMaintenanceData:
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise AnnotationMaintenanceUnavailable() from None
    data = reply.data
    if not reply.ok or reply.request != request or data.operation != request.operation:
        raise AnnotationMaintenanceUnavailable()
    if isinstance(data, DraftData):
        if data.draft is not None and (
            data.draft.task_id != request.task_id
            or data.draft.user_id != request.user_id
            or (isinstance(request, SaveDraft) and data.draft.id != request.draft_id)
        ):
            raise AnnotationMaintenanceUnavailable()
        if isinstance(request, SaveDraft) and data.draft is None:
            raise AnnotationMaintenanceUnavailable()
    elif isinstance(data, CleanupCandidates):
        _ids(data.annotations)
        if any(
            row.project_id != request.project_id or not row.is_active
            for row in data.annotations
        ):
            raise AnnotationMaintenanceUnavailable()
    elif isinstance(data, ObservedCleanupTasks):
        if not _ids(data.tasks) <= set(request.task_ids):
            raise AnnotationMaintenanceUnavailable()
    else:
        assert isinstance(request, CleanupSchema)
        annotations = {row.id: row.task_id for row in request.annotations}
        task_ids = _ids(request.tasks)
        if (
            _ids(data.annotations) != set(annotations)
            or _ids(data.tasks) != task_ids
            or _ids(data.batches)
            != {row.batch_id for row in request.tasks if row.batch_id is not None}
            or not set(data.affected_task_ids) <= task_ids
            or not set(data.deactivated_task_ids) <= set(data.affected_task_ids)
            or any(
                row.project_id != request.project_id
                or row.task_id != annotations[row.id]
                for row in data.annotations
            )
            or any(
                row.project_id != request.project_id
                for row in data.tasks + data.batches
            )
        ):
            raise AnnotationMaintenanceUnavailable()
    return data


def annotation_maintenance_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = ANNOTATION_MAINTENANCE_PROTOCOL
    schema["x-postgresql-functions"] = [ANNOTATION_MAINTENANCE_FUNCTION]
    return schema
