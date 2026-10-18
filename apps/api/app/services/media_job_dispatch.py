"""Execution port for legacy image and task thumbnail jobs."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class MediaJobDispatcher(Protocol):
    def dispatch_thumbnail(self, item_id: UUID) -> None: ...

    def dispatch_task_thumbnail(self, task_id: UUID) -> None: ...

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str: ...
