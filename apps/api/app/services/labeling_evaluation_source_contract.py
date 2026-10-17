"""In-process contract for reading current task source facts from labeling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypedDict
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CurrentTaskSourceRead:
    """Request one current read, with the caller setting its annotation cap.

    Project and task IDs scope the read but do not authorize it. The caller must
    check current access before and after any object-storage work.
    """

    project_id: UUID
    task_id: UUID
    annotation_limit: int

    def __post_init__(self) -> None:
        if type(self.annotation_limit) is not int or self.annotation_limit < 1:
            raise ValueError("annotation_limit must be a positive integer")


class CurrentTaskSourceAnnotation(TypedDict):
    id: str
    version: int
    class_name: str
    tool_unit_id: str
    geometry: dict[str, object]


class CurrentTaskSourceSnapshot(TypedDict):
    """Detached, mutable facts observed in one query, not frozen truth or a grant.

    The annotation list can be empty. When more rows exist than the supplied limit,
    it contains only that limited, ID-ordered projection.
    """

    task_id: str
    project_id: str
    batch_id: str | None
    dataset_item_id: str | None
    source_bucket: str
    source_key: str
    file_type: str
    version: int
    status: str
    submitted_at: str | None
    skipped_at: str | None
    skip_reason: str | None
    reviewed_at: str | None
    expected_width: int | None
    expected_height: int | None
    annotations: list[CurrentTaskSourceAnnotation]


class CurrentTaskSourceReader(Protocol):
    async def read_current_task_source(
        self, request: CurrentTaskSourceRead
    ) -> CurrentTaskSourceSnapshot | None:
        """Return None if the task is absent from the requested project."""
        ...
