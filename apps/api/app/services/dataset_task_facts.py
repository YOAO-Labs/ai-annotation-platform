from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
import uuid
from typing import Protocol, TypeAlias


MAX_PROJECT_TASK_SOURCE_FACTS = 20_001


@dataclass(frozen=True, slots=True)
class ProjectTaskSourceRead:
    """Bounded request for one project-scoped labeling source snapshot.

    An empty ``task_ids`` means all tasks in the optional batch scope. ``batch_id=None``
    means no batch restriction. Results are ordered by task UUID and limited to
    ``limit``; missing or out-of-scope requested IDs are absent from the result for
    the caller to classify. A no-match result has no tool bindings and no task facts.
    """

    project_id: uuid.UUID
    tool_unit_id: str
    batch_id: uuid.UUID | None = None
    task_ids: tuple[uuid.UUID, ...] = ()
    limit: int = MAX_PROJECT_TASK_SOURCE_FACTS

    def __post_init__(self) -> None:
        if not self.tool_unit_id:
            raise ValueError("tool_unit_id must not be empty")
        if (
            type(self.limit) is not int
            or not 1 <= self.limit <= MAX_PROJECT_TASK_SOURCE_FACTS
        ):
            raise ValueError(
                f"limit must be between 1 and {MAX_PROJECT_TASK_SOURCE_FACTS}"
            )
        if self.task_ids and len(self.task_ids) > self.limit:
            raise ValueError("limit must cover all requested task IDs")


@dataclass(frozen=True, slots=True)
class PreparedDatasetItem:
    """Asset facts needed by task creation, detached from the ORM identity map."""

    id: uuid.UUID
    file_name: str
    file_path: str
    file_type: str
    scene_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class GroupedFrame:
    lidar: PreparedDatasetItem | None
    cameras: tuple[tuple[str, PreparedDatasetItem], ...]


@dataclass(frozen=True, slots=True)
class PreparedPointCloudFrame:
    scene_id: uuid.UUID | None
    frame_id: str
    lidar: PreparedDatasetItem
    cameras: tuple[tuple[str, PreparedDatasetItem], ...]


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
JsonObject: TypeAlias = Mapping[str, JsonValue]


def freeze_source_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: freeze_source_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_source_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported JSON source fact value: {type(value).__name__}")


def thaw_source_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_source_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_source_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AnnotationSourceFact:
    id: str
    version: int
    class_name: str
    tool_unit_id: str
    geometry: JsonValue

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "class_name": self.class_name,
            "tool_unit_id": self.tool_unit_id,
            "geometry": thaw_source_json(self.geometry),
        }


@dataclass(frozen=True, slots=True)
class TaskLabelingSourceFact:
    task_id: uuid.UUID
    task_version: int
    file_type: str
    reviewed: bool
    unusable: bool
    reviewed_negative: bool
    excluded_annotation_count: int
    dataset_item_id: uuid.UUID | None
    file_path: str
    scene_id: uuid.UUID | None
    width: int | None
    height: int | None
    annotations: tuple[AnnotationSourceFact, ...]


@dataclass(frozen=True, slots=True)
class ProjectTaskSourceFacts:
    tool_bindings: JsonObject | None
    tasks: tuple[TaskLabelingSourceFact, ...]


class ProjectTaskSourceReader(Protocol):
    async def read_project_task_source_facts(
        self, request: ProjectTaskSourceRead
    ) -> ProjectTaskSourceFacts:
        """Read all source facts in one consistent, bounded database statement."""
        ...
