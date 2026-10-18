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
        # Producer transactions must accept work through Assets before dispatch.
        # This compatibility signal deliberately carries no source identifier.
        self._wake_pool()

    def dispatch_task_thumbnail(self, task_id: UUID) -> None:
        # Legacy Task deliveries are wakes only; IDs cannot admit work.
        self._wake_pool()

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str:
        # The old producer name remains for compatibility, but the caller must
        # have accepted a native backfill before requesting a pool wake.
        result = self._wake_pool()
        return str(result.id)
