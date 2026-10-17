from __future__ import annotations

import asyncio
import ctypes
import gc
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.observability.metrics import (
    IMAGE_PYRAMID_ASSET_BYTES,
    IMAGE_PYRAMID_DECODED_PIXELS,
    IMAGE_PYRAMID_DERIVED_BYTES,
    IMAGE_PYRAMID_GC_TOTAL,
    IMAGE_PYRAMID_GENERATIONS_TOTAL,
    IMAGE_PYRAMID_PHASE_DURATION_SECONDS,
    IMAGE_PYRAMID_SOURCE_BYTES,
    IMAGE_PYRAMID_TEMP_BYTES,
    IMAGE_PYRAMID_TILE_COUNT,
)
from app.services.image_pyramid import (
    PYRAMID_OVERLAP,
    PYRAMID_TILE_SIZE,
    ImagePyramidError,
    ImagePyramidOwner,
    OwnerKind,
    acquire_generation_lease,
    build_manifest,
    canonical_manifest_bytes,
    expected_tile_dimensions,
    fail_generation,
    generation_prefix,
    load_owner,
    manifest_key,
    overview_key,
    prepare_generation,
    publish_generation,
    pyramid_levels,
    sha256_digest,
    source_identity,
    tile_key,
    utcnow,
    validate_dimensions,
)
from app.services.storage import StorageService
from app.workers.celery_app import celery_app
from app.workers.media_materialization import SqlAlchemyMediaMaterializationWriter

logger = logging.getLogger(__name__)
_ORPHAN_KEY_RE = re.compile(
    r"^image-pyramids/(?P<asset>[0-9a-f-]{36})/g(?P<generation>[1-9][0-9]*)/"
)


@dataclass
class GeneratedPyramid:
    source_fingerprint: str
    width: int
    height: int
    max_level: int
    manifest_storage_key: str
    manifest_digest: str
    overview_storage_key: str
    overview_width: int
    overview_height: int
    overview_digest: str
    tile_count: int
    retained_bytes: int
    temp_bytes: int


class _PhaseTimer:
    def __init__(self, phase: str):
        self.phase = phase
        self.started = 0.0
        self.outcome = "error"

    def __enter__(self):
        self.started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.outcome = "success" if exc_type is None else "error"
        IMAGE_PYRAMID_PHASE_DURATION_SECONDS.labels(
            phase=self.phase, outcome=self.outcome
        ).observe(max(0.0, time.monotonic() - self.started))
        return False


def _stable_error_code(exc: BaseException) -> str:
    if isinstance(exc, ImagePyramidError):
        return exc.code
    if isinstance(exc, (SoftTimeLimitExceeded, TimeoutError)):
        return "timeout"
    if isinstance(exc, ImportError):
        return "generator_unavailable"
    return "generation_failed"


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ImagePyramidError("timeout")


def _directory_size(path: Path, *, limit: int) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            total += entry.stat().st_size
            if total > limit:
                raise ImagePyramidError("resource_limit")
    return total


def _cleanup_stale_temp_directories() -> None:
    temp_root = Path(tempfile.gettempdir())
    cutoff = time.time() - settings.image_pyramid_lease_seconds
    for candidate in temp_root.glob("aap-image-pyramid-*"):
        try:
            if candidate.is_dir() and candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate)
        except OSError:
            logger.warning("failed to clean stale pyramid temp directory")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _download_source(
    storage: StorageService,
    owner: ImagePyramidOwner,
    output: Path,
    *,
    expected_size: int,
    deadline: float,
) -> str:
    digest = hashlib.sha256()
    written = 0
    try:
        response = storage.client.get_object(Bucket=owner.bucket, Key=owner.file_path)
        body = response["Body"]
        with output.open("xb") as stream:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if (
                    written > settings.image_pyramid_max_source_bytes
                    or written > settings.image_pyramid_max_temp_bytes
                ):
                    raise ImagePyramidError("resource_limit")
                digest.update(chunk)
                stream.write(chunk)
                _check_deadline(deadline)
    except ImagePyramidError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ImagePyramidError("source_unavailable") from exc
    if written != expected_size:
        raise ImagePyramidError("source_changed")
    return f"sha256:{digest.hexdigest()}"


def _resolve_srgb_profile() -> str:
    candidates = (
        Path(settings.image_pyramid_srgb_profile),
        Path("/usr/share/color/icc/colord/sRGB.icc"),
        Path("/usr/share/color/icc/sRGB.icc"),
        Path("/usr/share/color/icc/ghostscript/srgb.icc"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise ImagePyramidError("color_profile_unavailable")


def _normalized_image(source: Path):
    os.environ.setdefault(
        "VIPS_CONCURRENCY", str(settings.image_pyramid_vips_concurrency)
    )
    try:
        import pyvips
    except ImportError as exc:
        raise ImagePyramidError("generator_unavailable") from exc

    pyvips.cache_set_max(64)
    pyvips.cache_set_max_mem(64 * 1024 * 1024)
    pyvips.cache_set_max_files(16)
    try:
        image = pyvips.Image.new_from_file(
            str(source), access="sequential", fail_on="error"
        )
        if image.get_typeof("n-pages") and int(image.get("n-pages")) > 1:
            raise ImagePyramidError("multi_page_unsupported")
        image = image.autorot()
        if image.get_typeof("icc-profile-data"):
            image = image.icc_transform(_resolve_srgb_profile(), embedded=True)
        elif image.interpretation != "srgb":
            image = image.colourspace("srgb")
        return image
    except ImagePyramidError:
        raise
    except Exception as exc:  # noqa: BLE001 - pyvips uses one generic Error type
        raise ImagePyramidError("decode_failed") from exc


def _clear_vips_cache() -> None:
    try:
        import pyvips

        pyvips.cache_set_max(0)
        pyvips.cache_set_max_mem(0)
        pyvips.cache_set_max_files(0)
    except Exception:  # noqa: BLE001 - best-effort process hygiene
        pass
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except Exception:  # noqa: BLE001 - only glibc Linux exposes malloc_trim
        pass


def _generate_local(
    source: Path,
    output_dir: Path,
    *,
    owner: ImagePyramidOwner,
    deadline: float,
) -> tuple[Path, Path, int, int, int, int]:
    image = _normalized_image(source)
    width, height = int(image.width), int(image.height)
    validate_dimensions(width, height)
    if (
        owner.width is not None
        and owner.height is not None
        and (owner.width, owner.height) != (width, height)
    ):
        raise ImagePyramidError("dimension_mismatch")
    _check_deadline(deadline)

    base = output_dir / "pyramid"
    try:
        image.dzsave(
            str(base),
            layout="dz",
            depth="onepixel",
            tile_size=PYRAMID_TILE_SIZE,
            overlap=PYRAMID_OVERLAP,
            suffix=".webp[Q=88,strip]",
        )
        levels = pyramid_levels(width, height)
        overview_level = next(
            level
            for level in levels
            if level["width"] <= 512 and level["height"] <= 512
        )
        dzi_level = len(levels) - 1 - overview_level["level"]
        overview_source = Path(str(base) + "_files") / str(dzi_level) / "0_0.webp"
        overview_path = output_dir / "overview.webp"
        shutil.copyfile(overview_source, overview_path)
    except Exception as exc:  # noqa: BLE001
        raise ImagePyramidError("encode_failed") from exc
    _check_deadline(deadline)
    return (
        Path(str(base) + "_files"),
        overview_path,
        width,
        height,
        int(overview_level["width"]),
        int(overview_level["height"]),
    )


def _verify_local_tiles(
    dzi_root: Path,
    *,
    width: int,
    height: int,
    deadline: float,
) -> list[tuple[Path, int, int, int]]:
    try:
        from PIL import Image
    except ImportError as exc:  # Pillow is already a declared API dependency
        raise ImagePyramidError("generator_unavailable") from exc

    levels = pyramid_levels(width, height)
    dzi_max_level = len(levels) - 1
    verified: list[tuple[Path, int, int, int]] = []
    for level in levels:
        app_level = level["level"]
        dzi_level = dzi_max_level - app_level
        level_dir = dzi_root / str(dzi_level)
        for y in range(level["rows"]):
            for x in range(level["columns"]):
                local_path = level_dir / f"{x}_{y}.webp"
                if not local_path.is_file():
                    raise ImagePyramidError("tile_grid_mismatch")
                expected = expected_tile_dimensions(level, x, y)
                try:
                    with Image.open(local_path) as image:
                        actual = (int(image.width), int(image.height))
                        if image.format != "WEBP" or actual != expected:
                            raise ImagePyramidError("tile_grid_mismatch")
                except ImagePyramidError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise ImagePyramidError("tile_grid_mismatch") from exc
                verified.append((local_path, app_level, x, y))
                _check_deadline(deadline)
    actual_tiles = list(dzi_root.rglob("*.webp"))
    if len(actual_tiles) != len(verified):
        raise ImagePyramidError("tile_grid_mismatch")
    return verified


def _delete_prefix(storage: StorageService, prefix: str) -> int:
    return storage.delete_prefix(prefix, bucket=storage.media_cache_bucket)


def _upload_and_verify(
    storage: StorageService,
    *,
    asset_id: uuid.UUID,
    generation_number: int,
    tiles: list[tuple[Path, int, int, int]],
    overview_path: Path,
    manifest_bytes: bytes,
    deadline: float,
) -> tuple[str, str, int]:
    prefix = generation_prefix(asset_id, generation_number)
    expected: dict[str, int] = {}
    for local_path, level, x, y in tiles:
        key = tile_key(asset_id, generation_number, level, x, y)
        size = local_path.stat().st_size
        storage.upload_file(
            str(local_path),
            key,
            bucket=storage.media_cache_bucket,
            content_type="image/webp",
            cache_control="private, max-age=31536000, immutable",
        )
        expected[key] = size
        _check_deadline(deadline)

    ov_key = overview_key(asset_id, generation_number)
    storage.upload_file(
        str(overview_path),
        ov_key,
        bucket=storage.media_cache_bucket,
        content_type="image/webp",
        cache_control="private, max-age=31536000, immutable",
    )
    expected[ov_key] = overview_path.stat().st_size

    remote = {
        row["key"]: int(row["size"])
        for row in storage.list_objects(prefix + "/", bucket=storage.media_cache_bucket)
    }
    if remote != expected:
        raise ImagePyramidError("upload_verification_failed")

    mf_key = manifest_key(asset_id, generation_number)
    storage.client.put_object(
        Bucket=storage.media_cache_bucket,
        Key=mf_key,
        Body=manifest_bytes,
        ContentType="application/json",
        CacheControl="private, max-age=31536000, immutable",
    )
    manifest_head = storage.verify_upload(mf_key, bucket=storage.media_cache_bucket)
    if not manifest_head or int(manifest_head.get("ContentLength") or -1) != len(
        manifest_bytes
    ):
        raise ImagePyramidError("upload_verification_failed")
    retained = sum(expected.values()) + len(manifest_bytes)
    if retained > settings.image_pyramid_max_derived_bytes:
        raise ImagePyramidError("resource_limit")
    _check_deadline(deadline)
    return mf_key, ov_key, retained


def _build_generation(
    *,
    storage: StorageService,
    owner: ImagePyramidOwner,
    asset_id: uuid.UUID,
    generation_number: int,
    expected_source_size: int,
) -> GeneratedPyramid:
    deadline = time.monotonic() + settings.image_pyramid_job_timeout_seconds
    _cleanup_stale_temp_directories()
    with tempfile.TemporaryDirectory(prefix="aap-image-pyramid-") as temp:
        temp_dir = Path(temp)
        source_path = temp_dir / "source"
        output_dir = temp_dir / "generated"
        output_dir.mkdir(mode=0o700)
        with _PhaseTimer("download"):
            fingerprint = _download_source(
                storage,
                owner,
                source_path,
                expected_size=expected_source_size,
                deadline=deadline,
            )
        with _PhaseTimer("generate"):
            (
                dzi_root,
                overview_path,
                width,
                height,
                overview_width,
                overview_height,
            ) = _generate_local(source_path, output_dir, owner=owner, deadline=deadline)
        with _PhaseTimer("verify"):
            tiles = _verify_local_tiles(
                dzi_root, width=width, height=height, deadline=deadline
            )
            generated_bytes = _directory_size(
                output_dir, limit=settings.image_pyramid_max_derived_bytes
            )
            temp_bytes = source_path.stat().st_size + generated_bytes
            if temp_bytes > settings.image_pyramid_max_temp_bytes:
                raise ImagePyramidError("resource_limit")
            overview_digest = _hash_file(overview_path)
            manifest = build_manifest(
                generation=generation_number,
                source_fingerprint=fingerprint,
                width=width,
                height=height,
                overview_width=overview_width,
                overview_height=overview_height,
                overview_digest=overview_digest,
            )
            manifest_bytes = canonical_manifest_bytes(manifest)
            manifest_digest = sha256_digest(manifest_bytes)
        with _PhaseTimer("upload"):
            mf_key, ov_key, retained_bytes = _upload_and_verify(
                storage,
                asset_id=asset_id,
                generation_number=generation_number,
                tiles=tiles,
                overview_path=overview_path,
                manifest_bytes=manifest_bytes,
                deadline=deadline,
            )
        return GeneratedPyramid(
            source_fingerprint=fingerprint,
            width=width,
            height=height,
            max_level=len(pyramid_levels(width, height)) - 1,
            manifest_storage_key=mf_key,
            manifest_digest=manifest_digest,
            overview_storage_key=ov_key,
            overview_width=overview_width,
            overview_height=overview_height,
            overview_digest=overview_digest,
            tile_count=len(tiles),
            retained_bytes=retained_bytes,
            temp_bytes=temp_bytes,
        )


async def _run_image_pyramid(
    owner_kind: OwnerKind, owner_id: str, *, force: bool
) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    asset_id: uuid.UUID | None = None
    generation_number: int | None = None
    lease_token: uuid.UUID | None = None
    storage = StorageService()
    error_code = "generation_failed"
    try:
        async with session_factory() as db:
            owner = await load_owner(db, owner_kind, uuid.UUID(owner_id))
            generation = await prepare_generation(db, owner, force=force)
            await db.commit()
            if generation is None or generation.status == "ready":
                return
            asset_id = generation.asset_id
            generation_number = generation.generation
            lease_token = await acquire_generation_lease(
                db, asset_id, generation_number
            )
            await db.commit()
            if lease_token is None:
                return

        current_identity, expected_size = source_identity(owner, storage)
        if current_identity != generation.source_identity:
            raise ImagePyramidError("source_changed")
        storage.ensure_bucket(storage.media_cache_bucket)
        result = _build_generation(
            storage=storage,
            owner=owner,
            asset_id=asset_id,
            generation_number=generation_number,
            expected_source_size=expected_size,
        )
        current_identity, _ = source_identity(owner, storage)
        if current_identity != generation.source_identity:
            raise ImagePyramidError("source_changed")

        async with session_factory() as db:
            published = await publish_generation(
                db,
                asset_id=asset_id,
                generation_number=generation_number,
                lease_token=lease_token,
                source_fingerprint=result.source_fingerprint,
                width=result.width,
                height=result.height,
                max_level=result.max_level,
                manifest_storage_key=result.manifest_storage_key,
                manifest_digest=result.manifest_digest,
                overview_storage_key=result.overview_storage_key,
                overview_width=result.overview_width,
                overview_height=result.overview_height,
                overview_digest=result.overview_digest,
                tile_count=result.tile_count,
                retained_bytes=result.retained_bytes,
                task_thumbnail_writer=SqlAlchemyMediaMaterializationWriter(db),
            )
            await db.commit()
        if not published:
            _delete_prefix(storage, generation_prefix(asset_id, generation_number))
            raise ImagePyramidError("lease_lost")
        IMAGE_PYRAMID_SOURCE_BYTES.observe(expected_size)
        IMAGE_PYRAMID_DECODED_PIXELS.observe(result.width * result.height)
        IMAGE_PYRAMID_TILE_COUNT.observe(result.tile_count)
        IMAGE_PYRAMID_DERIVED_BYTES.observe(result.retained_bytes)
        IMAGE_PYRAMID_TEMP_BYTES.observe(result.temp_bytes)
        IMAGE_PYRAMID_GENERATIONS_TOTAL.labels(outcome="ready", error_code="none").inc()
    except Exception as exc:  # noqa: BLE001 - terminal worker boundary
        error_code = _stable_error_code(exc)
        logger.exception(
            "image pyramid generation failed",
            extra={
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "asset_id": str(asset_id) if asset_id else None,
                "generation": generation_number,
                "error_code": error_code,
            },
        )
        if asset_id is not None and generation_number is not None:
            try:
                _delete_prefix(storage, generation_prefix(asset_id, generation_number))
            except Exception:  # noqa: BLE001
                logger.exception("image pyramid partial-prefix cleanup failed")
            async with session_factory() as db:
                await fail_generation(
                    db,
                    asset_id=asset_id,
                    generation_number=generation_number,
                    lease_token=lease_token,
                    error_code=error_code,
                )
                await db.commit()
        IMAGE_PYRAMID_GENERATIONS_TOTAL.labels(
            outcome="failed", error_code=error_code
        ).inc()
    finally:
        _clear_vips_cache()
        await engine.dispose()


def enqueue_image_pyramid(
    owner_kind: OwnerKind, owner_id: uuid.UUID | str, *, force: bool = False
) -> str:
    result = generate_image_pyramid.delay(owner_kind, str(owner_id), force)
    return str(result.id)


@celery_app.task(
    bind=True,
    queue="image-pyramid",
    max_retries=0,
    soft_time_limit=settings.image_pyramid_job_timeout_seconds,
    time_limit=settings.image_pyramid_job_timeout_seconds + 60,
)
def generate_image_pyramid(
    self, owner_kind: OwnerKind, owner_id: str, force: bool = False
) -> None:
    asyncio.run(_run_image_pyramid(owner_kind, owner_id, force=force))


async def _reconcile_image_pyramids() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    storage = StorageService()
    cutoff = utcnow() - timedelta(hours=settings.image_pyramid_gc_grace_hours)
    try:
        async with session_factory() as db:
            expired = list(
                (
                    await db.execute(
                        select(ImagePyramidGeneration)
                        .where(
                            ImagePyramidGeneration.status == "building",
                            ImagePyramidGeneration.lease_expires_at <= utcnow(),
                        )
                        .limit(100)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for generation in expired:
                await fail_generation(
                    db,
                    asset_id=generation.asset_id,
                    generation_number=generation.generation,
                    lease_token=generation.lease_token,
                    error_code="lease_expired",
                )
            await db.commit()

        async with session_factory() as db:
            stale_rows = list(
                (
                    await db.execute(
                        select(ImagePyramidGeneration, ImagePyramidAsset)
                        .join(
                            ImagePyramidAsset,
                            ImagePyramidAsset.id == ImagePyramidGeneration.asset_id,
                        )
                        .where(
                            ImagePyramidGeneration.updated_at < cutoff,
                            or_(
                                ImagePyramidGeneration.status == "failed",
                                (ImagePyramidGeneration.status == "ready")
                                & (
                                    ImagePyramidGeneration.generation
                                    != ImagePyramidAsset.active_generation
                                ),
                            ),
                        )
                        .limit(100)
                    )
                ).all()
            )
            for generation, asset in stale_rows:
                _delete_prefix(
                    storage,
                    generation_prefix(generation.asset_id, generation.generation),
                )
                await db.delete(generation)
                IMAGE_PYRAMID_GC_TOTAL.labels(outcome="stale_generation").inc()
            await db.commit()

        response = storage.client.list_objects_v2(
            Bucket=storage.media_cache_bucket,
            Prefix="image-pyramids/",
            MaxKeys=1000,
        )
        candidates: dict[tuple[uuid.UUID, int], Any] = {}
        for row in response.get("Contents", []):
            match = _ORPHAN_KEY_RE.match(row["Key"])
            if not match or row["LastModified"] >= cutoff:
                continue
            key = (
                uuid.UUID(match.group("asset")),
                int(match.group("generation")),
            )
            candidates.setdefault(key, row["LastModified"])
            if len(candidates) >= 100:
                break
        if candidates:
            async with session_factory() as db:
                existing = set(
                    (
                        await db.execute(
                            select(
                                ImagePyramidGeneration.asset_id,
                                ImagePyramidGeneration.generation,
                            ).where(
                                tuple_(
                                    ImagePyramidGeneration.asset_id,
                                    ImagePyramidGeneration.generation,
                                ).in_(list(candidates))
                            )
                        )
                    ).all()
                )
            for asset_id, generation_number in candidates.keys() - existing:
                _delete_prefix(storage, generation_prefix(asset_id, generation_number))
                IMAGE_PYRAMID_GC_TOTAL.labels(outcome="orphan_prefix").inc()

        async with session_factory() as db:
            total = (
                await db.execute(
                    select(
                        func.coalesce(
                            func.sum(ImagePyramidGeneration.retained_bytes), 0
                        )
                    )
                    .join(
                        ImagePyramidAsset,
                        (ImagePyramidAsset.id == ImagePyramidGeneration.asset_id)
                        & (
                            ImagePyramidAsset.active_generation
                            == ImagePyramidGeneration.generation
                        ),
                    )
                    .where(ImagePyramidGeneration.status == "ready")
                )
            ).scalar_one()
            IMAGE_PYRAMID_ASSET_BYTES.set(int(total or 0))
    finally:
        await engine.dispose()


@celery_app.task(bind=True, queue="cleanup", max_retries=0)
def reconcile_image_pyramids(self) -> None:
    asyncio.run(_reconcile_image_pyramids())
