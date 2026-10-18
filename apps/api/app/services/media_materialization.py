"""Write port for image and task thumbnails using caller transactions."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class MediaMaterializationWriter(Protocol):
    async def set_item_dimensions(
        self, item_id: UUID, dimensions: tuple[int, int]
    ) -> None: ...

    async def record_thumbnail_error(self, item_id: UUID, error: str) -> None: ...

    async def set_item_thumbnail(
        self, item_id: UUID, thumbnail_path: str, *, blurhash: str
    ) -> None: ...

    async def set_task_thumbnail(
        self, task_id: UUID, thumbnail_path: str, blurhash: str | None
    ) -> None:
        """A ``None`` blurhash preserves the task's existing value."""
        ...
