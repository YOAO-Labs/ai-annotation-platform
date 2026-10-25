"""Closed operations for source-less Labeling Task maintenance."""

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
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .annotation_write_contract import _json_bytes
from .batch_control_contract import BatchControlState

TASK_MAINTENANCE_PROTOCOL = "aap-labeling-task-maintenance/v1"
TASK_MAINTENANCE_FUNCTION = "aap_labeling_v1.task_maintenance_command(jsonb)"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Command(_Closed):
    schema_: Literal["aap-labeling-task-maintenance/v1"] = Field(alias="schema")
    operation: str


class ReadSourceLessTaskCandidates(_Command):
    operation: Literal["read_source_less_task_candidates"]
    project_id: uuid.UUID


class LockSourceLessTasks(_Command):
    operation: Literal["lock_source_less_tasks"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def unique_tasks(self):
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("task_ids must be unique")
        return self


class DeleteSourceLessChildren(_Command):
    operation: Literal["delete_source_less_children"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def unique_tasks(self):
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("task_ids must be unique")
        return self


class DeleteSourceLessTaskRecords(_Command):
    operation: Literal["delete_source_less_task_records"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def unique_tasks(self):
        if len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError("task_ids must be unique")
        return self


class ReadSourceLessProjectCounters(_Command):
    operation: Literal["read_source_less_project_counters"]
    project_id: uuid.UUID


class ReadSourceLessProjectBatches(_Command):
    operation: Literal["read_source_less_project_batches"]
    project_id: uuid.UUID


class ReadSourceLessBatchCounters(_Command):
    operation: Literal["read_source_less_batch_counters"]
    project_id: uuid.UUID
    batch_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def unique_batches(self):
        if len(self.batch_ids) != len(set(self.batch_ids)):
            raise ValueError("batch_ids must be unique")
        return self


class BatchCounterPatch(_Closed):
    batch_id: uuid.UUID
    total_tasks_set: StrictBool
    total_tasks: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    completed_tasks_set: StrictBool
    completed_tasks: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    review_tasks_set: StrictBool
    review_tasks: StrictInt | None = Field(default=None, ge=0, le=2_147_483_647)
    updated_at_set: StrictBool
    updated_at: AwareDatetime | None

    @model_validator(mode="after")
    def check_timestamp(self):
        for name in ("total_tasks", "completed_tasks", "review_tasks"):
            if getattr(self, f"{name}_set") != (getattr(self, name) is not None):
                raise ValueError(f"{name}_set must match the counter value")
        if self.updated_at_set != (self.updated_at is not None):
            raise ValueError("updated_at_set must match the timestamp value")
        return self


class WriteSourceLessBatchCounters(_Command):
    operation: Literal["write_source_less_batch_counters"]
    project_id: uuid.UUID
    batches: list[BatchCounterPatch]

    @model_validator(mode="after")
    def unique_batches(self):
        ids = [batch.batch_id for batch in self.batches]
        if len(ids) != len(set(ids)):
            raise ValueError("batch_ids must be unique")
        return self


TaskMaintenanceCommand = Annotated[
    ReadSourceLessTaskCandidates
    | LockSourceLessTasks
    | DeleteSourceLessChildren
    | DeleteSourceLessTaskRecords
    | ReadSourceLessProjectCounters
    | ReadSourceLessProjectBatches
    | ReadSourceLessBatchCounters
    | WriteSourceLessBatchCounters,
    Field(discriminator="operation"),
]


class _Data(_Closed):
    operation: str


class SourceLessTaskIds(_Data):
    operation: Literal["read_source_less_task_candidates", "lock_source_less_tasks"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]


class DeletedSourceLessChildren(_Data):
    operation: Literal["delete_source_less_children"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]
    annotation_count: StrictInt = Field(ge=0)


class DeletedSourceLessTaskRecords(_Data):
    operation: Literal["delete_source_less_task_records"]
    project_id: uuid.UUID
    task_ids: list[uuid.UUID]
    deleted_count: StrictInt = Field(ge=0)


class SourceLessProjectCounters(_Data):
    operation: Literal["read_source_less_project_counters"]
    project_id: uuid.UUID
    total_tasks: StrictInt = Field(ge=0)
    completed_tasks: StrictInt = Field(ge=0)
    review_tasks: StrictInt = Field(ge=0)
    in_progress_tasks: StrictInt = Field(ge=0)


class SourceLessProjectBatches(_Data):
    operation: Literal["read_source_less_project_batches"]
    project_id: uuid.UUID
    batches: list[BatchControlState]


class SourceLessBatchCounters(_Data):
    operation: Literal["read_source_less_batch_counters"]
    project_id: uuid.UUID
    batches: list["SourceLessBatchCounter"]


class SourceLessBatchCounter(_Closed):
    batch_id: uuid.UUID
    total_tasks: StrictInt = Field(ge=0)
    completed_tasks: StrictInt = Field(ge=0)
    review_tasks: StrictInt = Field(ge=0)


class BatchCounterSnapshot(_Closed):
    batch_id: uuid.UUID
    project_id: uuid.UUID
    total_tasks: StrictInt = Field(ge=0)
    completed_tasks: StrictInt = Field(ge=0)
    review_tasks: StrictInt = Field(ge=0)
    updated_at: AwareDatetime


class WrittenSourceLessBatchCounters(_Data):
    operation: Literal["write_source_less_batch_counters"]
    project_id: uuid.UUID
    batches: list[BatchCounterSnapshot]


TaskMaintenanceData = Annotated[
    SourceLessTaskIds
    | DeletedSourceLessChildren
    | DeletedSourceLessTaskRecords
    | SourceLessProjectCounters
    | SourceLessProjectBatches
    | SourceLessBatchCounters
    | WrittenSourceLessBatchCounters,
    Field(discriminator="operation"),
]


class _Reply(_Closed):
    schema_: Literal["aap-labeling-task-maintenance/v1"] = Field(alias="schema")
    ok: StrictBool
    request: TaskMaintenanceCommand
    data: TaskMaintenanceData


class TaskMaintenanceUnavailable(RuntimeError):
    """The Labeling Task maintenance owner returned an invalid reply."""


_COMMAND = TypeAdapter(TaskMaintenanceCommand)
_REPLY = TypeAdapter(_Reply)


def decode_task_maintenance_command(value: object) -> TaskMaintenanceCommand:
    try:
        return _COMMAND.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskMaintenanceUnavailable() from None


def task_maintenance_command_json(request: TaskMaintenanceCommand) -> str:
    validated = decode_task_maintenance_command(request.model_dump_json(by_alias=True))
    return validated.model_dump_json(by_alias=True)


def decode_task_maintenance_reply(
    request: TaskMaintenanceCommand, value: object
) -> TaskMaintenanceData:
    try:
        reply = _REPLY.validate_json(_json_bytes(value))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        raise TaskMaintenanceUnavailable() from None
    if not reply.ok or reply.request != request:
        raise TaskMaintenanceUnavailable()
    data = reply.data
    if data.project_id != request.project_id:
        raise TaskMaintenanceUnavailable()
    expected_types = {
        ReadSourceLessTaskCandidates: SourceLessTaskIds,
        LockSourceLessTasks: SourceLessTaskIds,
        DeleteSourceLessChildren: DeletedSourceLessChildren,
        DeleteSourceLessTaskRecords: DeletedSourceLessTaskRecords,
        ReadSourceLessProjectCounters: SourceLessProjectCounters,
        ReadSourceLessProjectBatches: SourceLessProjectBatches,
        ReadSourceLessBatchCounters: SourceLessBatchCounters,
        WriteSourceLessBatchCounters: WrittenSourceLessBatchCounters,
    }
    if type(data) is not expected_types[type(request)]:
        raise TaskMaintenanceUnavailable()
    if isinstance(data, SourceLessTaskIds):
        if isinstance(request, LockSourceLessTasks) and (
            data.task_ids != sorted(data.task_ids, key=str)
            or not set(data.task_ids).issubset(request.task_ids)
        ):
            raise TaskMaintenanceUnavailable()
        if len(data.task_ids) != len(set(data.task_ids)) or data.task_ids != sorted(
            data.task_ids, key=str
        ):
            raise TaskMaintenanceUnavailable()
    elif isinstance(data, DeletedSourceLessChildren):
        assert isinstance(request, DeleteSourceLessChildren)
        if data.task_ids != request.task_ids:
            raise TaskMaintenanceUnavailable()
    elif isinstance(data, DeletedSourceLessTaskRecords):
        assert isinstance(request, DeleteSourceLessTaskRecords)
        if data.task_ids != request.task_ids:
            raise TaskMaintenanceUnavailable()
        if data.deleted_count > len(request.task_ids):
            raise TaskMaintenanceUnavailable()
    elif isinstance(data, SourceLessProjectBatches):
        ids = [batch.id for batch in data.batches]
        if len(ids) != len(set(ids)) or any(
            batch.project_id != request.project_id for batch in data.batches
        ):
            raise TaskMaintenanceUnavailable()
    elif isinstance(data, SourceLessBatchCounters):
        assert isinstance(request, ReadSourceLessBatchCounters)
        ids = [batch.batch_id for batch in data.batches]
        if ids != sorted(request.batch_ids, key=str) or len(ids) != len(set(ids)):
            raise TaskMaintenanceUnavailable()
    elif isinstance(data, SourceLessProjectCounters):
        if not isinstance(request, ReadSourceLessProjectCounters):
            raise TaskMaintenanceUnavailable()
    else:
        assert isinstance(request, WriteSourceLessBatchCounters)
        requested = {batch.batch_id for batch in request.batches}
        returned = {batch.batch_id for batch in data.batches}
        if len(requested) != len(request.batches) or returned != requested:
            raise TaskMaintenanceUnavailable()
        if any(batch.project_id != request.project_id for batch in data.batches):
            raise TaskMaintenanceUnavailable()
    return data


def task_maintenance_schema() -> dict[str, object]:
    schema = _REPLY.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = TASK_MAINTENANCE_PROTOCOL
    schema["x-postgresql-functions"] = [TASK_MAINTENANCE_FUNCTION]
    return schema
