from __future__ import annotations

from dataclasses import dataclass
import uuid


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
