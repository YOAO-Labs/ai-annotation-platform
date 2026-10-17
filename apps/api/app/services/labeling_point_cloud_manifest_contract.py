"""Pure task-to-dataset-item link facts for the point-cloud manifest consumer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TaskDatasetItemLinkFact:
    dataset_item_id: UUID
    role: str


class TaskDatasetItemLinksReader(Protocol):
    async def read_task_dataset_item_links(
        self, task_id: UUID
    ) -> tuple[TaskDatasetItemLinkFact, ...]:
        """Return linked item identifiers in the existing query order."""
        ...
