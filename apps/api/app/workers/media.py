"""Image thumbnail workers and thin Celery adapters for the Assets media owner."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

from aap_assets_media.contract import (
    BackfillSelection,
    BackfillSpec,
    ChunkSpec,
    FrameSpec,
    MetadataSpec,
    ScheduleWorkRequest,
)
from app.config import settings
from app.observability.metrics import VIDEO_FRAME_ASSET_BYTES
from app.services.assets_media import (
    maintain_video_media,
    media_execution_session,
    media_gateway,
)
from app.workers.celery_app import celery_app

VIDEO_MEDIA_DRAIN_LIMIT = 32


@asynccontextmanager
async def _celery_media_gateway():
    if settings.assets_media_url:
        async with media_gateway() as gateway:
            yield gateway
        return
    from app.workers._db import task_session_factory

    async with task_session_factory() as session_factory:
        async with media_gateway(session_factory=session_factory) as gateway:
            yield gateway


def _wake_accepted_image_pool() -> None:
    """Legacy ID-only deliveries may drain, but cannot admit work themselves."""
    from app.workers.image_pyramid import execute_image_media

    execute_image_media.delay(None)


def _celery_operation_id(task_id: str | None, suffix: str) -> uuid.UUID:
    if task_id:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"aap:assets-media:{task_id}:{suffix}")
    return uuid.uuid4()


async def _schedule_video_work(
    *,
    item_id: uuid.UUID | None,
    spec,
    operation_id: uuid.UUID,
    force: bool = False,
) -> None:
    async with _celery_media_gateway() as gateway:
        await gateway.call(
            "schedule_video_media",
            ScheduleWorkRequest(
                operation_id=operation_id,
                dataset_item_id=item_id,
                spec=spec,
                force=force,
            ),
        )


async def _schedule_video_metadata(item_id: str, operation_id: uuid.UUID) -> None:
    await _schedule_video_work(
        item_id=uuid.UUID(item_id),
        spec=MetadataSpec(),
        operation_id=operation_id,
    )


async def _schedule_video_chunks(
    item_id: str, chunk_ids: list[int], task_id: str | None
) -> None:
    unique_ids = sorted(set(int(chunk_id) for chunk_id in chunk_ids))
    if not unique_ids:
        return
    item_uuid = uuid.UUID(item_id)
    async with _celery_media_gateway() as gateway:
        for chunk_id in unique_ids:
            await gateway.call(
                "schedule_video_media",
                ScheduleWorkRequest(
                    operation_id=_celery_operation_id(
                        task_id, f"{item_id}:chunk:{chunk_id}"
                    ),
                    dataset_item_id=item_uuid,
                    spec=ChunkSpec(
                        chunk_id=chunk_id,
                        chunk_size_frames=max(1, settings.video_chunk_size_frames),
                    ),
                ),
            )


async def _schedule_video_frames(
    item_id: str,
    frame_requests: list[dict[str, Any]],
    task_id: str | None,
) -> None:
    requests = {
        (int(item["frame_index"]), int(item["width"]), str(item["format"]))
        for item in frame_requests
    }
    if not requests:
        return
    item_uuid = uuid.UUID(item_id)
    async with _celery_media_gateway() as gateway:
        for frame_index, width, format_ in sorted(requests):
            await gateway.call(
                "schedule_video_media",
                ScheduleWorkRequest(
                    operation_id=_celery_operation_id(
                        task_id, f"{item_id}:frame:{frame_index}:{width}:{format_}"
                    ),
                    dataset_item_id=item_uuid,
                    spec=FrameSpec(
                        frame_index=frame_index,
                        width=width,
                        format=format_,
                    ),
                ),
            )


async def _schedule_video_backfill(dataset_id: str, operation_id: uuid.UUID) -> None:
    await _schedule_video_work(
        item_id=None,
        spec=BackfillSpec(
            selection=BackfillSelection(dataset_id=uuid.UUID(dataset_id))
        ),
        operation_id=operation_id,
    )


async def _execute_video_media(job_id: str | None) -> None:
    requested_job_id = uuid.UUID(job_id) if job_id else None
    if settings.assets_media_url:
        return
    from app.workers._db import task_session_factory

    async with task_session_factory() as session_factory:
        async with media_execution_session(session_factory=session_factory) as executor:
            # A remote owner runs its own poller. Local mode keeps one executor
            # for the whole drain so verified source downloads remain reusable.
            if executor is None:
                return
            for _ in range(VIDEO_MEDIA_DRAIN_LIMIT):
                executed = await executor.execute_once(job_id=requested_job_id)
                if not executed:
                    return
                requested_job_id = None
    execute_video_media.delay(None)


async def _maintain_video_media() -> None:
    if settings.assets_media_url:
        return
    from app.workers._db import task_session_factory

    async with task_session_factory() as session_factory:
        usage = await maintain_video_media(session_factory=session_factory)
        if usage is None:
            return
    execute_video_media.delay(None)
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="frame").set(usage.frame_bytes)
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="chunk").set(usage.chunk_bytes)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_thumbnail(self, item_id: str) -> None:
    # Legacy delivery IDs cannot admit work after migration 0220.
    _wake_accepted_image_pool()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_video_metadata(self, item_id: str) -> None:
    asyncio.run(
        _schedule_video_metadata(
            item_id, _celery_operation_id(self.request.id, f"{item_id}:metadata")
        )
    )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_task_thumbnail(self, task_id: str) -> None:
    # Legacy delivery IDs cannot admit work after migration 0220.
    _wake_accepted_image_pool()


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_media(self, dataset_id: str, *, schedule_video: bool = True) -> None:
    async def run() -> None:
        if schedule_video:
            await _schedule_video_backfill(
                dataset_id,
                _celery_operation_id(self.request.id, f"{dataset_id}:backfill"),
            )
        _wake_accepted_image_pool()

    asyncio.run(run())


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_tasks(self, project_id: str) -> None:
    _wake_accepted_image_pool()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def ensure_video_chunks(self, item_id: str, chunk_ids: list[int]) -> None:
    asyncio.run(_schedule_video_chunks(item_id, chunk_ids, self.request.id))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def extract_video_frames(
    self, item_id: str, frame_requests: list[dict[str, Any]]
) -> None:
    asyncio.run(_schedule_video_frames(item_id, frame_requests, self.request.id))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, queue="media")
def execute_video_media(self, job_id: str | None = None) -> None:
    asyncio.run(_execute_video_media(job_id))


@celery_app.task(bind=True, max_retries=1, queue="media")
def cleanup_video_frame_assets(self) -> None:
    asyncio.run(_maintain_video_media())
