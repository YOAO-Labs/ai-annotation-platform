import asyncio
import io
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.db.models.dataset import (
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
    VideoFrameIndex,
)
from app.observability.metrics import (
    VIDEO_CHUNK_GENERATION_SECONDS,
    VIDEO_FRAME_ASSET_BYTES,
    VIDEO_FRAME_EXTRACTION_SECONDS,
)
from app.schemas.task import VideoMetadata
from app.services.video_frame_service import (
    cache_key_for_chunk,
    cache_key_for_frame,
    metadata_for_item,
    pts_ms_for_frame,
    source_key_for_item,
)
from app.workers.celery_app import celery_app
from app.workers.media_probe import (
    FFPROBE_TIMEOUT_SECONDS as FFPROBE_TIMEOUT_SECONDS,
    _parse_frame_time_ms as _parse_frame_time_ms,
    _parse_int as _parse_int,
    _parse_ratio as _parse_ratio,
    parse_ffprobe_frame_timetable as parse_ffprobe_frame_timetable,
    parse_ffprobe_packet_samples as parse_ffprobe_packet_samples,
    parse_ffprobe_video_metadata as parse_ffprobe_video_metadata,
    probe_chunk_samples as probe_chunk_samples,
    probe_video_file as probe_video_file,
    probe_video_frame_timetable as probe_video_frame_timetable,
)
from app.workers.media_codec import (
    _avc_codec_string as _avc_codec_string,
    _extract_decoder_config as _extract_decoder_config,
    _find_mp4_box_payload as _find_mp4_box_payload,
    _hevc_codec_string as _hevc_codec_string,
    _webcodecs_codec_string as _webcodecs_codec_string,
)

# v0.16.x 拆分：分块/单帧 ffmpeg 抽取已抽到 media_chunks/media_frames,此处冗余别名
# re-export,保持 `from app.workers.media import extract_video_chunk*/extract_video_frame_image
# /FFMPEG_*_TIMEOUT_SECONDS` 旧入口与 monkeypatch target 不变(调用方 _store_* 仍在
# 本模块以裸名调用,走 media 全局查找)。
from app.workers.media_chunks import (
    FFMPEG_CHUNK_TIMEOUT_SECONDS as FFMPEG_CHUNK_TIMEOUT_SECONDS,
    extract_video_chunk as extract_video_chunk,
    extract_video_chunk_smart_copy as extract_video_chunk_smart_copy,
)
from app.workers.media_frames import (
    FFMPEG_FRAME_TIMEOUT_SECONDS as FFMPEG_FRAME_TIMEOUT_SECONDS,
    extract_video_frame_image as extract_video_frame_image,
)
from app.config import settings

FFMPEG_POSTER_TIMEOUT_SECONDS = 60
FFMPEG_TRANSCODE_TIMEOUT_SECONDS = 600
BROWSER_PLAYABLE_VIDEO_CODECS = {"h264", "avc1"}
SMART_COPY_VIDEO_CODECS = {"h264", "avc1", "hevc", "h265"}
THUMBNAIL_MAX_SOURCE_BYTES = 512 * 1024 * 1024


def _pyramid_dimensions_from_stage(stage) -> tuple[int, int]:
    """Inspect the verified file without allocating or decoding its pixels."""
    import gc

    from app.services.image_pyramid import ImagePyramidError, validate_dimensions
    from app.services.storage_access import mark_unsettled

    try:
        import pyvips
    except (ImportError, OSError) as exc:
        raise ImagePyramidError("generator_unavailable") from exc

    image, dimensions, failure = None, None, None
    try:
        image = pyvips.Image.new_from_file(
            f"/proc/self/fd/{stage.file.fileno()}",
            access="sequential",
            fail_on="error",
        )
        width, height = int(image.width), int(image.height)
        orientation = image.get("orientation") if image.get_typeof("orientation") else 1
        if type(orientation) is not int or not 1 <= orientation <= 8:
            raise ImagePyramidError("decode_failed")
        if orientation in {5, 6, 7, 8}:
            width, height = height, width
        validate_dimensions(width, height)
        dimensions = width, height
    except ImagePyramidError as exc:
        failure = ImagePyramidError(exc.code)
        if getattr(exc, "storage_operation_unsettled", False):
            mark_unsettled(failure)
    except Exception as exc:
        failure = ImagePyramidError("decode_failed")
        if getattr(exc, "storage_operation_unsettled", False):
            mark_unsettled(failure)
    except BaseException as exc:
        failure = mark_unsettled(exc)
    finally:
        try:
            image = None
            pyvips.cache_set_max(0)
            pyvips.cache_set_max_mem(0)
            pyvips.cache_set_max_files(0)
            gc.collect()
        except BaseException:
            failure = mark_unsettled(ImagePyramidError("decode_failed"))
    if failure is not None:
        # An exception raised inside a libvips method may retain its image in
        # traceback frames. Export only the stable error after those frames end.
        raise failure
    return dimensions


async def _pyramid_dimensions(db, kind, identifier) -> tuple[int, int]:
    from app.services import image_pyramid_originals as originals

    source = await originals.pin(db, kind, identifier)
    authority = await originals.delegated(
        db, originals.system_authorization(kind, identifier, source)
    )
    async with originals.media(db, authority, source) as stage:
        return await originals.physical(_pyramid_dimensions_from_stage, stage)


def _thumbnail_from_path(source_path: Path) -> tuple[bytes, str]:
    from PIL import Image, ImageOps

    with Image.open(source_path) as source:
        img = ImageOps.exif_transpose(source).convert("RGB")
        max_side = 256
        w, h = img.size
        scale = min(max_side / w, max_side / h)
        thumb = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS
        )

        import blurhash as bh

        small = img.resize((32, 32), Image.Resampling.LANCZOS)
        hash_str = bh.encode(small, x_components=4, y_components=3)
        buf = io.BytesIO()
        thumb.save(buf, format="WEBP", quality=80)
        return buf.getvalue(), hash_str


def extract_video_poster(input_path: str | Path, output_path: str | Path) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(512,iw)':-2",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFMPEG_POSTER_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg poster extraction failed")


def transcode_video_for_browser(
    input_path: str | Path, output_path: str | Path
) -> None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TRANSCODE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg browser transcode failed")


async def _get_or_create_frame_cache_row(
    db,
    item_id: uuid.UUID,
    frame_index: int,
    width: int,
    format_: str,
) -> VideoFrameCache:
    row = (
        await db.execute(
            select(VideoFrameCache).where(
                VideoFrameCache.dataset_item_id == item_id,
                VideoFrameCache.frame_index == frame_index,
                VideoFrameCache.width == width,
                VideoFrameCache.format == format_,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = VideoFrameCache(
            dataset_item_id=item_id,
            frame_index=frame_index,
            width=width,
            format=format_,
            status="pending",
        )
        db.add(row)
        await db.flush()
    return row


async def _store_frame_cache_image(
    db,
    storage,
    item: DatasetItem,
    metadata,
    input_path: Path,
    tmp_dir: Path,
    row: VideoFrameCache,
) -> None:
    fmt = row.format if row.format in {"webp", "jpeg"} else "webp"
    ext = "jpg" if fmt == "jpeg" else "webp"
    output_path = tmp_dir / f"frame-{row.frame_index}-{row.width}.{ext}"
    pts_ms = await pts_ms_for_frame(db, item.id, row.frame_index, metadata)
    extract_video_frame_image(input_path, output_path, pts_ms or 0, row.width)
    key = cache_key_for_frame(item.id, row.frame_index, row.width, fmt)
    body = output_path.read_bytes()
    storage.ensure_bucket(storage.media_cache_bucket)
    storage.client.put_object(
        Bucket=storage.media_cache_bucket,
        Key=key,
        Body=body,
        ContentType="image/jpeg" if fmt == "jpeg" else "image/webp",
    )
    row.storage_key = key
    row.byte_size = len(body)
    row.status = "ready"
    row.error = None
    row.last_accessed_at = datetime.now(timezone.utc)


async def _generate_thumbnail(item_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from app.db.models.dataset import DatasetItem
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with SessionLocal() as db:
            item = await db.get(DatasetItem, uuid.UUID(item_id))
            if not item or item.file_type != "image":
                return
            from app.services.directory_policy import owns_media

            if await owns_media(db, item.id):
                return

            from app.services.reference_keys import reject_reference

            reject_reference(item.file_path)

            storage = StorageService()
            if item.width is None or item.height is None:
                item.width, item.height = await _pyramid_dimensions(
                    db, "dataset_item", item.id
                )
                await db.commit()
            from app.services.image_pyramid import pyramid_eligible

            if pyramid_eligible(item.width, item.height):
                if settings.image_pyramid_auto_generate:
                    from app.services import image_pyramid_originals as originals
                    from app.workers.image_pyramid import enqueue_image_pyramid

                    source = await originals.pin(db, "dataset_item", item.id)
                    enqueue_image_pyramid(
                        "dataset_item",
                        item.id,
                        delegation=originals.system_authorization(
                            "dataset_item", item.id, source
                        ),
                    )
                return

            try:
                head = storage.client.head_object(
                    Bucket=storage.datasets_bucket, Key=item.file_path
                )
                if int(head.get("ContentLength") or 0) > THUMBNAIL_MAX_SOURCE_BYTES:
                    raise RuntimeError("thumbnail_source_too_large")
            except Exception as exc:
                meta = dict(item.metadata_ or {})
                meta["thumbnail_error"] = str(exc)
                item.metadata_ = meta
                await db.commit()
                return

            try:
                with tempfile.TemporaryDirectory(prefix="anno-thumbnail-") as tmp:
                    source_path = Path(tmp) / "source"
                    with source_path.open("xb") as stream:
                        storage.client.download_fileobj(
                            Bucket=storage.datasets_bucket,
                            Key=item.file_path,
                            Fileobj=stream,
                        )
                    thumbnail_bytes, hash_str = _thumbnail_from_path(source_path)
            except Exception as exc:
                meta = dict(item.metadata_ or {})
                meta["thumbnail_error"] = str(exc)
                item.metadata_ = meta
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
                meta = dict(item.metadata_ or {})
                meta["thumbnail_error"] = str(exc)
                item.metadata_ = meta
                await db.commit()
                return

            item.thumbnail_path = thumb_key
            item.blurhash = hash_str
            await db.commit()
    finally:
        await engine.dispose()


async def _generate_video_metadata(item_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from app.db.models.dataset import DatasetItem
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        item = await db.get(DatasetItem, uuid.UUID(item_id))
        if not item or item.file_type != "video":
            return

        storage = StorageService()
        meta = dict(item.metadata_ or {})
        video_meta = dict(meta.get("video") or {})

        with tempfile.TemporaryDirectory(prefix="anno-video-") as tmp:
            suffix = Path(item.file_name).suffix or ".mp4"
            input_path = Path(tmp) / f"source{suffix}"
            playback_path = Path(tmp) / "playback.mp4"

            try:
                with input_path.open("wb") as fh:
                    storage.client.download_fileobj(
                        Bucket=storage.datasets_bucket,
                        Key=item.file_path,
                        Fileobj=fh,
                    )
            except Exception as exc:
                video_meta["probe_error"] = str(exc)
                meta["video"] = video_meta
                item.metadata_ = meta
                await db.commit()
                return

            try:
                video_meta.update(probe_video_file(input_path))
                video_meta.pop("probe_error", None)
            except subprocess.TimeoutExpired:
                video_meta["probe_error"] = (
                    f"ffprobe timed out after {FFPROBE_TIMEOUT_SECONDS}s"
                )
            except Exception as exc:
                video_meta["probe_error"] = str(exc)

            if not video_meta.get("probe_error"):
                try:
                    frame_timetable = probe_video_frame_timetable(input_path)
                    await db.execute(
                        delete(VideoFrameIndex).where(
                            VideoFrameIndex.dataset_item_id == item.id
                        )
                    )
                    db.add_all(
                        [
                            VideoFrameIndex(
                                dataset_item_id=item.id,
                                frame_index=entry["frame_index"],
                                pts_ms=entry["pts_ms"],
                                is_keyframe=entry["is_keyframe"],
                                pict_type=entry.get("pict_type"),
                                byte_offset=entry.get("byte_offset"),
                            )
                            for entry in frame_timetable
                        ]
                    )
                    video_meta["frame_timetable_frame_count"] = len(frame_timetable)
                    video_meta.pop("frame_timetable_error", None)
                except subprocess.TimeoutExpired:
                    video_meta["frame_timetable_error"] = (
                        f"ffprobe frame timetable timed out after {FFPROBE_TIMEOUT_SECONDS}s"
                    )
                except Exception as exc:
                    video_meta["frame_timetable_error"] = str(exc)

            if video_meta.get("width") is not None:
                item.width = int(video_meta["width"])
            if video_meta.get("height") is not None:
                item.height = int(video_meta["height"])

            if (
                video_meta.get("codec")
                and video_meta.get("codec") not in BROWSER_PLAYABLE_VIDEO_CODECS
            ):
                try:
                    transcode_video_for_browser(input_path, playback_path)
                    playback_key = f"playback/{item_id}.mp4"
                    storage.ensure_bucket(storage.media_cache_bucket)
                    storage.client.put_object(
                        Bucket=storage.media_cache_bucket,
                        Key=playback_key,
                        Body=playback_path.read_bytes(),
                        ContentType="video/mp4",
                    )
                    video_meta["playback_path"] = playback_key
                    video_meta["playback_codec"] = "h264"
                    video_meta.pop("playback_error", None)
                except subprocess.TimeoutExpired:
                    video_meta["playback_error"] = (
                        f"ffmpeg browser transcode timed out after {FFMPEG_TRANSCODE_TIMEOUT_SECONDS}s"
                    )
                except Exception as exc:
                    video_meta["playback_error"] = str(exc)

            poster_row = None
            start = time.perf_counter()
            try:
                poster_row = await _get_or_create_frame_cache_row(
                    db, item.id, 0, 512, "webp"
                )
                poster_row.status = "pending"
                poster_row.error = None
                await db.commit()
                await _store_frame_cache_image(
                    db,
                    storage,
                    item,
                    VideoMetadata.model_validate(video_meta),
                    input_path,
                    Path(tmp),
                    poster_row,
                )
                VIDEO_FRAME_EXTRACTION_SECONDS.labels(
                    outcome="success", format="webp"
                ).observe(time.perf_counter() - start)
                item.thumbnail_path = poster_row.storage_key
                video_meta["poster_frame_path"] = poster_row.storage_key
                video_meta.pop("poster_error", None)
            except subprocess.TimeoutExpired:
                if poster_row is not None:
                    poster_row.status = "failed"
                    poster_row.error = f"ffmpeg poster extraction timed out after {FFMPEG_FRAME_TIMEOUT_SECONDS}s"
                video_meta["poster_error"] = (
                    f"ffmpeg poster extraction timed out after {FFMPEG_FRAME_TIMEOUT_SECONDS}s"
                )
                VIDEO_FRAME_EXTRACTION_SECONDS.labels(
                    outcome="error", format="webp"
                ).observe(time.perf_counter() - start)
            except Exception as exc:
                if poster_row is not None:
                    poster_row.status = "failed"
                    poster_row.error = str(exc)
                video_meta["poster_error"] = str(exc)
                VIDEO_FRAME_EXTRACTION_SECONDS.labels(
                    outcome="error", format="webp"
                ).observe(time.perf_counter() - start)

        meta["video"] = video_meta
        item.metadata_ = meta
        await db.commit()

    await engine.dispose()


async def _backfill_media(dataset_id: str) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from sqlalchemy import and_, or_, select
    from app.db.models.dataset import DatasetItem

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        rows = await db.execute(
            select(DatasetItem.id, DatasetItem.file_type).where(
                DatasetItem.dataset_id == uuid.UUID(dataset_id),
                ~DatasetItem.file_path.startswith(
                    "__aap_reference__/", autoescape=True
                ),
                or_(
                    and_(
                        DatasetItem.file_type == "image",
                        DatasetItem.thumbnail_path.is_(None),
                    ),
                    and_(
                        DatasetItem.file_type == "video",
                        or_(
                            DatasetItem.metadata_["video"]["frame_count"].astext.is_(
                                None
                            ),
                            # poster(缩略图)缺失也需回填:seed 同步填了 video metadata
                            # (frame_count 非空)但从未生成 poster,thumbnail_path 仍为 NULL,
                            # 仅按 frame_count 判定会漏掉这类视频。_generate_video_metadata
                            # 幂等(重建 timetable + poster),重复回填安全。
                            DatasetItem.thumbnail_path.is_(None),
                        ),
                    ),
                ),
            )
        )
        items = [(str(row.id), row.file_type) for row in rows.all()]

    await engine.dispose()

    for item_id, file_type in items:
        if file_type == "image":
            await _generate_thumbnail(item_id)
        elif file_type == "video":
            await _generate_video_metadata(item_id)


async def _generate_task_thumbnail(task_id: str, *, allow_pyramid: bool = True) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from app.db.models.task import Task
    from app.db.models.dataset import DatasetItem  # noqa: F401 — needed for FK resolution
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        async with SessionLocal() as db:
            task = await db.get(Task, uuid.UUID(task_id))
            if not task or task.file_type != "image":
                return
            from app.services.directory_policy import owns_media

            if await owns_media(db, task.dataset_item_id):
                return

            from app.services.reference_keys import reject_reference

            reject_reference(task.file_path)

            storage = StorageService()
            dimensions = (
                await _pyramid_dimensions(db, "task", task.id)
                if allow_pyramid
                else storage.read_image_dimensions(
                    task.file_path, bucket=storage.bucket
                )
            )
            from app.services.image_pyramid import pyramid_eligible

            if dimensions and pyramid_eligible(*dimensions):
                if allow_pyramid and settings.image_pyramid_auto_generate:
                    from app.services import image_pyramid_originals as originals
                    from app.workers.image_pyramid import enqueue_image_pyramid

                    source = await originals.pin(db, "task", task.id)
                    enqueue_image_pyramid(
                        "task",
                        task.id,
                        force=True,
                        delegation=originals.system_authorization(
                            "task", task.id, source
                        ),
                    )
                return
            try:
                head = storage.client.head_object(
                    Bucket=storage.bucket, Key=task.file_path
                )
                if int(head.get("ContentLength") or 0) > THUMBNAIL_MAX_SOURCE_BYTES:
                    return
                with tempfile.TemporaryDirectory(prefix="anno-task-thumbnail-") as tmp:
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

            task.thumbnail_path = thumb_key
            task.blurhash = hash_str
            await db.commit()
    finally:
        await engine.dispose()


async def _backfill_tasks(project_id: str, *, allow_pyramid: bool = False) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from sqlalchemy import select
    from app.db.models.task import Task
    from app.db.models.dataset import DatasetItem  # noqa: F401 — needed for FK resolution

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        rows = await db.execute(
            select(Task.id).where(
                Task.project_id == uuid.UUID(project_id),
                Task.file_type == "image",
                Task.dataset_item_id.is_(None),
                Task.thumbnail_path.is_(None),
            )
        )
        task_ids = [str(r[0]) for r in rows.all()]

    await engine.dispose()

    for tid in task_ids:
        await _generate_task_thumbnail(tid, allow_pyramid=allow_pyramid)


def _normalize_video_codec(codec: str | None) -> str | None:
    if not codec:
        return None
    normalized = codec.lower().strip()
    if normalized in {"avc1", "h264"}:
        return "h264"
    if normalized in {"hevc", "h265"}:
        return "hevc"
    return normalized


def _chunk_source_codec(metadata: VideoMetadata) -> str | None:
    if metadata.playback_path:
        codec = metadata.playback_codec or metadata.codec
    else:
        codec = metadata.codec
    return _normalize_video_codec(codec)


def _transcode_fallback_reason(
    source_codec: str | None,
    keyframe_aligned: bool | None,
    has_start_timetable: bool,
) -> str | None:
    if not source_codec:
        return "missing_source_codec"
    if source_codec not in SMART_COPY_VIDEO_CODECS:
        return "unsupported_codec"
    if not has_start_timetable:
        return "missing_start_frame_timetable"
    if not keyframe_aligned:
        return "start_frame_not_keyframe"
    return None


def _chunk_duration_ms(row: VideoChunk, metadata: VideoMetadata) -> int:
    if row.start_pts_ms is not None and row.end_pts_ms is not None:
        frame_ms = int(round(1000 / metadata.fps)) if metadata.fps else 1
        return max(1, row.end_pts_ms - row.start_pts_ms + frame_ms)
    if metadata.fps:
        frame_count = max(1, row.end_frame - row.start_frame + 1)
        return max(1, int(round((frame_count / metadata.fps) * 1000)))
    return 1


async def _chunk_diagnostics(
    db,
    item: DatasetItem,
    metadata: VideoMetadata,
    row: VideoChunk,
) -> dict[str, Any]:
    frame_rows = {
        frame.frame_index: frame
        for frame in (
            await db.execute(
                select(VideoFrameIndex).where(
                    VideoFrameIndex.dataset_item_id == item.id,
                    VideoFrameIndex.frame_index.in_([row.start_frame, row.end_frame]),
                )
            )
        )
        .scalars()
        .all()
    }
    start_entry = frame_rows.get(row.start_frame)
    end_entry = frame_rows.get(row.end_frame)
    source_codec = _chunk_source_codec(metadata)
    keyframe_aligned = start_entry.is_keyframe if start_entry else None
    fallback_reason = _transcode_fallback_reason(
        source_codec,
        keyframe_aligned,
        start_entry is not None,
    )
    return {
        "source_codec": source_codec,
        "output_codec": None,
        "keyframe_aligned": keyframe_aligned,
        "start_byte_offset": start_entry.byte_offset if start_entry else None,
        "end_byte_offset": end_entry.byte_offset if end_entry else None,
        "smart_copy_eligible": fallback_reason is None,
        "fallback_reason": fallback_reason,
    }


def _extract_chunk_samples(
    chunk_path: Path,
    start_frame: int,
    metadata: VideoMetadata,
    output_codec: str | None,
) -> dict[str, Any]:
    """扫 chunk mp4 的 packet, 提取 sample manifest (WebCodecs demux 用)。失败返回 {}。"""
    try:
        samples = probe_chunk_samples(chunk_path, start_frame)
        if not samples:
            return {}
        codec_string, description = _extract_decoder_config(chunk_path)
        result: dict[str, Any] = {
            "codec_string": codec_string or _webcodecs_codec_string(output_codec),
            "width": metadata.width or 0,
            "height": metadata.height or 0,
            "samples": samples,
        }
        if description:
            result["description"] = description
        return result
    except Exception:
        return {}


async def _store_video_chunk(
    db,
    storage,
    item: DatasetItem,
    metadata: VideoMetadata,
    input_path: Path,
    tmp_dir: Path,
    row: VideoChunk,
) -> None:
    diagnostics = await _chunk_diagnostics(db, item, metadata, row)
    start_ms = row.start_pts_ms
    if start_ms is None:
        start_ms = await pts_ms_for_frame(db, item.id, row.start_frame, metadata)

    output_path = tmp_dir / f"chunk-{row.chunk_id}.mp4"
    generation_mode = "transcode"
    if diagnostics["smart_copy_eligible"]:
        try:
            extract_video_chunk_smart_copy(
                input_path,
                output_path,
                start_ms or 0,
                _chunk_duration_ms(row, metadata),
            )
            generation_mode = "smart_copy"
            diagnostics["output_codec"] = diagnostics["source_codec"]
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            diagnostics["fallback_reason"] = f"smart_copy_failed: {exc}"
            diagnostics["smart_copy_eligible"] = False

    if generation_mode == "transcode":
        row.generation_mode = generation_mode
        row.diagnostics = diagnostics
        extract_video_chunk(
            input_path,
            output_path,
            start_ms or 0,
            row.end_frame - row.start_frame + 1,
        )
        diagnostics["output_codec"] = "h264"

    key = cache_key_for_chunk(item.id, row.chunk_id)
    storage.ensure_bucket(storage.media_cache_bucket)
    body = output_path.read_bytes()
    storage.client.put_object(
        Bucket=storage.media_cache_bucket,
        Key=key,
        Body=body,
        ContentType="video/mp4",
    )
    row.storage_key = key
    row.byte_size = len(body)
    row.generation_mode = generation_mode
    sample_meta = _extract_chunk_samples(
        output_path, row.start_frame, metadata, diagnostics.get("output_codec")
    )
    if sample_meta:
        diagnostics.update(sample_meta)
    row.diagnostics = diagnostics
    row.status = "ready"
    row.error = None
    row.last_accessed_at = datetime.now(timezone.utc)


async def _ensure_video_chunks(item_id: str, chunk_ids: list[int]) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        item = await db.get(DatasetItem, uuid.UUID(item_id))
        if not item or item.file_type != "video":
            await engine.dispose()
            return
        metadata = metadata_for_item(item)
        source_key = source_key_for_item(item)
        storage = StorageService()

        with tempfile.TemporaryDirectory(prefix="anno-video-chunk-") as tmp:
            suffix = Path(item.file_name).suffix or ".mp4"
            input_path = Path(tmp) / f"source{suffix}"
            try:
                with input_path.open("wb") as fh:
                    storage.client.download_fileobj(
                        Bucket=storage.datasets_bucket,
                        Key=source_key,
                        Fileobj=fh,
                    )
            except Exception as exc:
                rows = (
                    await db.execute(
                        select(VideoChunk).where(
                            VideoChunk.dataset_item_id == item.id,
                            VideoChunk.chunk_id.in_(chunk_ids),
                        )
                    )
                ).scalars()
                for row in rows:
                    row.status = "failed"
                    row.error = str(exc)
                await db.commit()
                await engine.dispose()
                return

            for chunk_id in chunk_ids:
                row = (
                    await db.execute(
                        select(VideoChunk).where(
                            VideoChunk.dataset_item_id == item.id,
                            VideoChunk.chunk_id == int(chunk_id),
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    continue
                row.status = "pending"
                row.error = None
                row.generation_mode = None
                row.diagnostics = {}
                await db.commit()
                start = time.perf_counter()
                try:
                    await _store_video_chunk(
                        db,
                        storage,
                        item,
                        metadata,
                        input_path,
                        Path(tmp),
                        row,
                    )
                    VIDEO_CHUNK_GENERATION_SECONDS.labels(outcome="success").observe(
                        time.perf_counter() - start
                    )
                except Exception as exc:
                    row.status = "failed"
                    row.error = str(exc)
                    VIDEO_CHUNK_GENERATION_SECONDS.labels(outcome="error").observe(
                        time.perf_counter() - start
                    )
                await db.commit()

        await _refresh_video_asset_bytes(db)

    await engine.dispose()


async def _extract_video_frames(
    item_id: str, frame_requests: list[dict[str, Any]]
) -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        item = await db.get(DatasetItem, uuid.UUID(item_id))
        if not item or item.file_type != "video":
            await engine.dispose()
            return
        metadata = metadata_for_item(item)
        source_key = source_key_for_item(item)
        storage = StorageService()

        with tempfile.TemporaryDirectory(prefix="anno-video-frame-") as tmp:
            suffix = Path(item.file_name).suffix or ".mp4"
            input_path = Path(tmp) / f"source{suffix}"
            try:
                with input_path.open("wb") as fh:
                    storage.client.download_fileobj(
                        Bucket=storage.datasets_bucket,
                        Key=source_key,
                        Fileobj=fh,
                    )
            except Exception as exc:
                for request in frame_requests:
                    row = await _load_frame_cache_row(db, item.id, request)
                    if row:
                        row.status = "failed"
                        row.error = str(exc)
                await db.commit()
                await engine.dispose()
                return

            for request in frame_requests:
                row = await _load_frame_cache_row(db, item.id, request)
                if row is None:
                    continue
                if row.status == "ready" and row.storage_key:
                    continue
                row.status = "pending"
                row.error = None
                await db.commit()
                fmt = row.format if row.format in {"webp", "jpeg"} else "webp"
                start = time.perf_counter()
                try:
                    await _store_frame_cache_image(
                        db,
                        storage,
                        item,
                        metadata,
                        input_path,
                        Path(tmp),
                        row,
                    )
                    VIDEO_FRAME_EXTRACTION_SECONDS.labels(
                        outcome="success", format=fmt
                    ).observe(time.perf_counter() - start)
                except Exception as exc:
                    row.status = "failed"
                    row.error = str(exc)
                    VIDEO_FRAME_EXTRACTION_SECONDS.labels(
                        outcome="error", format=fmt
                    ).observe(time.perf_counter() - start)
                await db.commit()

        await _refresh_video_asset_bytes(db)

    await engine.dispose()


async def _load_frame_cache_row(
    db, item_id: uuid.UUID, request: dict[str, Any]
) -> VideoFrameCache | None:
    return (
        await db.execute(
            select(VideoFrameCache).where(
                VideoFrameCache.dataset_item_id == item_id,
                VideoFrameCache.frame_index == int(request["frame_index"]),
                VideoFrameCache.width == int(request["width"]),
                VideoFrameCache.format == str(request["format"]),
            )
        )
    ).scalar_one_or_none()


async def _cleanup_video_frame_assets() -> None:
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from app.services.storage import StorageService

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with SessionLocal() as db:
        storage = StorageService()
        now = datetime.now(timezone.utc)
        frame_cutoff = now - timedelta(days=settings.video_frame_cache_ttl_days)
        chunk_cutoff = now - timedelta(days=settings.video_chunk_cache_ttl_days)

        frame_rows = (
            await db.execute(
                select(VideoFrameCache).where(
                    VideoFrameCache.status == "ready",
                    VideoFrameCache.last_accessed_at.is_not(None),
                    VideoFrameCache.last_accessed_at < frame_cutoff,
                )
            )
        ).scalars()
        for row in frame_rows:
            if row.storage_key:
                storage.delete_object(
                    row.storage_key,
                    bucket=storage.bucket_for_cache_key(row.storage_key),
                )
            row.status = "pending"
            row.storage_key = None
            row.byte_size = None
            row.generation_mode = None
            row.diagnostics = {}

        chunk_rows = (
            await db.execute(
                select(VideoChunk).where(
                    VideoChunk.status == "ready",
                    VideoChunk.last_accessed_at.is_not(None),
                    VideoChunk.last_accessed_at < chunk_cutoff,
                )
            )
        ).scalars()
        for row in chunk_rows:
            if row.storage_key:
                storage.delete_object(
                    row.storage_key,
                    bucket=storage.bucket_for_cache_key(row.storage_key),
                )
            row.status = "pending"
            row.storage_key = None
            row.byte_size = None

        await db.commit()
        await _refresh_video_asset_bytes(db)

    await engine.dispose()


async def _refresh_video_asset_bytes(db) -> None:
    frame_bytes = sum(
        row.byte_size or 0
        for row in (
            await db.execute(
                select(VideoFrameCache).where(VideoFrameCache.status == "ready")
            )
        )
        .scalars()
        .all()
    )
    chunk_bytes = sum(
        row.byte_size or 0
        for row in (
            await db.execute(select(VideoChunk).where(VideoChunk.status == "ready"))
        )
        .scalars()
        .all()
    )
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="frame").set(frame_bytes)
    VIDEO_FRAME_ASSET_BYTES.labels(asset_type="chunk").set(chunk_bytes)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_thumbnail(self, item_id: str) -> None:
    asyncio.run(_generate_thumbnail(item_id))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_video_metadata(self, item_id: str) -> None:
    asyncio.run(_generate_video_metadata(item_id))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_task_thumbnail(self, task_id: str) -> None:
    asyncio.run(_generate_task_thumbnail(task_id))


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_media(self, dataset_id: str) -> None:
    asyncio.run(_backfill_media(dataset_id))


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_tasks(self, project_id: str, allow_pyramid: bool = False) -> None:
    asyncio.run(_backfill_tasks(project_id, allow_pyramid=allow_pyramid))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def ensure_video_chunks(self, item_id: str, chunk_ids: list[int]) -> None:
    asyncio.run(_ensure_video_chunks(item_id, chunk_ids))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def extract_video_frames(
    self, item_id: str, frame_requests: list[dict[str, Any]]
) -> None:
    asyncio.run(_extract_video_frames(item_id, frame_requests))


@celery_app.task(bind=True, max_retries=1, queue="media")
def cleanup_video_frame_assets(self) -> None:
    asyncio.run(_cleanup_video_frame_assets())
