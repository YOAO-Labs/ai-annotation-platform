from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.dataset import (
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
)
from app.db.models.task import Task
from app.observability.metrics import (
    VIDEO_CHUNK_REQUESTS_TOTAL,
    VIDEO_FRAME_CACHE_TOTAL,
)

from app.schemas.task import (
    VideoMetadata,
    TaskVideoManifestResponse,
    TaskVideoFrameTimetableResponse,
)
from app.schemas.video_frame_service import (
    VideoChunkOut,
    VideoChunksResponse,
    VideoChunkSamplesResponse,
    VideoFrameOut,
    VideoFramePrefetchResponse,
    VideoManifestV2Response,
)
from app.services import media_item_reads, storage_access
from app.services import video_derivatives as derivatives
from app.services import video_derivative_originals as originals
from app.services import video_derivative_objects as objects
from app.services import video_derivative_storage as physical
from app.services.content_snapshot import ContentError
from app.services.system_settings_service import SystemSettingsService


FrameFormat = Literal["webp", "jpeg"]
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoContext:
    item: DatasetItem
    metadata: VideoMetadata
    task: Task | None = None
    authority: originals.Authority | None = None
    generation: Any | None = None
    actor_id: uuid.UUID | None = None

    @property
    def task_id(self) -> uuid.UUID | None:
        return self.task.id if self.task else None

    # Source facts consumed by segment and tracking workflows. They come from
    # the same Assets public projection as the request context, never a second
    # ORM read.
    @property
    def dataset_item_id(self) -> uuid.UUID:
        return self.item.id

    @property
    def project_id(self) -> uuid.UUID | None:
        return self.task.project_id if self.task else None

    @property
    def source_file_path(self) -> str:
        return self.item.file_path

    @property
    def source_width(self) -> int | None:
        return self.item.width

    @property
    def source_height(self) -> int | None:
        return self.item.height


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _video_meta(item: DatasetItem) -> VideoMetadata:
    raw = (item.metadata_ or {}).get("video") or {}
    return VideoMetadata.model_validate(raw)


def _ensure_video_item(item: DatasetItem | None) -> DatasetItem:
    if not item or item.file_type != "video":
        raise HTTPException(status_code=404, detail="Video not found")
    return item


def _metadata_ready(metadata: VideoMetadata) -> None:
    if not metadata.fps or not metadata.frame_count:
        raise HTTPException(status_code=503, detail="Video metadata not ready")


def _safe_frame_range(
    metadata: VideoMetadata, from_frame: int | None, to_frame: int | None
) -> tuple[int, int]:
    _metadata_ready(metadata)
    last_frame = max(0, int(metadata.frame_count or 1) - 1)
    start = 0 if from_frame is None else max(0, from_frame)
    end = last_frame if to_frame is None else min(last_frame, to_frame)
    if end < start:
        raise HTTPException(status_code=400, detail="Invalid frame range")
    return start, end


def _chunk_ids_for_range(start: int, end: int) -> list[int]:
    size = max(1, settings.video_chunk_size_frames)
    return list(range(start // size, end // size + 1))


def _last_chunk_id(metadata: VideoMetadata) -> int:
    size = max(1, settings.video_chunk_size_frames)
    last_frame = max(0, int(metadata.frame_count or 1) - 1)
    return last_frame // size


def _chunk_bounds(chunk_id: int, metadata: VideoMetadata) -> tuple[int, int]:
    _metadata_ready(metadata)
    size = max(1, settings.video_chunk_size_frames)
    start = chunk_id * size
    end = min(int(metadata.frame_count or 1) - 1, start + size - 1)
    return start, end


def _estimated_pts_ms(frame_index: int, metadata: VideoMetadata) -> int | None:
    if not metadata.fps:
        return None
    return int(round((frame_index / metadata.fps) * 1000))


def _asset_url(key: str) -> str:
    """Legacy raw keys have no immutable output owner and cannot be signed."""
    raise HTTPException(409, {"code": "video_derivative_owner_required"})


def _http_errors(call):
    @wraps(call)
    async def guarded(*args, **kwargs):
        try:
            return await call(*args, **kwargs)
        except ContentError as exc:
            raise HTTPException(exc.status, {"code": exc.code}) from None
        except physical.VideoDerivativeStorageError as exc:
            raise HTTPException(exc.status_code, {"code": exc.code}) from None
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                raise HTTPException(409, {"code": "content_busy"}) from None
            raise

    return guarded


async def _public_video_item(
    db: AsyncSession, dataset_item_id: uuid.UUID
) -> DatasetItem | None:
    """Materialise the read-only source view from Assets' public item facts."""
    facts = await media_item_reads.read_item(db, dataset_item_id)
    if facts is None:
        return None
    links = (
        await media_item_reads.read_video_derivative_links(db, [dataset_item_id])
    ).get(dataset_item_id)
    return DatasetItem(
        id=facts.id,
        dataset_id=facts.dataset_id,
        file_name=facts.file_name,
        file_path=facts.file_path,
        file_type=facts.file_type,
        file_size=facts.file_size,
        content_hash=facts.content_hash,
        width=facts.width,
        height=facts.height,
        thumbnail_path=facts.thumbnail_path,
        metadata_=dict(facts.metadata),
        scene_id=facts.scene_id,
        frame_index=facts.frame_index,
        video_output_id=links.video_output_id if links else None,
        video_building_output_id=links.video_building_output_id if links else None,
    )


async def build_context_from_task(
    db: AsyncSession,
    task: Task,
    *,
    authority: originals.Authority | None = None,
    actor_id: uuid.UUID | None = None,
) -> VideoContext:
    if task.file_type != "video" or not task.dataset_item_id:
        raise HTTPException(status_code=400, detail="Task is not a video task")
    item = _ensure_video_item(await _public_video_item(db, task.dataset_item_id))
    return VideoContext(
        item=item,
        metadata=_video_meta(item),
        task=task,
        authority=authority,
        actor_id=actor_id,
    )


async def build_context_from_dataset_item(
    db: AsyncSession,
    dataset_item_id: uuid.UUID,
    task: Task | None = None,
    *,
    authority: originals.Authority | None = None,
    actor_id: uuid.UUID | None = None,
) -> VideoContext:
    item = _ensure_video_item(await _public_video_item(db, dataset_item_id))
    return VideoContext(
        item=item,
        metadata=_video_meta(item),
        task=task,
        authority=authority,
        actor_id=actor_id,
    )


@_http_errors
async def authorize_context(db, task, actor, request) -> VideoContext:
    """Freeze the particular visible Task and the original HTTP credentials."""
    if task.file_type != "video":
        raise HTTPException(400, "Task is not a video task")
    authority = await originals.authorize(db, task.id, actor, request)
    item = _ensure_video_item(await _public_video_item(db, task.dataset_item_id))
    # Legacy mutable metadata is not a prerequisite for loading an owned
    # generation. Pure metadata-only constructors retain their existing view.
    return VideoContext(
        item=item,
        metadata=VideoMetadata(),
        task=task,
        authority=authority,
        actor_id=actor.id,
    )


def _repairable(exc: Exception) -> bool:
    if getattr(exc, "storage_operation_unsettled", False):
        return False
    return (
        isinstance(exc, physical.VideoDerivativeStorageError)
        and exc.code
        in {
            "video_derivative_storage_missing",
            "video_derivative_storage_integrity_mismatch",
            "video_derivative_storage_invalid_receipt",
        }
    ) or (
        isinstance(exc, ContentError) and exc.code == "video_derivative_result_invalid"
    )


async def _metadata_preparing(db, ctx, *, force=False):
    current = await derivatives.row(db, DatasetItem, ctx.item.id)
    # Concurrent requests reuse the existing building attempt while preserving
    # the active metadata generation until publication.
    force = force and current.video_building_output_id is None
    output, enqueue = await derivatives.prepare(
        db, ctx.item.id, ctx.authority, "metadata", spec={"schema": 1}, force=force
    )
    await db.commit()
    if enqueue:
        derivatives.dispatch([output.id])
    raise HTTPException(503, {"code": "video_metadata_preparing"})


async def _physical_context(db, ctx: VideoContext) -> VideoContext:
    objects.begin_reads(db, ctx.authority)
    source = await originals.check(db, ctx.authority)
    if source.item_id != ctx.item.id:
        raise ContentError("content_binding_changed")
    item = _ensure_video_item(await derivatives.row(db, DatasetItem, ctx.item.id))
    ctx = replace(ctx, item=item)
    try:
        output = await derivatives.metadata(db, item, source)
        metadata, _ = derivatives.metadata_result(output)
        generation = objects.snapshot(output)
    except (ContentError, physical.VideoDerivativeStorageError) as exc:
        if not _repairable(exc) and not (
            isinstance(exc, ContentError)
            and exc.code
            in {
                "video_metadata_unpinned",
                "video_metadata_changed",
                "video_metadata_invalid",
            }
        ):
            raise
        await _metadata_preparing(db, ctx, force=item.video_output_id is not None)
    return replace(ctx, metadata=metadata, generation=generation)


async def pts_ms_for_frame(
    db: AsyncSession,
    dataset_item_id: uuid.UUID,
    frame_index: int,
    metadata: VideoMetadata,
) -> int | None:
    row = (
        await db.execute(
            select(VideoFrameIndex.pts_ms).where(
                VideoFrameIndex.dataset_item_id == dataset_item_id,
                VideoFrameIndex.frame_index == frame_index,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return int(row)
    return _estimated_pts_ms(frame_index, metadata)


async def _slot(db, ctx, kind, spec):
    model = VideoFrameCache if kind == "frame" else VideoChunk
    predicates = [model.dataset_item_id == ctx.item.id]
    names = ("frame_index", "width", "format") if kind == "frame" else ("chunk_id",)
    predicates.extend(getattr(model, name) == spec[name] for name in names)
    return await db.scalar(
        select(model).where(*predicates).execution_options(populate_existing=True)
    )


async def _prepare(db, ctx, kind, spec, *, force=False):
    output, enqueue = await derivatives.prepare(
        db, ctx.item.id, ctx.authority, kind, spec=spec, force=force
    )
    slot = await _slot(db, ctx, kind, spec)
    slot.last_accessed_at = _now()
    if output.state == "ready":
        try:
            output = await derivatives.ready_output(db, slot, ctx.authority)
        except (ContentError, physical.VideoDerivativeStorageError) as exc:
            if not _repairable(exc):
                raise
            await derivatives.check(db, output, ctx.authority)
            output, enqueue = await derivatives.prepare(
                db, ctx.item.id, ctx.authority, kind, spec=spec, force=True
            )
    return objects.snapshot(output), enqueue


async def _replacement(db, ctx, output):
    # A physical cache failure does not authorize replacing a different current
    # attempt or crossing a changed source, metadata generation or Gate.
    await derivatives.check(db, output, ctx.authority)
    replacement, enqueue = await _prepare(db, ctx, output.kind, output.spec, force=True)
    await db.commit()
    return replacement, enqueue


async def _deliver(db, ctx, output, queue):
    relative = (
        f"frame.{output.spec['format']}" if output.kind == "frame" else "chunk.mp4"
    )
    url = None
    if output.state == "ready":
        try:
            url = await derivatives.verified(
                db, output, ctx.authority, relative=relative
            )
        except (ContentError, physical.VideoDerivativeStorageError) as exc:
            if not _repairable(exc):
                raise
            output, enqueue = await _replacement(db, ctx, output)
            if enqueue:
                queue.append(output.id)
    ready = output.state == "ready"
    identity = (
        derivatives.identity(output, relative)
        if ready
        else {
            "output_id": output.id,
            "metadata_generation_id": output.metadata_output_id,
        }
    )
    if output.kind == "frame":
        VIDEO_FRAME_CACHE_TOTAL.labels(
            result="hit" if ready else "miss", format=output.spec["format"]
        ).inc()
        return VideoFrameOut(
            frame_index=output.spec["frame_index"],
            width=output.spec["width"],
            format=output.spec["format"],
            status="ready" if ready else "pending",
            url=url,
            retry_after=None if ready else 3,
            **identity,
        )
    VIDEO_CHUNK_REQUESTS_TOTAL.labels(status="ready" if ready else "pending").inc()
    result = output.result if ready else {}
    return VideoChunkOut(
        chunk_id=output.spec["chunk_id"],
        start_frame=output.spec["start_frame"],
        end_frame=output.spec["end_frame"],
        status="ready" if ready else "pending",
        url=url,
        generation_mode=result.get("generation_mode"),
        diagnostics=result.get("diagnostics"),
        retry_after=None if ready else 3,
        **identity,
    )


async def _warmup_specs(db, ctx, requested):
    try:
        async with db.begin_nested():
            look_ahead = await SystemSettingsService.get(
                db, "video_chunk_warmup_lookahead"
            )
    except Exception:  # noqa: BLE001
        log.exception("video chunk warmup setting unavailable; skipping warmup")
        return []
    return [
        derivatives.chunk_spec(ctx.generation, identifier)
        for identifier in warmup_chunk_ids(
            requested, _last_chunk_id(ctx.metadata), look_ahead
        )
    ]


async def _finish_batch(db, ctx, outputs, results, queue):
    # Earlier assets in a batch must still refer to the current generation when
    # later verification completes. No physical operation runs under these locks.
    await derivatives.check(db, ctx.generation, ctx.authority)
    for output, result in zip(outputs, results, strict=True):
        if result.status == "ready":
            await derivatives.check(db, output, ctx.authority)
    await db.commit()
    originals.clock(ctx.authority)
    derivatives.dispatch(list(dict.fromkeys(queue)))


async def _chunks(db, ctx, chunk_ids):
    queue = []
    requested = []
    for identifier in chunk_ids:
        output, enqueue = await _prepare(
            db, ctx, "chunk", derivatives.chunk_spec(ctx.generation, identifier)
        )
        # Each durable attempt releases its Item/source locks before the next
        # slot. The final batch fence still decides delivery and dispatch.
        await db.commit()
        requested.append(output)
        if enqueue:
            queue.append(output.id)
    for spec in await _warmup_specs(db, ctx, chunk_ids):
        output, enqueue = await _prepare(db, ctx, "chunk", spec)
        await db.commit()
        if enqueue:
            queue.append(output.id)
    await db.commit()
    result = [await _deliver(db, ctx, output, queue) for output in requested]
    await _finish_batch(db, ctx, requested, result, queue)
    return result


@_http_errors
async def list_chunks(
    db: AsyncSession, ctx: VideoContext, from_frame: int | None, to_frame: int | None
) -> VideoChunksResponse:
    ctx = await _physical_context(db, ctx)
    start, end = _safe_frame_range(ctx.metadata, from_frame, to_frame)
    chunks = await _chunks(db, ctx, _chunk_ids_for_range(start, end))
    return VideoChunksResponse(
        dataset_item_id=ctx.item.id,
        task_id=ctx.task_id,
        chunk_size_frames=settings.video_chunk_size_frames,
        chunks=chunks,
    )


@_http_errors
async def get_chunk(
    db: AsyncSession, ctx: VideoContext, chunk_id: int
) -> VideoChunkOut:
    ctx = await _physical_context(db, ctx)
    return (await _chunks(db, ctx, [chunk_id]))[0]


async def _frames(db, ctx, indices, width, format_, *, force=False):
    queue, outputs = [], []
    for index in sorted(set(indices)):
        output, enqueue = await _prepare(
            db,
            ctx,
            "frame",
            derivatives.frame_spec(ctx.generation, index, width, format_),
            force=force,
        )
        # A prefetch may contain hundreds of slots. Retain committed ownership,
        # rather than one Item UPDATE lock, across the rest of that batch.
        await db.commit()
        outputs.append(output)
        if enqueue:
            queue.append(output.id)
    await db.commit()
    result = [await _deliver(db, ctx, output, queue) for output in outputs]
    await _finish_batch(db, ctx, outputs, result, queue)
    return result


@_http_errors
async def get_frame(
    db: AsyncSession,
    ctx: VideoContext,
    frame_index: int,
    width: int,
    format_: FrameFormat,
) -> VideoFrameOut:
    ctx = await _physical_context(db, ctx)
    return (await _frames(db, ctx, [frame_index], width, format_))[0]


@_http_errors
async def prefetch_frames(
    db: AsyncSession,
    ctx: VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
) -> VideoFramePrefetchResponse:
    ctx = await _physical_context(db, ctx)
    frames = await _frames(db, ctx, frame_indices, width, format_)
    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.item.id, task_id=ctx.task_id, frames=frames
    )


@_http_errors
async def retry_frames(
    db: AsyncSession,
    ctx: VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
    *,
    force: bool = False,
) -> VideoFramePrefetchResponse:
    ctx = await _physical_context(db, ctx)
    indices = sorted(set(frame_indices))
    if not (force and indices):
        stmt = (
            select(VideoFrameCache.frame_index)
            .where(
                VideoFrameCache.dataset_item_id == ctx.item.id,
                VideoFrameCache.width == width,
                VideoFrameCache.format == format_,
                VideoFrameCache.status == "failed",
            )
            .order_by(VideoFrameCache.frame_index)
            .limit(500)
        )
        if indices:
            stmt = stmt.where(VideoFrameCache.frame_index.in_(indices))
        indices = list((await db.scalars(stmt)).all())
    frames = await _frames(db, ctx, indices, width, format_, force=True)
    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.item.id, task_id=ctx.task_id, frames=frames
    )


async def _verified_metadata(db, ctx):
    await db.commit()
    try:
        await derivatives.verified(db, ctx.generation, ctx.authority)
    except (ContentError, physical.VideoDerivativeStorageError) as exc:
        if not _repairable(exc):
            raise
        await derivatives.check(db, ctx.generation, ctx.authority)
        await _metadata_preparing(db, ctx, force=True)


@_http_errors
async def frame_timetable(
    db, ctx, from_frame=None, to_frame=None, *, expected_metadata_generation_id=None
):
    ctx = await _physical_context(db, ctx)
    if (
        expected_metadata_generation_id is not None
        and expected_metadata_generation_id != ctx.generation.id
    ):
        raise ContentError("video_metadata_generation_changed")
    if from_frame is not None and to_frame is not None and to_frame < from_frame:
        raise HTTPException(400, "Invalid frame range")
    metadata, frames = derivatives.metadata_result(ctx.generation)
    selected = [
        frame
        for frame in frames
        if (from_frame is None or frame["frame_index"] >= from_frame)
        and (to_frame is None or frame["frame_index"] <= to_frame)
    ]
    await _verified_metadata(db, ctx)
    return TaskVideoFrameTimetableResponse(
        task_id=ctx.task_id,
        metadata_generation_id=ctx.generation.id,
        fps=metadata.fps,
        frame_count=metadata.frame_count,
        source="ffprobe" if frames else "estimated",
        frames=selected,
    )


@_http_errors
async def chunk_samples(db, ctx, chunk_id, *, expected_output_id=None):
    ctx = await _physical_context(db, ctx)
    spec = derivatives.chunk_spec(ctx.generation, chunk_id)
    slot = await _slot(db, ctx, "chunk", spec)
    if expected_output_id is not None and (
        slot is None or slot.output_id != expected_output_id
    ):
        raise ContentError("video_derivative_generation_changed")
    if slot is None:
        raise HTTPException(404, "samples_not_available")
    output, enqueue = await _prepare(db, ctx, "chunk", spec)
    if expected_output_id is not None and output.id != expected_output_id:
        await db.commit()
        if enqueue:
            derivatives.dispatch([output.id])
        raise ContentError("video_derivative_generation_changed")
    if output.state != "ready":
        await db.commit()
        if enqueue:
            derivatives.dispatch([output.id])
        raise ContentError("video_derivative_not_ready")
    diagnostics = (output.result or {}).get("diagnostics") or {}
    if "samples" not in diagnostics:
        raise HTTPException(404, "samples_not_available")
    await db.commit()
    # Sample offsets describe this exact immutable chunk, including its bytes.
    try:
        await derivatives.verified(db, output, ctx.authority, relative="chunk.mp4")
    except (ContentError, physical.VideoDerivativeStorageError) as exc:
        if not _repairable(exc):
            raise
        replacement, enqueue = await _replacement(db, ctx, output)
        if enqueue:
            derivatives.dispatch([replacement.id])
        raise ContentError("video_derivative_not_ready") from None
    identity = derivatives.identity(output, "chunk.mp4")
    identity.pop("byte_size")
    return VideoChunkSamplesResponse(
        dataset_item_id=ctx.item.id,
        chunk_id=chunk_id,
        codec_string=diagnostics.get("codec_string", "avc1.4d001e"),
        description=diagnostics.get("description"),
        width=diagnostics.get("width", 0),
        height=diagnostics.get("height", 0),
        samples=diagnostics["samples"],
        **identity,
    )


@_http_errors
async def legacy_manifest(db, ctx):
    ctx = await _physical_context(db, ctx)
    await _verified_metadata(db, ctx)
    poster = None
    if ctx.metadata.poster_frame_path:
        try:
            poster = await derivatives.verified(
                db, ctx.generation, ctx.authority, relative="poster.webp"
            )
        except (ContentError, physical.VideoDerivativeStorageError) as exc:
            if not _repairable(exc):
                raise
            await derivatives.check(db, ctx.generation, ctx.authority)
            await _metadata_preparing(db, ctx, force=True)
    return TaskVideoManifestResponse(
        task_id=ctx.task_id,
        dataset_item_id=ctx.item.id,
        metadata_generation_id=ctx.generation.id,
        original_identity_path=f"/api/v1/files/tasks/{ctx.task_id}/original-identity",
        poster_url=poster,
        metadata=ctx.metadata,
    )


@_http_errors
async def manifest_v2(
    db: AsyncSession, ctx: VideoContext, base_url: str
) -> VideoManifestV2Response:
    ctx = await _physical_context(db, ctx)
    from app.services.video_segment_service import ensure_segments, segment_out
    from app.db.models.project import Project
    from app.services.video_collaboration import collaboration_config

    segment_rows = await ensure_segments(db, ctx)
    project = await db.get(Project, ctx.task.project_id) if ctx.task else None
    collaboration = collaboration_config(project)
    segments = [
        segment_out(
            row,
            frame_count=max(1, int(ctx.metadata.frame_count or 1)),
            segment_count=len(segment_rows),
            overlap_frames=collaboration.overlap_frames if collaboration.enabled else 0,
        )
        for row in segment_rows
    ]
    manifest = await legacy_manifest(db, ctx)
    base = base_url.rstrip("/")
    service_base = (
        f"{base}/api/v1/tasks/{ctx.task_id}/video"
        if ctx.task_id
        else f"{base}/api/v1/videos/{ctx.item.id}"
    )
    return VideoManifestV2Response(
        task_id=ctx.task_id,
        dataset_item_id=ctx.item.id,
        metadata_generation_id=manifest.metadata_generation_id,
        poster_url=manifest.poster_url,
        fps=ctx.metadata.fps,
        frame_count=ctx.metadata.frame_count,
        duration_ms=ctx.metadata.duration_ms,
        chunks_manifest_url=f"{service_base}/chunks",
        frame_timetable_url=f"{service_base}/frame-timetable",
        frame_service_base=f"{service_base}/frames",
        chunk_size_frames=settings.video_chunk_size_frames,
        segments=segments,
    )


def cache_key_for_frame(
    dataset_item_id: uuid.UUID, frame_index: int, width: int, format_: str
) -> str:
    return f"videos/{dataset_item_id}/frames/{frame_index}_{width}.{format_}"


def cache_key_for_chunk(dataset_item_id: uuid.UUID, chunk_id: int) -> str:
    return f"videos/{dataset_item_id}/chunks/{chunk_id}.mp4"


def source_key_for_item(item: DatasetItem) -> str:
    """ffmpeg 处理(抽帧 / 抽 chunk / 时间表探测)用的源 key —— 永远是**原始视频**。

    原始视频在 datasets_bucket。不要返回 `playback_path`(浏览器播放用的 h264 转码版,
    存在 media_cache_bucket): 用它会(1)与 worker 的 datasets_bucket 不匹配 → HeadObject
    404 抽帧全失败;(2)转码重编码后帧数 / 时序可能与原视频不一致 → 破坏 D2「frame_index
    永远对齐原视频帧号」。浏览器原件播放使用核验后的原字节，派生播放副本另有归属合同。
    """
    return item.file_path


def metadata_for_item(item: DatasetItem) -> VideoMetadata:
    return _video_meta(item)


def put_frame_array_cache(
    dataset_item_id: uuid.UUID, frame_index: int, width: int, format_: str, array: Any
) -> None:
    raise RuntimeError("Video frame array caches require an immutable output identity")


def get_frame_array_cache(
    dataset_item_id: uuid.UUID, frame_index: int, width: int, format_: str
) -> Any | None:
    raise RuntimeError("Video frame array caches require an immutable output identity")


def image_bytes_to_array(data: bytes) -> Any:
    from PIL import Image
    import numpy as np

    with Image.open(io.BytesIO(data)) as img:
        return np.asarray(img.convert("RGB"))


# ── v0.10.29 · 视频帧逻辑采样网格 helper (纯函数, 供导出 / 前端共用) ──────
#
# 采样只是项目级导航/打点网格的视图层 (决策 D1/D2): frame_index 永远是源视频
# 帧号, 这里只从配置派生「步长 step」与「采样帧列表」。算法见计划 §1。


def derive_step(source_fps: float | None, sampling: dict) -> int:
    """从采样配置派生网格步长 step (源帧空间), 最小为 1。

    - mode="step" → frame_step
    - mode="fps"  → max(1, round(source_fps / target_fps))
    - mode="none" / 缺省 / 配置不全 → 1 (退化为不采样, 所有帧都是网格点)
    """
    if not sampling:
        return 1
    mode = sampling.get("mode", "none")
    if mode == "step":
        frame_step = sampling.get("frame_step")
        if frame_step is None:
            return 1
        return max(1, int(frame_step))
    if mode == "fps":
        target_fps = sampling.get("target_fps")
        if not source_fps or not target_fps:
            return 1
        return max(1, round(source_fps / target_fps))
    return 1


def derive_sampled_frames(frame_count: int, step: int) -> list[int]:
    """绝对网格 (锚定 0) 上的采样帧列表: [0, step, 2*step, ...] 且 < frame_count。"""
    if frame_count <= 0:
        return []
    step = max(1, int(step))
    return list(range(0, frame_count, step))


# ── v0.10.29 · 长视频 sparse timetable helper (计划 §1.2, 纯函数) ─────────
#
# 超长视频不必给每帧都存一行 VideoFrameIndex(frame_index, pts_ms)。改成 sparse:
# 只持久化「锚点帧」(stride 网格上的真值 pts_ms), 中间帧的 pts_ms 由相邻锚点线性
# 插值得到; 落在锚点范围外则退化为 fps 估算 (沿用 _estimated_pts_ms 的语义)。
#
# 对外契约不变 (决策 D2): frame_index 永远是源视频帧号; pts_ms_for_frame 命中
# DB 真值优先, miss 时落到 fps 估算。本 helper 只是给「无 DB 全帧表」的 sparse
# 写入路径提供一个可单测的纯函数。锚点本身仍存进 VideoFrameIndex (无需新表)。


def derive_anchor_frames(frame_count: int, stride: int) -> list[int]:
    """sparse 锚点帧网格: [0, stride, 2*stride, ...] 且 < frame_count, 末帧也补上。

    末帧补锚点是为了让插值有右边界, 避免视频尾部全靠外推。
    """
    if frame_count <= 0:
        return []
    stride = max(1, int(stride))
    anchors = list(range(0, frame_count, stride))
    last = frame_count - 1
    if anchors[-1] != last:
        anchors.append(last)
    return anchors


def select_sparse_anchor_rows(
    rows: list[dict[str, Any]], stride: int
) -> list[dict[str, Any]]:
    """从全帧 timetable 行里挑出要持久化的 sparse 锚点子集 (按 frame_index)。

    锚点 = derive_anchor_frames(frame_count, stride) 网格上的帧 ∪ 所有关键帧。
    保留关键帧是为了 chunk smart-copy 的 keyframe 对齐判定 (依赖 is_keyframe) 不退化。
    rows 需含 frame_index / is_keyframe 字段; 返回保持原顺序的子集。
    stride<=1 时退化为全帧 (返回原列表), 即不做 sparse。
    """
    stride = max(1, int(stride))
    if stride == 1 or not rows:
        return rows
    frame_count = max(r["frame_index"] for r in rows) + 1
    anchor_set = set(derive_anchor_frames(frame_count, stride))
    return [r for r in rows if r["frame_index"] in anchor_set or r.get("is_keyframe")]


def resolve_pts_ms_sparse(
    frame_index: int,
    anchors: list[tuple[int, int]],
    fps: float | None,
    stride: int,
) -> int | None:
    """由 sparse 锚点解析任意源帧号的 pts_ms。

    - anchors: 已按 frame_index 升序排列的 (frame_index, pts_ms) 列表 (锚点真值)。
    - 命中锚点 → 直接返回真值。
    - 落在两个相邻锚点之间 → 按 frame_index 线性插值。
    - 落在锚点范围外 (或 anchors 为空) → 用最近锚点 + fps 外推; 无锚点无 fps → None。

    stride 仅作语义占位 (与 derive_anchor_frames 共享步长概念), 解析本身不依赖它。
    """
    _ = stride
    if not anchors:
        if not fps:
            return None
        return int(round((frame_index / fps) * 1000))

    first_f, first_pts = anchors[0]
    last_f, last_pts = anchors[-1]

    # 范围外: 从最近端锚点按 fps 外推
    if frame_index <= first_f:
        if frame_index == first_f:
            return first_pts
        if not fps:
            return None
        return int(round(first_pts + ((frame_index - first_f) / fps) * 1000))
    if frame_index >= last_f:
        if frame_index == last_f:
            return last_pts
        if not fps:
            return None
        return int(round(last_pts + ((frame_index - last_f) / fps) * 1000))

    # 范围内: 找到 bracketing 锚点 (lo < frame_index <= hi) 做线性插值
    lo_f, lo_pts = first_f, first_pts
    for f, pts in anchors:
        if f == frame_index:
            return pts
        if f > frame_index:
            hi_f, hi_pts = f, pts
            span = hi_f - lo_f
            if span <= 0:
                return lo_pts
            ratio = (frame_index - lo_f) / span
            return int(round(lo_pts + ratio * (hi_pts - lo_pts)))
        lo_f, lo_pts = f, pts
    return last_pts


# ── v0.10.29 · chunk warmup look-ahead 选择 (计划 §1.6, 纯函数) ───────────


def warmup_chunk_ids(
    requested_chunk_ids: list[int],
    last_chunk_id: int,
    look_ahead: int,
) -> list[int]:
    """请求命中某些 chunk 时, 计算应预解码的相邻 (向后) chunk id。

    - 只向后看 (逐帧导航多为向前推进): 取 max(requested)+1 .. +look_ahead。
    - 不超过 last_chunk_id (视频末尾 chunk), 不与 requested 重叠。
    - look_ahead<=0 或无请求 → 空列表 (warmup 完全降级)。
    """
    if look_ahead <= 0 or not requested_chunk_ids:
        return []
    frontier = max(requested_chunk_ids)
    requested = set(requested_chunk_ids)
    out: list[int] = []
    for cid in range(frontier + 1, frontier + 1 + look_ahead):
        if cid > last_chunk_id:
            break
        if cid not in requested:
            out.append(cid)
    return out


@_http_errors
async def get_frame_array(
    db: AsyncSession,
    dataset_item_id: uuid.UUID,
    frame_index: int,
    width: int = 512,
    format_: FrameFormat = "webp",
    *,
    authority: originals.Authority,
) -> Any:
    ctx = await build_context_from_dataset_item(
        db, dataset_item_id, authority=authority
    )
    ctx = await _physical_context(db, ctx)
    spec = derivatives.frame_spec(ctx.generation, frame_index, width, format_)
    slot = await _slot(db, ctx, "frame", spec)
    if slot is None:
        raise HTTPException(409, {"code": "video_derivative_not_ready"})
    output = objects.snapshot(await derivatives.ready_output(db, slot, authority))
    await db.commit()
    raw = await derivatives.verified(
        db,
        output,
        authority,
        relative=f"frame.{format_}",
        bytes_limit=64 * 1024**2,
    )
    return image_bytes_to_array(raw)
