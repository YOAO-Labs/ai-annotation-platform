"""Celery producer adapter for the video-tracker execution port."""

from __future__ import annotations

from uuid import UUID

from celery import current_app


class CeleryVideoTrackerJobDispatcher:
    __slots__ = ()

    def dispatch_tracker_job(self, job_id: UUID, *, execution_id: str) -> str:
        result = current_app.send_task(
            "app.workers.video_tracker.run_video_tracker_job",
            args=[str(job_id)],
            task_id=execution_id,
            queue="gpu",
        )
        return result.id
