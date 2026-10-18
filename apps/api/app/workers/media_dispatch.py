"""Celery producer adapter for the media-job execution port."""

from __future__ import annotations

from uuid import UUID

from app.services.media_job_dispatch import VideoFrameRequest


class CeleryMediaJobDispatcher:
    __slots__ = ()

    def dispatch_asset_storage_cleanup(self, operation_id: UUID) -> None:
        from app.workers.asset_storage_cleanup import retry_asset_storage_deletions

        retry_asset_storage_deletions.delay(str(operation_id))

    def dispatch_thumbnail(self, item_id: UUID) -> None:
        from app.workers.media import generate_thumbnail

        generate_thumbnail.delay(str(item_id))

    def dispatch_video_metadata(self, item_id: UUID) -> None:
        from app.workers.media import generate_video_metadata

        generate_video_metadata.delay(str(item_id))

    def dispatch_task_thumbnail(self, task_id: UUID) -> None:
        from app.workers.media import generate_task_thumbnail

        generate_task_thumbnail.delay(str(task_id))

    def dispatch_task_thumbnail_backfill(self, project_id: UUID) -> str:
        from app.workers.media import backfill_tasks

        result = backfill_tasks.delay(str(project_id))
        return str(result.id)

    def dispatch_video_chunks(self, item_id: UUID, chunk_ids: list[int]) -> None:
        from app.workers.media import ensure_video_chunks

        ensure_video_chunks.delay(str(item_id), chunk_ids)

    def dispatch_video_frames(
        self, item_id: UUID, requests: list[VideoFrameRequest]
    ) -> None:
        from app.workers.media import extract_video_frames

        extract_video_frames.delay(str(item_id), requests)
