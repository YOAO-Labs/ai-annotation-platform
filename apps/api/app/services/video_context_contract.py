"""Persistence-free reads for the current source facts of a video context."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.services.video_context import VideoContext


class VideoContextReader(Protocol):
    async def from_task(
        self,
        *,
        task_id: UUID,
        project_id: UUID,
        file_type: str,
        dataset_item_id: UUID | None,
    ) -> VideoContext:
        """Read a task's current dataset item and combine it with known task facts."""

    async def from_dataset_item(
        self,
        *,
        dataset_item_id: UUID,
        task_id: UUID | None = None,
        project_id: UUID | None = None,
    ) -> VideoContext:
        """Read one item, optionally carrying task identity already known by caller."""
