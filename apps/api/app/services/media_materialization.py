"""Write port for media results owned by assets and labeling tasks."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID


class MediaMaterializationWriter(Protocol):
    async def set_item_dimensions(
        self, item_id: UUID, dimensions: tuple[int, int]
    ) -> None: ...

    async def record_thumbnail_error(self, item_id: UUID, error: str) -> None: ...

    async def set_item_thumbnail(
        self, item_id: UUID, thumbnail_path: str, *, blurhash: str
    ) -> None: ...

    async def replace_video_frame_index(
        self,
        item_id: UUID,
        frame_timetable: Sequence[Mapping[str, Any]],
    ) -> None: ...

    async def set_video_dimensions(
        self,
        item_id: UUID,
        *,
        width: Any | None,
        height: Any | None,
    ) -> None: ...

    async def record_video_probe_error(
        self, item_id: UUID, video_metadata: Mapping[str, Any]
    ) -> None: ...

    async def save_video_materialization(
        self,
        item_id: UUID,
        video_metadata: Mapping[str, Any],
        *,
        thumbnail_path: str | None,
    ) -> None: ...

    async def set_task_thumbnail(
        self, task_id: UUID, thumbnail_path: str, blurhash: str
    ) -> None: ...
