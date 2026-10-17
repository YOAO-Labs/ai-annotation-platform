"""Session-free contract for Labeling's task creation operations."""

from __future__ import annotations

import uuid
from typing import Protocol, TypedDict

from app.services.dataset_task_facts import (
    PreparedDatasetItem,
    PreparedPointCloudFrame,
)


class TaskCreationCounts(TypedDict):
    """Counts of the pending snapshot; already-created tasks are excluded."""

    created: int
    total: int


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

    async def append_items(
        self,
        *,
        project_ids: tuple[uuid.UUID, ...],
        items: tuple[PreparedDatasetItem, ...],
    ) -> int: ...
