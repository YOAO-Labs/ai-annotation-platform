"""Image thumbnail workers and thin Celery adapters for the Assets media owner."""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from aap_assets_media.contract import (
    BackfillSelection,
    BackfillSpec,
    ChunkSpec,
    FrameSpec,
    MetadataSpec,
    ScheduleWorkRequest,
)
from app.config import settings
from app.db.models.dataset import DatasetItem
from app.observability.metrics import VIDEO_FRAME_ASSET_BYTES
from app.services.media_materialization import MediaMaterializationWriter
from app.services.assets_media import (
    maintain_video_media,
    media_execution_session,
    media_gateway,
)
from app.workers.celery_app import celery_app
from app.workers.media_materialization import SqlAlchemyMediaMaterializationWriter

THUMBNAIL_MAX_SOURCE_BYTES = 512 * 1024 * 1024
VIDEO_MEDIA_DRAIN_LIMIT = 32


def _thumbnail_from_path(source_path: Path) -> tuple[bytes, str]:
    from PIL import Image, ImageOps

    with Image.open(source_path) as source:
        img = ImageOps.exif_transpose(source).convert("RGB")
        max_side = 256
        width, height = img.size
        scale = min(max_side / width, max_side / height)
        thumb = img.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )

        import blurhash as bh

        small = img.resize((32, 32), Image.Resampling.LANCZOS)
        hash_str = bh.encode(small, x_components=4, y_components=3)
        buf = io.BytesIO()
        thumb.save(buf, format="WEBP", quality=80)
        return buf.getvalue(), hash_str


async def _generate_thumbnail(item_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        item = await db.get(DatasetItem, uuid.UUID(item_id))
        if not item or item.file_type != "image":
            return

        writer: MediaMaterializationWriter = SqlAlchemyMediaMaterializationWriter(db)
        storage = StorageService()
        if item.width is None or item.height is None:
            dimensions = storage.read_image_dimensions(
                item.file_path, bucket=storage.datasets_bucket
            )
            if dimensions:
                await writer.set_item_dimensions(item.id, dimensions)
                await db.commit()

        from app.services.image_pyramid import pyramid_eligible

        if pyramid_eligible(item.width, item.height):
            if settings.image_pyramid_auto_generate:
                from app.workers.image_pyramid import enqueue_image_pyramid

                enqueue_image_pyramid("dataset_item", item.id)
            return

        try:
            head = storage.client.head_object(
                Bucket=storage.datasets_bucket, Key=item.file_path
            )
            if int(head.get("ContentLength") or 0) > THUMBNAIL_MAX_SOURCE_BYTES:
                raise RuntimeError("thumbnail_source_too_large")
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="anno-thumbnail-") as tmp:
                source_path = Path(tmp) / "source"
                with source_path.open("xb") as stream:
                    storage.client.download_fileobj(
                        Bucket=storage.datasets_bucket,
                        Key=item.file_path,
                        Fileobj=stream,
                    )
                thumbnail_bytes, hash_str = _thumbnail_from_path(source_path)
        except Exception as exc:
            await writer.record_thumbnail_error(item.id, str(exc))
            await db.commit()
            return

        thumb_key = f"thumbnails/{item_id}.webp"
        try:
            storage.ensure_bucket(storage.media_cache_bucket)
            storage.client.put_object(
                Bucket=storage.media_cache_bucket,
                Key=thumb_key,
                Body=thumbnail_bytes,
                ContentType="image/webp",
            )
        except Exception as exc:
            await writer.record_thumbnail_error(item.id, str(exc))
            await db.commit()
            return

        await writer.set_item_thumbnail(item.id, thumb_key, blurhash=hash_str)
        await db.commit()

    await engine.dispose()


async def _backfill_media_images(dataset_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        rows = await db.execute(
            select(DatasetItem.id).where(
                DatasetItem.dataset_id == uuid.UUID(dataset_id),
                DatasetItem.file_type == "image",
                DatasetItem.thumbnail_path.is_(None),
            )
        )
        item_ids = [str(row[0]) for row in rows.all()]
    await engine.dispose()

    for item_id in item_ids:
        await _generate_thumbnail(item_id)


async def _generate_task_thumbnail(task_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.db.models.task import Task
    from app.db.models.dataset import DatasetItem  # noqa: F401 — needed for FK resolution
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        task = await db.get(Task, uuid.UUID(task_id))
        if not task or task.file_type != "image":
            return

        writer: MediaMaterializationWriter = SqlAlchemyMediaMaterializationWriter(db)
        storage = StorageService()
        dimensions = storage.read_image_dimensions(
            task.file_path, bucket=storage.bucket
        )
        from app.services.image_pyramid import pyramid_eligible

        if dimensions and pyramid_eligible(*dimensions):
            if settings.image_pyramid_auto_generate:
                from app.workers.image_pyramid import enqueue_image_pyramid

                enqueue_image_pyramid("task", task.id, force=True)
            return
        try:
            head = storage.client.head_object(Bucket=storage.bucket, Key=task.file_path)
            if int(head.get("ContentLength") or 0) > THUMBNAIL_MAX_SOURCE_BYTES:
                return
            from tempfile import TemporaryDirectory

            with TemporaryDirectory(prefix="anno-task-thumbnail-") as tmp:
                source_path = Path(tmp) / "source"
                with source_path.open("xb") as stream:
                    storage.client.download_fileobj(
                        Bucket=storage.bucket,
                        Key=task.file_path,
                        Fileobj=stream,
                    )
                thumbnail_bytes, hash_str = _thumbnail_from_path(source_path)
        except Exception:
            return

        thumb_key = f"thumbnails/{task_id}.webp"
        try:
            storage.ensure_bucket(storage.media_cache_bucket)
            storage.client.put_object(
                Bucket=storage.media_cache_bucket,
                Key=thumb_key,
                Body=thumbnail_bytes,
                ContentType="image/webp",
            )
        except Exception:
            return

        await writer.set_task_thumbnail(task.id, thumb_key, hash_str)
        await db.commit()

    await engine.dispose()


async def _backfill_tasks(project_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.db.models.task import Task
    from app.db.models.dataset import DatasetItem  # noqa: F401 — needed for FK resolution

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as db:
        rows = await db.execute(
            select(Task.id).where(
                Task.project_id == uuid.UUID(project_id),
                Task.file_type == "image",
                Task.dataset_item_id.is_(None),
                Task.thumbnail_path.is_(None),
            )
        )
        task_ids = [str(row[0]) for row in rows.all()]
    await engine.dispose()

    for task_id in task_ids:
        await _generate_task_thumbnail(task_id)


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
    from app.workers._db import task_session

    async with task_session() as db:
        async with media_gateway(db) as gateway:
            await gateway.call(
                "schedule_video_media",
                ScheduleWorkRequest(
                    operation_id=operation_id,
                    dataset_item_id=item_id,
                    spec=spec,
                    force=force,
                ),
            )
        await db.commit()


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
    from app.workers._db import task_session

    item_uuid = uuid.UUID(item_id)
    async with task_session() as db:
        async with media_gateway(db) as gateway:
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
        await db.commit()


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
    from app.workers._db import task_session

    item_uuid = uuid.UUID(item_id)
    async with task_session() as db:
        async with media_gateway(db) as gateway:
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
        await db.commit()


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
    async with media_execution_session() as executor:
        # A remote owner runs its own poller. Local mode keeps one executor for the
        # whole drain so verified source downloads remain reusable across jobs.
        if executor is None:
            return
        for _ in range(VIDEO_MEDIA_DRAIN_LIMIT):
            executed = await executor.execute_once(job_id=requested_job_id)
            if not executed:
                return
            requested_job_id = None
    execute_video_media.delay(None)


async def _maintain_video_media() -> None:
    usage = await maintain_video_media()
    if usage is None:
        return
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="frame").set(usage.frame_bytes)
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="chunk").set(usage.chunk_bytes)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_thumbnail(self, item_id: str) -> None:
    asyncio.run(_generate_thumbnail(item_id))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_video_metadata(self, item_id: str) -> None:
    asyncio.run(
        _schedule_video_metadata(
            item_id, _celery_operation_id(self.request.id, f"{item_id}:metadata")
        )
    )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_task_thumbnail(self, task_id: str) -> None:
    asyncio.run(_generate_task_thumbnail(task_id))


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_media(self, dataset_id: str, *, schedule_video: bool = True) -> None:
    async def run() -> None:
        if schedule_video:
            await _schedule_video_backfill(
                dataset_id,
                _celery_operation_id(self.request.id, f"{dataset_id}:backfill"),
            )
        await _backfill_media_images(dataset_id)

    asyncio.run(run())


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_tasks(self, project_id: str) -> None:
    asyncio.run(_backfill_tasks(project_id))


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
