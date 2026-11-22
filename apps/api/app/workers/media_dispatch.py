"""Compatibility producer that wakes already accepted image-owner work."""

from __future__ import annotations

from uuid import UUID


class CeleryMediaJobDispatcher:
    __slots__ = ()

    @staticmethod
    def _wake_pool():
        from app.workers.image_pyramid import execute_image_media

        return execute_image_media.delay(None)

    def dispatch_thumbnail(self, item_id: UUID) -> None:
        self._wake_pool()

    def dispatch_task_thumbnail(self, task_id: UUID) -> None:
        self._wake_pool()

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str:
        result = self._wake_pool()
        return str(result.id)
