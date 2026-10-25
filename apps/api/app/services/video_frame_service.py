"""API-side video media composition over the independent Assets media owner."""

from __future__ import annotations

import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_media.contract import (
    AssetsRequest,
    ChunkSelection,
    FrameSelection,
    InternalSourceRequest,
    MediaAsset,
    MediaFailure,
    MediaPreferences,
    MediaSource,
    ReadSourceRequest,
    UsageRequest,
)

from app.config import settings
from app.db.models.task import Task
from app.services.project_access import get_workspace_project
from app.observability.metrics import (
    VIDEO_CHUNK_REQUESTS_TOTAL,
    VIDEO_FRAME_CACHE_TOTAL,
)
from app.schemas.task import VideoMetadata
from app.schemas.video_frame_service import (
    VideoChunkDiagnostics,
    VideoChunkOut,
    VideoChunksResponse,
    VideoFrameOut,
    VideoFramePrefetchResponse,
    VideoManifestV2Response,
)
from app.services.assets_media import browser_media_url, media_gateway, media_http_error
from app.services import video_context
from app.services.video_context_contract import VideoContextReader

FrameFormat = Literal["webp", "jpeg"]
_ASSET_PAGE_SIZE = 500
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AssetReferences:
    source: MediaSource
    assets: list[MediaAsset]
    video_url: str | None
    expires_in: int


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


def _video_context_from_source(
    source: MediaSource,
    *,
    actor_id: uuid.UUID,
    source_reference: Any,
    task_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
) -> video_context.VideoContext:
    is_current = (
        not source.source_drifted
        and source.materialized_generation == source.source_generation
    )
    return video_context.VideoContext(
        metadata=VideoMetadata.model_validate(
            source.video_metadata if is_current else {}
        ),
        dataset_item_id=source.dataset_item_id,
        source_file_path=source.file_path,
        thumbnail_path=source.thumbnail_path,
        source_width=source.width,
        source_height=source.height,
        task_id=task_id,
        project_id=project_id,
        actor_id=actor_id,
        source_reference=source_reference,
        source_snapshot=source.source_snapshot,
    )


class AssetsVideoContextReader(VideoContextReader):
    """Read video source state from Assets; retain only known Task facts locally."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def from_task(
        self,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        project_id: uuid.UUID,
        file_type: str,
        dataset_item_id: uuid.UUID | None,
    ) -> video_context.VideoContext:
        if file_type != "video" or not dataset_item_id:
            raise HTTPException(status_code=400, detail="Task is not a video task")
        return await self.from_dataset_item(
            actor_id=actor_id,
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            project_id=project_id,
        )

    async def from_dataset_item(
        self,
        *,
        actor_id: uuid.UUID,
        dataset_item_id: uuid.UUID,
        task_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
    ) -> video_context.VideoContext:
        try:
            async with media_gateway(self._db) as gateway:
                reference = await gateway.source_reference(
                    ReadSourceRequest(
                        actor_id=actor_id,
                        dataset_item_id=dataset_item_id,
                        task_id=task_id,
                    )
                )
        except MediaFailure as exc:
            raise media_http_error(exc) from None
        if reference.source.file_type != "video":
            raise HTTPException(status_code=404, detail="Video not found")
        return _video_context_from_source(
            reference.source,
            actor_id=actor_id,
            source_reference=reference,
            task_id=task_id,
            project_id=project_id,
        )

    async def from_task_summary(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        task_id: uuid.UUID,
        project_id: uuid.UUID,
        file_type: str,
        dataset_item_id: uuid.UUID | None,
    ) -> video_context.VideoContext:
        """Trusted accepted work reads metadata only; it never asks for a URL."""
        if file_type != "video" or not dataset_item_id:
            raise HTTPException(status_code=400, detail="Task is not a video task")
        if actor_id is not None:
            try:
                async with media_gateway(self._db) as gateway:
                    source = await gateway.call(
                        "read_media_source",
                        ReadSourceRequest(
                            actor_id=actor_id,
                            dataset_item_id=dataset_item_id,
                            task_id=task_id,
                        ),
                    )
            except MediaFailure as exc:
                raise media_http_error(exc) from None
            current = (
                not source.source_drifted
                and source.materialized_generation == source.source_generation
            )
            return video_context.VideoContext(
                metadata=VideoMetadata.model_validate(
                    source.video_metadata if current else {}
                ),
                dataset_item_id=dataset_item_id,
                source_file_path=source.file_path,
                thumbnail_path=source.thumbnail_path,
                source_width=source.width,
                source_height=source.height,
                task_id=task_id,
                project_id=project_id,
                actor_id=actor_id,
                source_snapshot=source.source_snapshot,
            )

        try:
            async with media_gateway(self._db) as gateway:
                source = await gateway.call(
                    "resolve_internal_media_source",
                    InternalSourceRequest(dataset_item_id=dataset_item_id),
                )
        except MediaFailure as exc:
            raise media_http_error(exc) from None
        if source.dataset_item_id != dataset_item_id or source.file_type != "video":
            raise HTTPException(status_code=404, detail="Video not found")
        current = (
            not source.source_drifted
            and source.materialized_generation == source.source_generation
        )
        return video_context.VideoContext(
            metadata=VideoMetadata.model_validate(
                source.video_metadata if current else {}
            ),
            dataset_item_id=dataset_item_id,
            source_file_path=source.file_path,
            thumbnail_path=source.thumbnail_path,
            source_width=source.width,
            source_height=source.height,
            task_id=task_id,
            project_id=project_id,
            source_snapshot=source.source_snapshot,
        )


def _actor_id(ctx: video_context.VideoContext) -> uuid.UUID:
    if ctx.actor_id is None:
        raise HTTPException(
            status_code=500, detail="Video actor context is unavailable"
        )
    return ctx.actor_id


def _selection_locators(selection: FrameSelection | ChunkSelection) -> list[int]:
    return (
        selection.frame_indices
        if isinstance(selection, FrameSelection)
        else selection.chunk_ids
    )


def _source_snapshot(source: MediaSource) -> tuple[Any, ...]:
    return (
        source.dataset_item_id,
        source.source_generation,
        source.source_snapshot,
        source.timetable_revision,
        source.materialized_generation,
        source.source_drifted,
        source.object_identity,
        source.file_path,
        source.video_metadata,
    )


async def _asset_references(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    selection: FrameSelection | ChunkSelection,
) -> _AssetReferences:
    async with media_gateway(db) as gateway:
        return await _asset_references_with_gateway(ctx, selection, gateway)


async def _asset_references_with_gateway(ctx, selection, gateway):
    locators = _selection_locators(selection)
    batches = (
        [
            locators[offset : offset + _ASSET_PAGE_SIZE]
            for offset in range(0, len(locators), _ASSET_PAGE_SIZE)
        ]
        if locators
        else [[]]
    )
    source: MediaSource | None = None
    video_url: str | None = None
    expires_in = 3600
    assets: list[MediaAsset] = []
    seen: set[int] = set()
    try:
        for batch in batches:
            page_selection = selection.model_copy(
                update={
                    "frame_indices"
                    if isinstance(selection, FrameSelection)
                    else "chunk_ids": batch
                }
            )
            references = await gateway.assets_reference(
                AssetsRequest(
                    actor_id=_actor_id(ctx),
                    dataset_item_id=ctx.dataset_item_id,
                    task_id=ctx.task_id,
                    selection=page_selection.model_copy(
                        update={"expected_source_snapshot": ctx.source_snapshot}
                    ),
                )
            )
            page_source = references.state.source
            if source is None:
                source = page_source
                video_url = references.video_url
                expires_in = references.expires_in
            elif _source_snapshot(source) != _source_snapshot(page_source):
                raise MediaFailure("source_changed")
            page_assets = references.state.assets
            page_locators = [
                asset.frame_index
                if isinstance(selection, FrameSelection)
                else asset.chunk_id
                for asset in page_assets
            ]
            if any(locator is None for locator in page_locators):
                raise MediaFailure("invalid_response")
            page_ids = [int(locator) for locator in page_locators]
            if len(set(page_ids)) != len(page_ids) or seen.intersection(page_ids):
                raise MediaFailure("invalid_response")
            if locators and not selection.failed_only and set(page_ids) != set(batch):
                raise MediaFailure("invalid_response")
            seen.update(page_ids)
            assets.extend(page_assets)
    except MediaFailure as exc:
        raise media_http_error(exc) from None

    if source is None:
        raise HTTPException(
            status_code=503, detail="Video media references unavailable"
        )
    if locators and not selection.failed_only and seen != set(locators):
        raise HTTPException(
            status_code=503, detail="Video media references unavailable"
        )
    return _AssetReferences(source, assets, video_url, expires_in)


def _chunk_out(asset: MediaAsset) -> VideoChunkOut:
    status = "pending" if asset.status in {"pending", "running"} else asset.status
    VIDEO_CHUNK_REQUESTS_TOTAL.labels(status=status).inc()
    diagnostics = (
        VideoChunkDiagnostics.model_validate(asset.diagnostics)
        if asset.diagnostics
        else None
    )
    return VideoChunkOut(
        chunk_id=asset.chunk_id,
        start_frame=asset.start_frame,
        end_frame=asset.end_frame,
        status=status,
        url=browser_media_url(asset.url) if status == "ready" else None,
        byte_size=asset.byte_size,
        generation_mode=asset.generation_mode,
        diagnostics=diagnostics,
        retry_after=3 if status == "pending" else None,
        error=asset.error if status == "failed" else None,
    )


def _frame_out(asset: MediaAsset) -> VideoFrameOut:
    status = "pending" if asset.status in {"pending", "running"} else asset.status
    format_: FrameFormat = "jpeg" if asset.format == "jpeg" else "webp"
    VIDEO_FRAME_CACHE_TOTAL.labels(
        result="hit" if status == "ready" else "miss", format=format_
    ).inc()
    return VideoFrameOut(
        frame_index=asset.frame_index,
        width=asset.width,
        format=format_,
        status=status,
        url=browser_media_url(asset.url) if status == "ready" else None,
        retry_after=3 if status == "pending" else None,
        error=asset.error if status == "failed" else None,
    )


async def _warmup_neighbor_chunks(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    requested_chunk_ids: list[int],
) -> None:
    try:
        async with media_gateway(db) as gateway:
            preferences: MediaPreferences = await gateway.call(
                "read_media_preferences", UsageRequest()
            )
            candidates = warmup_chunk_ids(
                requested_chunk_ids,
                _last_chunk_id(ctx.metadata),
                int(preferences.warmup_lookahead or 0),
            )
            if not candidates:
                return
            references = await _asset_references_with_gateway(
                ctx,
                ChunkSelection(
                    chunk_ids=candidates,
                    chunk_size_frames=max(1, settings.video_chunk_size_frames),
                ),
                gateway,
            )
            failed_ids = [
                asset.chunk_id
                for asset in references.assets
                if asset.status == "failed"
            ]
            if failed_ids:
                await _asset_references_with_gateway(
                    ctx,
                    ChunkSelection(
                        chunk_ids=failed_ids,
                        chunk_size_frames=max(1, settings.video_chunk_size_frames),
                        failed_only=True,
                    ),
                    gateway,
                )
        await db.commit()
    except Exception:  # noqa: BLE001
        with suppress(Exception):
            await db.rollback()
        log.exception("video chunk warmup failed; primary chunk response is retained")


async def list_chunks(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    from_frame: int | None,
    to_frame: int | None,
) -> VideoChunksResponse:
    start, end = _safe_frame_range(ctx.metadata, from_frame, to_frame)
    requested_ids = _chunk_ids_for_range(start, end)
    references = await _asset_references(
        db,
        ctx,
        ChunkSelection(
            chunk_ids=requested_ids,
            chunk_size_frames=max(1, settings.video_chunk_size_frames),
        ),
    )
    response = VideoChunksResponse(
        dataset_item_id=ctx.dataset_item_id,
        task_id=ctx.task_id,
        chunk_size_frames=settings.video_chunk_size_frames,
        fallback_video_url=browser_media_url(references.video_url),
        chunks=[_chunk_out(asset) for asset in references.assets],
    )
    await db.commit()
    await _warmup_neighbor_chunks(db, ctx, requested_ids)
    return response


async def get_chunk(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    chunk_id: int,
) -> VideoChunkOut:
    _metadata_ready(ctx.metadata)
    references = await _asset_references(
        db,
        ctx,
        ChunkSelection(
            chunk_ids=[chunk_id],
            chunk_size_frames=max(1, settings.video_chunk_size_frames),
        ),
    )
    if not references.assets:
        raise HTTPException(status_code=404, detail="Video chunk not found")
    response = _chunk_out(references.assets[0])
    await db.commit()
    await _warmup_neighbor_chunks(db, ctx, [chunk_id])
    return response


async def get_frame(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    frame_index: int,
    width: int,
    format_: FrameFormat,
) -> VideoFrameOut:
    _safe_frame_range(ctx.metadata, frame_index, frame_index)
    references = await _asset_references(
        db,
        ctx,
        FrameSelection(frame_indices=[frame_index], width=width, format=format_),
    )
    if not references.assets:
        raise HTTPException(status_code=404, detail="Video frame not found")
    response = _frame_out(references.assets[0])
    await db.commit()
    return response


async def prefetch_frames(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
) -> VideoFramePrefetchResponse:
    normalized = sorted(set(frame_indices))
    for frame_index in normalized:
        _safe_frame_range(ctx.metadata, frame_index, frame_index)
    references = await _asset_references(
        db,
        ctx,
        FrameSelection(frame_indices=normalized, width=width, format=format_),
    )
    await db.commit()
    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.dataset_item_id,
        task_id=ctx.task_id,
        frames=[_frame_out(asset) for asset in references.assets],
    )


async def retry_frames(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    frame_indices: list[int],
    width: int,
    format_: FrameFormat,
    *,
    force: bool = False,
    task_annotation_retry: bool = False,
) -> VideoFramePrefetchResponse:
    normalized = sorted(set(frame_indices))
    if normalized:
        for frame_index in normalized:
            _safe_frame_range(ctx.metadata, frame_index, frame_index)
    references = await _asset_references(
        db,
        ctx,
        FrameSelection(
            frame_indices=normalized,
            width=width,
            format=format_,
            force=force,
            failed_only=not force,
            intent="task_frame_retry" if task_annotation_retry else "access",
        ),
    )
    await db.commit()
    return VideoFramePrefetchResponse(
        dataset_item_id=ctx.dataset_item_id,
        task_id=ctx.task_id,
        frames=[_frame_out(asset) for asset in references.assets],
    )


async def manifest_v2(
    db: AsyncSession,
    ctx: video_context.VideoContext,
    base_url: str,
    *,
    task: Task | None = None,
) -> VideoManifestV2Response:
    _metadata_ready(ctx.metadata)
    reference = ctx.source_reference
    if reference is None:
        try:
            async with media_gateway(db) as gateway:
                reference = await gateway.source_reference(
                    ReadSourceRequest(
                        actor_id=_actor_id(ctx),
                        dataset_item_id=ctx.dataset_item_id,
                        task_id=ctx.task_id,
                    )
                )
        except MediaFailure as exc:
            raise media_http_error(exc) from None

    from app.services.video_segment_service import ensure_segments, segment_out
    from app.services.video_collaboration import collaboration_config

    segment_rows = await ensure_segments(db, ctx, task=task)
    project = (
        await get_workspace_project(db, project_id=ctx.project_id)
        if ctx.project_id
        else None
    )
    collaboration = collaboration_config(project)
    await db.commit()
    base = base_url.rstrip("/")
    if ctx.task_id:
        service_base = f"{base}/api/v1/tasks/{ctx.task_id}/video"
        chunks_url = f"{service_base}/chunks"
        timetable_url = f"{service_base}/frame-timetable"
        frame_base = f"{service_base}/frames"
    else:
        service_base = f"{base}/api/v1/videos/{ctx.dataset_item_id}"
        chunks_url = f"{service_base}/chunks"
        timetable_url = f"{service_base}/frame-timetable"
        frame_base = f"{service_base}/frames"

    return VideoManifestV2Response(
        task_id=ctx.task_id,
        dataset_item_id=ctx.dataset_item_id,
        video_url=browser_media_url(reference.video_url),
        poster_url=browser_media_url(reference.poster_url),
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


def derive_anchor_frames(frame_count: int, stride: int) -> list[int]:
    """Return sparse timetable grid anchors, including the last source frame."""
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
    """Keep stride-grid and keyframe timetable rows without changing their order."""
    stride = max(1, int(stride))
    if stride == 1 or not rows:
        return rows
    frame_count = max(row["frame_index"] for row in rows) + 1
    anchors = set(derive_anchor_frames(frame_count, stride))
    return [
        row for row in rows if row["frame_index"] in anchors or row.get("is_keyframe")
    ]


def resolve_pts_ms_sparse(
    frame_index: int,
    anchors: list[tuple[int, int]],
    fps: float | None,
    stride: int,
) -> int | None:
    """Resolve exact, interpolated, or FPS-extrapolated PTS from sparse anchors."""
    _ = stride
    if not anchors:
        if not fps:
            return None
        return int(round((frame_index / fps) * 1000))

    first_frame, first_pts = anchors[0]
    last_frame, last_pts = anchors[-1]
    if frame_index <= first_frame:
        if frame_index == first_frame:
            return first_pts
        if not fps:
            return None
        return int(round(first_pts + ((frame_index - first_frame) / fps) * 1000))
    if frame_index >= last_frame:
        if frame_index == last_frame:
            return last_pts
        if not fps:
            return None
        return int(round(last_pts + ((frame_index - last_frame) / fps) * 1000))

    lower_frame, lower_pts = first_frame, first_pts
    for upper_frame, upper_pts in anchors:
        if upper_frame == frame_index:
            return upper_pts
        if upper_frame > frame_index:
            span = upper_frame - lower_frame
            if span <= 0:
                return lower_pts
            ratio = (frame_index - lower_frame) / span
            return int(round(lower_pts + ratio * (upper_pts - lower_pts)))
        lower_frame, lower_pts = upper_frame, upper_pts
    return last_pts


def warmup_chunk_ids(
    requested_chunk_ids: list[int],
    last_chunk_id: int,
    look_ahead: int,
) -> list[int]:
    """Select adjacent forward chunks without overlap or past-the-end requests."""
    if look_ahead <= 0 or not requested_chunk_ids:
        return []
    frontier = max(requested_chunk_ids)
    requested = set(requested_chunk_ids)
    return [
        chunk_id
        for chunk_id in range(frontier + 1, frontier + 1 + look_ahead)
        if chunk_id <= last_chunk_id and chunk_id not in requested
    ]
