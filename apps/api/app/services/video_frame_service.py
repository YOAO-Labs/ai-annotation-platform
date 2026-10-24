from __future__ import annotations

import io
import logging
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from app.schemas.task import VideoMetadata
from app.schemas.video_frame_service import (
    VideoChunkOut,
    VideoChunksResponse,
    VideoFrameOut,
    VideoFramePrefetchResponse,
    VideoManifestV2Response,
)
from app.services.storage import storage_service
from app.services.system_settings_service import SystemSettingsService


FrameFormat = Literal["webp", "jpeg"]
_FRAME_ARRAY_CACHE: OrderedDict[tuple[uuid.UUID, int, int, str], Any] = OrderedDict()
PENDING_FRAME_REQUEUE_AFTER = timedelta(seconds=30)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VideoContext:
    item: DatasetItem
    metadata: VideoMetadata
    task: Task | None = None

    @property
    def task_id(self) -> uuid.UUID | None:
        return self.task.id if self.task else None


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
    try:
        return storage_service.generate_download_url(
            key,
            expires_in=3600,
            bucket=storage_service.bucket_for_cache_key(key),
        )
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(
            status_code=503, detail="Video storage unavailable"
        ) from exc


async def build_context_from_task(db: AsyncSession, task: Task) -> VideoContext:
    if task.file_type != "video" or not task.dataset_item_id:
        raise HTTPException(status_code=400, detail="Task is not a video task")
    item = _ensure_video_item(await db.get(DatasetItem, task.dataset_item_id))
    return VideoContext(item=item, metadata=_video_meta(item), task=task)


async def build_context_from_dataset_item(
    db: AsyncSession, dataset_item_id: uuid.UUID, task: Task | None = None
) -> VideoContext:
    item = _ensure_video_item(await db.get(DatasetItem, dataset_item_id))
    return VideoContext(item=item, metadata=_video_meta(item), task=task)


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


async def _ensure_chunk_rows(
    db: AsyncSession, ctx: VideoContext, chunk_ids: list[int]
) -> list[VideoChunk]:
    existing = {
        row.chunk_id: row
        for row in (
            await db.execute(
                select(VideoChunk).where(
                    VideoChunk.dataset_item_id == ctx.item.id,
                    VideoChunk.chunk_id.in_(chunk_ids),
                )
            )
        )
        .scalars()
        .all()
    }
    rows: list[VideoChunk] = []
    for chunk_id in chunk_ids:
        row = existing.get(chunk_id)
        if row is None:
            start, end = _chunk_bounds(chunk_id, ctx.metadata)
            start_pts = await pts_ms_for_frame(db, ctx.item.id, start, ctx.metadata)
            end_pts = await pts_ms_for_frame(db, ctx.item.id, end, ctx.metadata)
            # 并发安全: list_chunks 主请求 + warmup + 并发 scrub 可能同时为同一 chunk_id
            # insert, 撞 uq_video_chunks_item_chunk。用 SAVEPOINT 包 INSERT, 冲突时只回滚该
            # savepoint (不破坏本批已建的其它 chunk), 再 select 拿别的请求插入的行。
            try:
                async with db.begin_nested():
                    row = VideoChunk(
                        dataset_item_id=ctx.item.id,
                        chunk_id=chunk_id,
                        start_frame=start,
                        end_frame=end,
                        start_pts_ms=start_pts,
                        end_pts_ms=end_pts,
                        status="pending",
                    )
                    db.add(row)
                    await db.flush()
            except IntegrityError:
                row = (
                    await db.execute(
                        select(VideoChunk).where(
                            VideoChunk.dataset_item_id == ctx.item.id,
                            VideoChunk.chunk_id == chunk_id,
                        )
                    )
                ).scalar_one()
        rows.append(row)
    await db.flush()
    return rows


def _chunk_out(row: VideoChunk) -> VideoChunkOut:
    status = row.status if row.status in {"pending", "ready", "failed"} else "pending"
    url = _asset_url(row.storage_key) if status == "ready" and row.storage_key else None
    generation_mode = (
        row.generation_mode
        if row.generation_mode in {"smart_copy", "transcode"}
        else None
    )
    VIDEO_CHUNK_REQUESTS_TOTAL.labels(status=status).inc()
    return VideoChunkOut(
        chunk_id=row.chunk_id,
        start_frame=row.start_frame,
        end_frame=row.end_frame,
        status=status,
        url=url,
        byte_size=row.byte_size,
        generation_mode=generation_mode,
        diagnostics=row.diagnostics or None,
        retry_after=3 if status == "pending" else None,
        error=row.error if status == "failed" else None,
    )


async def _warmup_neighbor_chunks(
    db: AsyncSession, ctx: VideoContext, requested_chunk_ids: list[int]
) -> None:
    """对热点 frame range 的相邻 (向后) chunk 做 look-ahead 预解码 (计划 §1.6)。

    保守降级: 只对「还没 ready 且没在 pending 进行中」的相邻 chunk 投递, 不重复
    投递、不阻塞主请求。warmup 失败/被关闭时静默跳过, 不影响主流程。
    """
    try:
        look_ahead = await SystemSettingsService.get(db, "video_chunk_warmup_lookahead")
    except Exception:  # noqa: BLE001
        # Warmup is an optional optimization.  A settings DB outage must not
        # turn the primary video chunk response into a playback failure.
        with suppress(Exception):
            await db.rollback()
        log.exception("video chunk warmup setting unavailable; skipping warmup")
        return

    candidates = warmup_chunk_ids(
        requested_chunk_ids,
        _last_chunk_id(ctx.metadata),
        look_ahead,
    )
    if not candidates:
        return
    rows = await _ensure_chunk_rows(db, ctx, candidates)
    to_warm = [r.chunk_id for r in rows if r.status not in {"ready", "pending"}]
    await db.commit()
    if to_warm:
        from app.workers.media import ensure_video_chunks

        ensure_video_chunks.delay(str(ctx.item.id), to_warm)


async def list_chunks(
    db: AsyncSession,
    ctx: VideoContext,
    from_frame: int | None,
    to_frame: int | None,
) -> VideoChunksResponse:
    start, end = _safe_frame_range(ctx.metadata, from_frame, to_frame)
    requested_ids = _chunk_ids_for_range(start, end)
    rows = await _ensure_chunk_rows(db, ctx, requested_ids)
    missing = [r.chunk_id for r in rows if r.status != "ready"]
    now = _now()
    for row in rows:
        row.last_accessed_at = now
    await db.commit()

    if missing:
        from app.workers.media import ensure_video_chunks

        ensure_video_chunks.delay(str(ctx.item.id), missing)

    # Freeze the primary response before optional I/O: a failed setting read
    # rolls back its transaction and expires ORM objects in this session.
    response = VideoChunksResponse(
        dataset_item_id=ctx.item.id,
        task_id=ctx.task_id,
        chunk_size_frames=settings.video_chunk_size_frames,
        chunks=[_chunk_out(row) for row in rows],
    )
    await _warmup_neighbor_chunks(db, ctx, requested_ids)
    return response


async def get_chunk(
    db: AsyncSession, ctx: VideoContext, chunk_id: int
) -> VideoChunkOut:
    _metadata_ready(ctx.metadata)
    row = (
        await db.execute(
            select(VideoChunk).where(
                VideoChunk.dataset_item_id == ctx.item.id,
                VideoChunk.chunk_id == chunk_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        rows = await _ensure_chunk_rows(db, ctx, [chunk_id])
        row = rows[0]
    row.last_accessed_at = _now()
    await db.commit()
    if row.status != "ready":
        from app.workers.media import ensure_video_chunks

        ensure_video_chunks.delay(str(ctx.item.id), [chunk_id])
    response = _chunk_out(row)
    await _warmup_neighbor_chunks(db, ctx, [chunk_id])
    return response


async def _ensure_frame_row(
    db: AsyncSession,
    ctx: VideoContext,
    frame_index: int,
    width: int,
    format_: FrameFormat,
) -> tuple[VideoFrameCache, bool, bool]:
    _safe_frame_range(ctx.metadata, frame_index, frame_index)
    stmt = select(VideoFrameCache).where(
        VideoFrameCache.dataset_item_id == ctx.item.id,
        VideoFrameCache.frame_index == frame_index,
        VideoFrameCache.width == width,
        VideoFrameCache.format == format_,
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    created = False
    now = _now()
    stale_pending = (
        row is not None
        and row.status == "pending"
        and row.updated_at is not None
        and now - row.updated_at > PENDING_FRAME_REQUEUE_AFTER
    )
    if row is None:
        # 并发安全: timeline scrub 时单帧 GET 与 prefetch 窗口可能同时为同一
        # (item, frame, width, format) 走到这里, select 都返回 None。用 SAVEPOINT 包住
        # INSERT, 撞 uq_video_frame_cache_item_frame_width_format 时只回滚该 savepoint
        # (不破坏 prefetch 批量里已 flush 的其它帧), 再 select 拿别的请求插入的行。
        try:
            async with db.begin_nested():
                row = VideoFrameCache(
                    dataset_item_id=ctx.item.id,
                    frame_index=frame_index,
                    width=width,
                    format=format_,
                    status="pending",
                )
                db.add(row)
                await db.flush()
            created = True
        except IntegrityError:
            row = (await db.execute(stmt)).scalar_one()
    row.last_accessed_at = now
    await db.flush()
    return row, created, stale_pending


def _frame_out(row: VideoFrameCache) -> VideoFrameOut:
    status = row.status if row.status in {"pending", "ready", "failed"} else "pending"
    result = "hit" if status == "ready" else "miss"
    VIDEO_FRAME_CACHE_TOTAL.labels(result=result, format=row.format).inc()
    format_: FrameFormat = "jpeg" if row.format == "jpeg" else "webp"
    return VideoFrameOut(
        frame_index=row.frame_index,
        width=row.width,
        format=format_,
        status=status,
        url=_asset_url(row.storage_key)
        if status == "ready" and row.storage_key
        else None,
        retry_after=3 if status == "pending" else None,
        error=row.error if status == "failed" else None,
    )


async def get_frame(
    db: AsyncSession,
    ctx: VideoContext,
    frame_index: int,
    width: int,
    format_: FrameFormat,
) -> VideoFrameOut:
    row, created, stale_pending = await _ensure_frame_row(
        db, ctx, frame_index, width, format_
    )
    should_enqueue = created or stale_pending or row.status == "failed"
    await db.commit()
    if should_enqueue:
        from app.workers.media import extract_video_frames

        extract_video_frames.delay(
            str(ctx.item.id),
            [{"frame_index": frame_index, "width": width, "format": format_}],
        )
    return _frame_out(row)


async def prefetch_frames(
    db: AsyncSession,
    ctx: VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
) -> VideoFramePrefetchResponse:
    ensured = [
        await _ensure_frame_row(db, ctx, frame_index, width, format_)
        for frame_index in sorted(set(frame_indices))
    ]
    rows = [row for row, _, _ in ensured]
    missing = [
        {"frame_index": row.frame_index, "width": row.width, "format": row.format}
        for row, created, stale_pending in ensured
        if created or stale_pending or row.status == "failed"
    ]
    await db.commit()
    if missing:
        from app.workers.media import extract_video_frames

        extract_video_frames.delay(str(ctx.item.id), missing)
    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.item.id,
        task_id=ctx.task_id,
        frames=[_frame_out(row) for row in rows],
    )


async def retry_frames(
    db: AsyncSession,
    ctx: VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
    *,
    force: bool = False,
) -> VideoFramePrefetchResponse:
    normalized = sorted(set(frame_indices))
    if normalized:
        if force:
            rows = [
                (await _ensure_frame_row(db, ctx, frame_index, width, format_))[0]
                for frame_index in normalized
            ]
        else:
            rows = (
                (
                    await db.execute(
                        select(VideoFrameCache)
                        .where(
                            VideoFrameCache.dataset_item_id == ctx.item.id,
                            VideoFrameCache.frame_index.in_(normalized),
                            VideoFrameCache.width == width,
                            VideoFrameCache.format == format_,
                            VideoFrameCache.status == "failed",
                        )
                        .order_by(VideoFrameCache.frame_index.asc())
                    )
                )
                .scalars()
                .all()
            )
    else:
        rows = (
            (
                await db.execute(
                    select(VideoFrameCache)
                    .where(
                        VideoFrameCache.dataset_item_id == ctx.item.id,
                        VideoFrameCache.width == width,
                        VideoFrameCache.format == format_,
                        VideoFrameCache.status == "failed",
                    )
                    .order_by(VideoFrameCache.frame_index.asc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )

    now = _now()
    requests = []
    for row in rows:
        row.status = "pending"
        row.error = None
        row.last_accessed_at = now
        if force:
            row.storage_key = None
            row.byte_size = None
        requests.append(
            {"frame_index": row.frame_index, "width": row.width, "format": row.format}
        )
    await db.commit()

    if requests:
        from app.workers.media import extract_video_frames

        extract_video_frames.delay(str(ctx.item.id), requests)

    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.item.id,
        task_id=ctx.task_id,
        frames=[_frame_out(row) for row in rows],
    )


async def manifest_v2(
    db: AsyncSession, ctx: VideoContext, base_url: str
) -> VideoManifestV2Response:
    _metadata_ready(ctx.metadata)
    from app.services.video_segment_service import ensure_segments, segment_out
    from app.db.models.project import Project
    from app.services.video_collaboration import collaboration_config

    segment_rows = await ensure_segments(db, ctx)
    project = await db.get(Project, ctx.task.project_id) if ctx.task else None
    collaboration = collaboration_config(project)
    await db.commit()
    base = base_url.rstrip("/")
    if ctx.task_id:
        service_base = f"{base}/api/v1/tasks/{ctx.task_id}/video"
        chunks_url = f"{service_base}/chunks"
        timetable_url = f"{service_base}/frame-timetable"
        frame_base = f"{service_base}/frames"
    else:
        service_base = f"{base}/api/v1/videos/{ctx.item.id}"
        chunks_url = f"{service_base}/chunks"
        timetable_url = f"{service_base}/frame-timetable"
        frame_base = f"{service_base}/frames"

    poster_key = ctx.metadata.poster_frame_path or ctx.item.thumbnail_path
    return VideoManifestV2Response(
        task_id=ctx.task_id,
        dataset_item_id=ctx.item.id,
        poster_url=_asset_url(poster_key) if poster_key else None,
        fps=ctx.metadata.fps,
        frame_count=ctx.metadata.frame_count,
        duration_ms=ctx.metadata.duration_ms,
        chunks_manifest_url=chunks_url,
        frame_timetable_url=timetable_url,
        frame_service_base=frame_base,
        chunk_size_frames=settings.video_chunk_size_frames,
        segments=[
            segment_out(
                row,
                frame_count=max(1, int(ctx.metadata.frame_count or 1)),
                segment_count=len(segment_rows),
                overlap_frames=(
                    collaboration.overlap_frames if collaboration.enabled else 0
                ),
            )
            for row in segment_rows
        ],
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
    key = (dataset_item_id, frame_index, width, format_)
    _FRAME_ARRAY_CACHE[key] = array
    _FRAME_ARRAY_CACHE.move_to_end(key)
    while len(_FRAME_ARRAY_CACHE) > max(0, settings.video_frame_memory_cache_items):
        _FRAME_ARRAY_CACHE.popitem(last=False)


def get_frame_array_cache(
    dataset_item_id: uuid.UUID, frame_index: int, width: int, format_: str
) -> Any | None:
    key = (dataset_item_id, frame_index, width, format_)
    array = _FRAME_ARRAY_CACHE.get(key)
    if array is not None:
        _FRAME_ARRAY_CACHE.move_to_end(key)
    return array


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


async def get_frame_array(
    db: AsyncSession,
    dataset_item_id: uuid.UUID,
    frame_index: int,
    width: int = 512,
    format_: FrameFormat = "webp",
) -> Any:
    cached = get_frame_array_cache(dataset_item_id, frame_index, width, format_)
    if cached is not None:
        return cached
    row = (
        await db.execute(
            select(VideoFrameCache).where(
                VideoFrameCache.dataset_item_id == dataset_item_id,
                VideoFrameCache.frame_index == frame_index,
                VideoFrameCache.width == width,
                VideoFrameCache.format == format_,
                VideoFrameCache.status == "ready",
            )
        )
    ).scalar_one_or_none()
    if row is None or not row.storage_key:
        raise RuntimeError("video frame is not cached")
    resp = storage_service.client.get_object(
        Bucket=storage_service.bucket_for_cache_key(row.storage_key),
        Key=row.storage_key,
    )
    array = image_bytes_to_array(resp["Body"].read())
    put_frame_array_cache(dataset_item_id, frame_index, width, format_, array)
    return array
