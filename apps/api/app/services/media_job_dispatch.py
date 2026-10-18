"""Execution port for Assets media jobs."""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict
from uuid import UUID


class VideoFrameRequest(TypedDict):
    frame_index: int
    width: int
    format: Literal["webp", "jpeg"]


class MediaJobDispatcher(Protocol):
    def dispatch_thumbnail(self, item_id: UUID) -> None: ...

    def dispatch_video_metadata(self, item_id: UUID) -> None: ...

    def dispatch_task_thumbnail(self, task_id: UUID) -> None: ...

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str: ...

    def dispatch_video_chunks(self, item_id: UUID, chunk_ids: list[int]) -> None: ...

    def dispatch_video_frames(
        self, item_id: UUID, requests: list[VideoFrameRequest]
    ) -> None: ...
