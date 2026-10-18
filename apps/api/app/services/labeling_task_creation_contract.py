"""Session-free contract for Labeling's task creation operations."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.services.task_creation_contract import (
    CreationChunk,
    CreationMode,
    CreationOperation,
)


class TaskCreationCounts(TypedDict):
    """Actual inserts and the operation's frozen pending-source total."""

    created: int
    total: int


class AppendProjectTotal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: uuid.UUID
    total_tasks: Annotated[int, Field(strict=True, ge=0)]


class AppendAssociation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    project_id: uuid.UUID


class TaskAppendResult(BaseModel):
    """The persisted aap-task-append/v1 receipt, independent of ORM objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["aap-task-append/v1"] = Field(alias="schema")
    operation_id: uuid.UUID
    dataset_id: uuid.UUID
    created: Annotated[int, Field(strict=True, ge=0)]
    project_totals: tuple[AppendProjectTotal, ...]
    associations: tuple[AppendAssociation, ...]


class LabelingTaskCreationPort(Protocol):
    """Versioned data operations; their caller retains transaction ownership."""

    async def accept_dataset_task_creation(
        self,
        *,
        operation_id: uuid.UUID,
        actor_id: uuid.UUID,
        dataset_id: uuid.UUID,
        project_id: uuid.UUID,
        association_id: uuid.UUID,
        job_id: uuid.UUID | None,
        mode: CreationMode,
        chunk_size: int,
    ) -> CreationOperation: ...

    async def prepare_dataset_task_creation(
        self,
        *,
        operation_id: uuid.UUID,
        frames: list[dict[str, object]] | None,
    ) -> CreationOperation: ...

    async def read_dataset_task_creation(
        self, operation_id: uuid.UUID
    ) -> CreationOperation: ...

    async def advance_dataset_task_creation(
        self, operation_id: uuid.UUID, chunk_index: int
    ) -> CreationChunk: ...

    async def append_dataset_items(
        self,
        *,
        dataset_id: uuid.UUID,
        item_ids: tuple[uuid.UUID, ...],
        operation_id: uuid.UUID,
    ) -> int: ...
