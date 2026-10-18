"""Language-neutral bindings for the initial task creation data capabilities."""

from __future__ import annotations

from typing import Annotated, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Count = Annotated[int, Field(strict=True, ge=0)]
CreationMode = Literal["generic", "pointcloud"]
JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class TaskCreationFailure(Exception):
    def __init__(self, code: str, facts: dict[str, object] | None = None):
        super().__init__(code)
        self.code = code
        self.facts = facts or {}


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreationKindFacts(_WireModel):
    project_data_type: str | None
    dataset_data_type: str | None
    project_scene_mode: StrictBool
    dataset_has_scenes: StrictBool


class CreationError(_WireModel):
    code: str
    details: CreationKindFacts | None = None


class _Reply(_WireModel):
    schema_version: str = Field(alias="schema")
    ok: StrictBool
    data: dict[str, object] | None = None
    error: CreationError | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("Successful creation reply must have only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("Failed creation reply must have only error")
        elif (self.error.code in {"media_kind_mismatch", "scene_kind_mismatch"}) != (
            self.error.details is not None
        ):
            raise ValueError("Kind mismatch details do not match the error code")
        return self


ReplyData = TypeVar("ReplyData", bound=BaseModel)


def decode_creation_reply(
    payload: object, schema: str, model: type[ReplyData]
) -> ReplyData:
    reply = _Reply.model_validate(payload)
    if reply.schema_version != schema:
        raise ValueError("Unexpected task creation reply schema")
    if not reply.ok:
        assert reply.error is not None
        raise TaskCreationFailure(
            reply.error.code,
            reply.error.details.model_dump()
            if reply.error.details is not None
            else None,
        )
    return model.model_validate(reply.data)


class ProjectManagerFacts(_WireModel):
    actor_id: UUID
    platform_role: str
    project_id: UUID
    owner_id: UUID
    data_type: str
    scene_mode: StrictBool


class DatasetAssociationFacts(_WireModel):
    link_id: UUID
    existing: StrictBool
    item_count: Count
    project_data_type: str


class CreationOperation(_WireModel):
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    project_id: UUID
    association_id: UUID
    job_id: UUID | None
    mode: CreationMode
    chunk_size: Annotated[int, Field(strict=True, ge=1, le=5000)]
    state: Literal["accepted", "running", "completed"]
    total: Count | None
    next_chunk: Count
    processed: Count
    created: Count

    @model_validator(mode="after")
    def check_counts(self):
        if self.state == "accepted":
            if (
                self.total is not None
                or self.created
                or self.processed
                or self.next_chunk
            ):
                raise ValueError("Accepted creation has no prepared source plan")
        elif self.total is None or not self.created <= self.processed <= self.total:
            raise ValueError("Creation counters do not match the source plan")
        elif (self.state == "completed") != (self.processed == self.total):
            raise ValueError("Creation completion does not match its counters")
        return self


class CreationChunk(_WireModel):
    operation_id: UUID
    project_id: UUID
    chunk_index: Count
    created: Count
    processed: Count
    cumulative_created: Count
    cumulative_processed: Count
    total: Count
    next_chunk: Count
    complete: StrictBool
    project_total: Count

    @model_validator(mode="after")
    def check_counts(self):
        if (
            not self.created
            <= self.processed
            <= self.cumulative_processed
            <= self.total
        ):
            raise ValueError("Creation chunk counters are inconsistent")
        if not self.created <= self.cumulative_created <= self.cumulative_processed:
            raise ValueError("Creation insert counters are inconsistent")
        if self.complete != (self.cumulative_processed == self.total):
            raise ValueError("Creation chunk completion is inconsistent")
        if self.next_chunk != self.chunk_index + 1:
            raise ValueError("Creation chunk cursor is inconsistent")
        return self


class CreationJobCreated(_WireModel):
    job_id: UUID


class CreationJobDiscovery(_WireModel):
    job_id: UUID | None
    operation_id: UUID | None
    status: JobStatus | None

    @model_validator(mode="after")
    def check_scope(self):
        if (self.job_id is None) != (self.operation_id is None) or (
            self.job_id is None
        ) != (self.status is None):
            raise ValueError("Creation discovery contains an incomplete job scope")
        return self


class CreationJobClaim(_WireModel):
    job_id: UUID
    operation_id: UUID
    actor_id: UUID
    dataset_id: UUID
    project_id: UUID
    association_id: UUID
    status: JobStatus
    cancel_requested: StrictBool


class CreationJobFinished(_WireModel):
    status: JobStatus
    result: dict[str, object]
