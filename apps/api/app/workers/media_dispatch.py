"""Celery producer adapter for image and task thumbnail jobs."""

from __future__ import annotations

from uuid import UUID


class CeleryMediaJobDispatcher:
    __slots__ = ()

    def dispatch_thumbnail(self, item_id: UUID) -> None:
        from app.workers.media import generate_thumbnail

        generate_thumbnail.delay(str(item_id))

    def dispatch_task_thumbnail(self, task_id: UUID) -> None:
        from app.workers.media import generate_task_thumbnail

        generate_task_thumbnail.delay(str(task_id))

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str:
        from app.workers.media import backfill_tasks

        result = backfill_tasks.delay(str(project_id))
        return str(result.id)
