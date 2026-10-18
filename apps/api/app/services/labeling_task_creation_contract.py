"""Session-free contract for Labeling's task creation operations."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.services.dataset_task_facts import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
)


class TaskCreationCounts(TypedDict):
    """Counts of the pending snapshot; already-created tasks are excluded."""

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
    """Local creation operations bound to the caller's existing transaction.

    Initial creation keeps its chunk commits when enabled, while append only
    flushes. Empty initial inputs return zero counts without committing. Asset
    calibration and its separate commit remain the application owner's work.
    """

    async def read_pending_dataset_items(
        self, *, dataset_id: uuid.UUID, project_id: uuid.UUID
    ) -> tuple[PreparedDatasetItem, ...]:
        """Read pending assets in one statement, ordered by asset UUID."""
        ...

    async def create_initial_generic(
        self,
        *,
        project_id: uuid.UUID,
        pending_items: tuple[PreparedDatasetItem, ...],
        job_id: uuid.UUID | None = None,
        chunk_size: int = 5000,
        commit_each_chunk: bool = True,
    ) -> TaskCreationCounts: ...

    async def create_initial_pointcloud(
        self,
        *,
        project_id: uuid.UUID,
        frames: tuple[PreparedPointCloudFrame, ...],
        job_id: uuid.UUID | None = None,
        chunk_size: int = 5000,
        commit_each_chunk: bool = True,
    ) -> TaskCreationCounts: ...

    async def append_dataset_items(
        self,
        *,
        dataset_id: uuid.UUID,
        item_ids: tuple[uuid.UUID, ...],
        operation_id: uuid.UUID,
    ) -> int: ...
