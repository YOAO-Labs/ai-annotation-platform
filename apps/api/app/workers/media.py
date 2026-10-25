import asyncio
import io
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models.dataset import (
    DatasetItem,
    VideoChunk,
    VideoFrameCache,
)
from app.observability.metrics import (
    VIDEO_FRAME_ASSET_BYTES,
)
from app.schemas.task import VideoMetadata
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

# Re-export decoder helpers for existing callers and test patch targets.
# Physical output publication belongs exclusively to media_derivatives.
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


def extract_video_poster(
    input_path: str | Path, output_path: str | Path, *, pass_fds=()
) -> None:
    from app.workers.media_process import run_process

    proc = run_process(
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
        pass_fds=pass_fds,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg poster extraction failed")


def transcode_video_for_browser(
    input_path: str | Path, output_path: str | Path, *, pass_fds=()
) -> None:
    from app.workers.media_process import run_process

    proc = run_process(
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
        pass_fds=pass_fds,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg browser transcode failed")


async def _generate_thumbnail(item_id: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.services.ordinary_thumbnails import generate

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as db:
            await generate(
                db,
                "dataset_item",
                uuid.UUID(item_id),
                encode=_thumbnail_from_path,
                dimensions_probe=_pyramid_dimensions,
                max_source_bytes=THUMBNAIL_MAX_SOURCE_BYTES,
            )
    finally:
        await engine.dispose()


async def _generate_video_metadata(item_id: str, *, delegation=None) -> None:
    from app.workers.media_derivatives import run_legacy

    await run_legacy("metadata", item_id, delegation=delegation)


async def _backfill_media(dataset_id: str, *, video_delegations=None) -> None:
    from sqlalchemy.ext.asyncio import (
        create_async_engine,
        async_sessionmaker,
        AsyncSession,
    )
    from sqlalchemy import and_, or_, select

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
                            DatasetItem.video_output_id.is_(None),
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
            await _generate_video_metadata(
                item_id, delegation=(video_delegations or {}).get(item_id)
            )


async def _generate_task_thumbnail(task_id: str, *, allow_pyramid: bool = True) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.services.ordinary_thumbnails import generate

    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as db:
            await generate(
                db,
                "task",
                uuid.UUID(task_id),
                encode=_thumbnail_from_path,
                dimensions_probe=_pyramid_dimensions,
                max_source_bytes=THUMBNAIL_MAX_SOURCE_BYTES,
                allow_pyramid=allow_pyramid,
            )
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


async def _ensure_video_chunks(
    item_id: str, chunk_ids: list[int], *, delegation=None
) -> None:
    from app.workers.media_derivatives import run_legacy

    await run_legacy("chunk", item_id, delegation=delegation, chunk_ids=chunk_ids)


async def _extract_video_frames(
    item_id: str, frame_requests: list[dict[str, Any]], *, delegation=None
) -> None:
    from app.workers.media_derivatives import run_legacy

    groups = {}
    for request in frame_requests:
        key = (request["width"], request["format"])
        groups.setdefault(key, []).append(request["frame_index"])
    for (width, fmt), indices in groups.items():
        await run_legacy(
            "frame",
            item_id,
            delegation=delegation,
            frame_indices=indices,
            width=width,
            fmt=fmt,
        )


async def _cleanup_video_frame_assets() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.services import storage_access, video_derivative_objects

    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await video_derivative_objects.reconcile(db)
            await _refresh_video_asset_bytes(db)
    finally:
        await storage_access.finish(asyncio.create_task(engine.dispose()))


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
def generate_video_metadata(self, item_id: str, delegation=None) -> None:
    asyncio.run(_generate_video_metadata(item_id, delegation=delegation))


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, queue="media")
def generate_task_thumbnail(self, task_id: str) -> None:
    asyncio.run(_generate_task_thumbnail(task_id))


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_media(self, dataset_id: str, video_delegations=None) -> None:
    asyncio.run(_backfill_media(dataset_id, video_delegations=video_delegations))


@celery_app.task(bind=True, max_retries=1, queue="media")
def backfill_tasks(self, project_id: str, allow_pyramid: bool = False) -> None:
    asyncio.run(_backfill_tasks(project_id, allow_pyramid=allow_pyramid))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def ensure_video_chunks(
    self, item_id: str, chunk_ids: list[int], delegation=None
) -> None:
    asyncio.run(_ensure_video_chunks(item_id, chunk_ids, delegation=delegation))


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, queue="media")
def extract_video_frames(
    self, item_id: str, frame_requests: list[dict[str, Any]], delegation=None
) -> None:
    asyncio.run(_extract_video_frames(item_id, frame_requests, delegation=delegation))


@celery_app.task(bind=True, max_retries=1, queue="media")
def cleanup_video_frame_assets(self) -> None:
    asyncio.run(_cleanup_video_frame_assets())


@celery_app.task(
    bind=True, max_retries=0, queue="media", soft_time_limit=1800, time_limit=0
)
def generate_video_derivatives(self, output_ids: list[str]) -> None:
    from app.workers.media_derivatives import run_outputs

    asyncio.run(run_outputs(output_ids))
