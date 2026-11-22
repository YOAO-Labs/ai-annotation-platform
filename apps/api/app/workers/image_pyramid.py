from __future__ import annotations

import asyncio
import ctypes
import gc
import hashlib
import logging
import os
import re
import shutil
import time
import uuid
import traceback
import sys
from functools import wraps
from dataclasses import dataclass
from pathlib import Path

from botocore.config import Config
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.image_pyramid import (
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
    load_owner,
    prepare_generation,
    publish_generation,
    pyramid_levels,
    sha256_digest,
    validate_dimensions,
)
from app.services import media_cache_accounting
from app.services import (
    image_pyramid_originals as originals,
    image_pyramid_objects as objects,
)
from app.services import image_pyramid_storage as physical, storage_access
from app.services.content_snapshot import ContentError
from app.services.storage import StorageService
from app.workers._db import task_session_factory
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
_ORPHAN_KEY_RE = re.compile(
    r"^image-pyramids/(?P<asset>[0-9a-f-]{36})/g(?P<generation>[1-9][0-9]*)/"
)


async def _execute_image_media(job_id: str | None) -> bool:
    from app.services.assets_images import run_image_job

    async with task_session_factory() as factory:
        return await run_image_job(
            uuid.UUID(job_id) if job_id else None, session_factory=factory
        )


async def _reconcile_image_media() -> None:
    from app.services.assets_images import maintain_images

    async with task_session_factory() as factory:
        result = await maintain_images(session_factory=factory)
    if result is not None:
        execute_image_media.delay(None)


@celery_app.task(
    bind=True,
    name="app.workers.image_pyramid.execute_image_media",
    max_retries=3,
    default_retry_delay=30,
    queue="image-pyramid",
)
def execute_image_media(self, job_id: str | None = None) -> None:
    """Wake one accepted job, or drain one item from the accepted pool."""
    try:
        if asyncio.run(_execute_image_media(job_id)):
            execute_image_media.delay(None)
    except Exception as exc:  # noqa: BLE001 - celery boundary hides source details
        logger.warning(
            "accepted image job wake deferred (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from None


@celery_app.task(
    bind=True,
    name="app.workers.image_pyramid.reconcile_image_media",
    queue="cleanup",
)
def reconcile_image_media(self) -> None:
    """Run local Assets reconciliation; a selected remote owner polls itself."""
    try:
        asyncio.run(_reconcile_image_media())
    except Exception as exc:  # noqa: BLE001 - celery boundary hides source details
        logger.warning(
            "accepted image reconciliation deferred (%s)",
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from None


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
    inventory: dict


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
    if isinstance(
        exc, (ImagePyramidError, ContentError, physical.ImagePyramidStorageError)
    ):
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


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with storage_access.closing_registered(path.open("rb")) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
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


def _native(call):
    """Release decoder references held by failed third-party traceback frames."""

    @wraps(call)
    def guarded(*args, **kwargs):
        failure = None
        try:
            return call(*args, **kwargs)
        except BaseException as exc:
            traceback.clear_frames(exc.__traceback__)
            exc.__traceback__ = exc.__cause__ = exc.__context__ = None
            failure = exc
        raise failure

    return guarded


@_native
def _normalized_image(source: Path):
    os.environ.setdefault(
        "VIPS_CONCURRENCY", str(settings.image_pyramid_vips_concurrency)
    )
    try:
        import pyvips
    except (ImportError, OSError) as exc:
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
        error = ImagePyramidError("decode_failed")
        if storage_access.failure_unsettled(exc):
            storage_access.mark_unsettled(error)
        raise error from exc


def _clear_vips_cache() -> None:
    pyvips = sys.modules.get("pyvips")
    if pyvips is None:
        return
    try:
        pyvips.cache_set_max(0)
        pyvips.cache_set_max_mem(0)
        pyvips.cache_set_max_files(0)
    except ImportError:
        return
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        gc.collect()
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except Exception:  # noqa: BLE001 - only glibc Linux exposes malloc_trim
        pass


@_native
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
        error = ImagePyramidError("encode_failed")
        if storage_access.failure_unsettled(exc):
            storage_access.mark_unsettled(error)
        raise error from exc
    _check_deadline(deadline)
    return (
        Path(str(base) + "_files"),
        overview_path,
        width,
        height,
        int(overview_level["width"]),
        int(overview_level["height"]),
    )


@_native
def _verify_local_tiles(
    dzi_root: Path,
    *,
    width: int,
    height: int,
    deadline: float,
) -> list[tuple[Path, int, int, int]]:
    try:
        from PIL import Image
    except ImportError as exc:
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
                    with storage_access.closing_registered(
                        Image.open(local_path)
                    ) as image:
                        actual = (int(image.width), int(image.height))
                        if image.format != "WEBP" or actual != expected:
                            raise ImagePyramidError("tile_grid_mismatch")
                except ImagePyramidError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    error = ImagePyramidError("tile_grid_mismatch")
                    if storage_access.failure_unsettled(exc):
                        storage_access.mark_unsettled(error)
                    raise error from exc
                verified.append((local_path, app_level, x, y))
                _check_deadline(deadline)
    actual_tiles = list(dzi_root.rglob("*.webp"))
    if len(actual_tiles) != len(verified):
        raise ImagePyramidError("tile_grid_mismatch")
    return verified


def _build_generation(
    *,
    storage: StorageService,
    owner: ImagePyramidOwner,
    asset_id: uuid.UUID,
    generation_number: int,
    expected_source_size: int,
    stage,
    output,
    native_stage,
    accounting=None,
) -> GeneratedPyramid:
    deadline = time.monotonic() + settings.image_pyramid_job_timeout_seconds
    with native_stage as temp_dir:
        source_path = Path(f"/proc/self/fd/{stage.file.fileno()}")
        output_dir = temp_dir / "generated"
        output_dir.mkdir(mode=0o700)
        if stage.sha256 != owner.source.sha256 or stage.size != expected_source_size:
            raise ImagePyramidError("source_changed")
        fingerprint = "sha256:" + stage.sha256
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
            mf_key, ov_key, retained_bytes, inventory = physical.upload_and_verify(
                storage,
                output,
                tiles=tiles,
                overview_path=overview_path,
                manifest_bytes=manifest_bytes,
                deadline=deadline,
                **({"accounting": accounting} if accounting is not None else {}),
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
            inventory=inventory,
        )


async def _run_image_pyramid(
    owner_kind: OwnerKind, owner_id: str, *, force: bool, delegation=None
) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    asset_id = generation_number = lease_token = None
    storage = source_hold = None
    try:
        async with (
            engine.connect() as business,
            AsyncSession(bind=business, expire_on_commit=False) as db,
        ):
            authority = await originals.delegated(db, delegation)
            if (
                authority.owner_kind != owner_kind
                or str(authority.owner_id) != owner_id
            ):
                raise ImagePyramidError("owner_changed")
            owner = await load_owner(db, owner_kind, uuid.UUID(owner_id))
            from app.services import fixture_original_read, original_sources

            if await fixture_original_read.is_fixture(db, owner.source):
                await db.rollback()
                pending_hold = fixture_original_read.admitted_system(
                    db, owner.source, original_sources.read_plan(owner.source).spec
                )
                await pending_hold.__aenter__()
                source_hold = pending_hold
            from app.services import fixture_media_execution

            execution = fixture_media_execution.current()
            if execution is None:
                generation = await prepare_generation(
                    db, owner, force=force, delegation=delegation
                )
                await db.commit()
            else:
                from app.db.models.dataset import DatasetItem

                item = owner.source.item_id
                dataset = (
                    await db.scalar(
                        select(DatasetItem.dataset_id).where(DatasetItem.id == item)
                    )
                    if item
                    else None
                )
                task = owner.id if item is None and owner.kind == "task" else None
                async with execution.birth_scope(db, dataset, item, task):
                    generation = await prepare_generation(
                        db, owner, force=force, delegation=delegation
                    )
                    await db.commit()
            if execution is not None and generation is not None:
                if execution.kind not in {"pyramid", "independent"}:
                    raise ImagePyramidError("fixture_media_scope_mismatch")
                await execution.bind_generation(
                    generation.asset_id, generation.generation
                )
            if generation is None or generation.status == "ready":
                return
            asset_id, generation_number = generation.asset_id, generation.generation
            authority = await originals.delegated(db, generation.auth_ref)
            await originals.check(db, authority, lock=True)
            lease_token = await acquire_generation_lease(
                db, asset_id, generation_number
            )
            await db.commit()
            if lease_token is None:
                return
            generation = await db.get(
                ImagePyramidGeneration,
                (asset_id, generation_number),
                populate_existing=True,
            )
            output_id = generation.output_id
            if execution is not None:
                await execution.record_pyramid_attempt(
                    asset_id, generation_number, lease_token, output_id
                )
            output, revision = await objects.check(
                db,
                owner,
                asset_id,
                generation_number,
                output_id,
                authority=authority,
                lease_token=lease_token,
            )
            await db.commit()
            target = await physical.transport_target(db, output)
            context, entered = None, []
            recorder = media_cache_accounting.Recorder(
                factory,
                "image_pyramid_outputs",
                output.id,
                output.storage_ref["endpoint_url"],
                output.storage_ref["bucket"],
            )
            accounting_settled = True
            async with await storage_access.acquire(
                db,
                uuid.UUID(output.storage_ref["location_id"]),
                objects.purpose(output.id),
                expected_revision=revision,
            ) as permit:
                from app.services.media_native_stage import NativeStage

                native_stage = NativeStage(
                    db,
                    permit,
                    "pyramid_output",
                    output.id,
                    before_cleanup=_clear_vips_cache,
                )

                async def guard():
                    async with factory() as short:
                        await objects.check(
                            short,
                            owner,
                            asset_id,
                            generation_number,
                            output_id,
                            authority=authority,
                            lease_token=lease_token,
                            gate_revision=revision,
                        )

                await guard()
                try:
                    if target is None:
                        try:
                            storage = StorageService(
                                client_config=Config(
                                    retries={
                                        "total_max_attempts": 1,
                                        "mode": "standard",
                                    }
                                )
                            )
                        except BaseException as exc:
                            raise storage_access.mark_unsettled(exc)
                    else:
                        context = physical.open_storage(
                            target=target, single_attempt=True
                        )

                        def open_owned():
                            value = context.__enter__()
                            entered.append(True)
                            return value

                        storage = await originals.physical(open_owned)
                    async with originals.media(
                        db, authority, owner.source, guard=guard
                    ) as stage:
                        result = await originals.physical(
                            _build_generation,
                            storage=storage,
                            owner=owner,
                            asset_id=asset_id,
                            generation_number=generation_number,
                            expected_source_size=owner.source.size_bytes,
                            stage=stage,
                            output=output,
                            accounting=recorder,
                            native_stage=native_stage,
                            timeout=settings.image_pyramid_job_timeout_seconds,
                        )
                except BaseException as exc:
                    accounting_settled = not storage_access.failure_unsettled(exc)
                    raise
                finally:
                    primary = sys.exception()
                    closed, cleanup_failure = False, None
                    try:
                        if entered:
                            await originals.physical(context.__exit__, *sys.exc_info())
                            storage = None
                            closed = True
                        elif storage is not None:
                            await originals.physical(
                                storage_access.close_registered, storage.client
                            )
                            storage = None
                            closed = True
                    except BaseException as exc:
                        cleanup_failure = exc
                    try:
                        await recorder.finish(
                            settled=closed
                            and accounting_settled
                            and cleanup_failure is None
                        )
                    except BaseException as exc:
                        cleanup_failure = (
                            exc
                            if cleanup_failure is None
                            else BaseExceptionGroup(
                                "Image pyramid close and accounting failed",
                                [cleanup_failure, exc],
                            )
                        )
                    if cleanup_failure is not None:
                        if primary is not None:
                            originals._raise_drain_failure(
                                primary,
                                cleanup_failure,
                                "Image pyramid generation and cleanup failed",
                            )
                        raise cleanup_failure
                await guard()
                async with factory() as final:
                    published = await publish_generation(
                        final,
                        asset_id=asset_id,
                        generation_number=generation_number,
                        lease_token=lease_token,
                        output_id=output.id,
                        inventory=result.inventory,
                        gate_revision=revision,
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
                    )
                    if not published:
                        raise ImagePyramidError("lease_lost")
                    await final.commit()
        IMAGE_PYRAMID_SOURCE_BYTES.observe(owner.source.size_bytes)
        IMAGE_PYRAMID_DECODED_PIXELS.observe(result.width * result.height)
        IMAGE_PYRAMID_TILE_COUNT.observe(result.tile_count)
        IMAGE_PYRAMID_DERIVED_BYTES.observe(result.retained_bytes)
        IMAGE_PYRAMID_TEMP_BYTES.observe(result.temp_bytes)
        IMAGE_PYRAMID_GENERATIONS_TOTAL.labels(outcome="ready", error_code="none").inc()
    except BaseException as exc:
        if storage_access.failure_unsettled(exc):
            raise
        code = _stable_error_code(exc)
        if (
            asset_id is not None
            and generation_number is not None
            and lease_token is not None
        ):

            async def failed():
                async with factory() as final:
                    await fail_generation(
                        final,
                        asset_id=asset_id,
                        generation_number=generation_number,
                        lease_token=lease_token,
                        error_code=code,
                    )
                    await final.commit()

            await storage_access.finish(asyncio.create_task(failed()))
        IMAGE_PYRAMID_GENERATIONS_TOTAL.labels(outcome="failed", error_code=code).inc()
        logger.warning("image pyramid generation refused: code=%s", code)
        if not isinstance(exc, Exception):
            raise
    finally:
        failure = sys.exception()
        cleanup_failure = None
        try:
            if source_hold is not None:
                await source_hold.__aexit__(*sys.exc_info())
        except BaseException as close_error:
            cleanup_failure = storage_access.mark_unsettled(close_error)
        try:
            await storage_access.finish(asyncio.create_task(engine.dispose()))
        except BaseException as disposal_error:
            storage_access.mark_unsettled(disposal_error)
            cleanup_failure = (
                disposal_error
                if cleanup_failure is None
                else BaseExceptionGroup(
                    "Image pyramid admission close and database shutdown failed",
                    [cleanup_failure, disposal_error],
                )
            )
            storage_access.mark_unsettled(cleanup_failure)
        if cleanup_failure is not None:
            if failure is not None:
                originals._raise_drain_failure(
                    failure, cleanup_failure, "Image pyramid and cleanup failed"
                )
            raise cleanup_failure


async def enqueue_image_pyramid(
    db,
    owner_kind: OwnerKind,
    owner_id: uuid.UUID | str,
    *,
    force: bool = False,
    delegation: dict,
) -> str:
    from app.services import fixture_media_execution, media_dispatch

    if not isinstance(delegation, dict) or not isinstance(
        delegation.get("original"), dict
    ):
        raise ImagePyramidError("pyramid_authority_unrecorded")
    identifier = uuid.UUID(str(owner_id))
    args = [owner_kind, str(identifier), force, delegation]
    execution = fixture_media_execution.current()
    if execution is not None and execution.kind == "independent":
        from app.db.models.dataset import DatasetItem

        delivery = uuid.uuid4()
        async with (
            storage_access.engine(db).connect() as business,
            AsyncSession(bind=business, expire_on_commit=False) as producer,
        ):
            source = await originals.pin(producer, owner_kind, identifier)
            execution.require_source(originals.receipt(source))
            item = source.item_id
            dataset = (
                await producer.scalar(
                    select(DatasetItem.dataset_id).where(DatasetItem.id == item)
                )
                if item
                else None
            )
            task = identifier if item is None and owner_kind == "task" else None
            async with execution.birth_scope(
                producer, dataset, item, task, delivery=delivery
            ):
                prepared = await media_dispatch.prepare(
                    producer,
                    owner_kind=owner_kind,
                    owner_id=identifier,
                    producer_kind="pyramid",
                    source=delegation["original"],
                    task_name="app.workers.image_pyramid.generate_image_pyramid",
                    args=args,
                    delivery_id=delivery,
                )
                if prepared.ownership != "fixture":
                    raise ImagePyramidError("fixture_media_scope_mismatch")
                await producer.commit()
        await execution.bind_independent_child(prepared.delivery_id)
    else:
        prepared = await media_dispatch.prepare(
            db,
            owner_kind=owner_kind,
            owner_id=identifier,
            producer_kind="pyramid",
            source=delegation["original"],
            task_name="app.workers.image_pyramid.generate_image_pyramid",
            args=args,
        )
        await db.commit()
    return await media_dispatch.publish(
        prepared, lambda: generate_image_pyramid.delay(*args)
    )


async def _dispatch_image_pyramid(
    owner_kind, owner_id, force, delegation, delivery_id, queue, *, soft_time_limit=None
):
    from app.services import fixture_media_execution

    if not await fixture_media_execution.is_fixture("pyramid", delivery_id):
        if (
            isinstance(queue, str)
            and queue.startswith("aap_workspace_worker_")
            and queue.endswith("_pyramid")
        ):
            async with fixture_media_execution.admitted_independent(
                delivery_id,
                "app.workers.image_pyramid.generate_image_pyramid",
                queue,
                [owner_kind, owner_id, force, delegation],
                {},
                soft_time_limit=soft_time_limit,
            ) as started:
                if started:
                    await _run_image_pyramid(
                        owner_kind, owner_id, force=force, delegation=delegation
                    )
            return
        if await fixture_media_execution.is_fixture("independent", delivery_id):
            raise ImagePyramidError("fixture_media_scope_untracked")
        await _run_image_pyramid(
            owner_kind, owner_id, force=force, delegation=delegation
        )
        return
    if owner_kind != "dataset_item" or force is not False:
        raise ImagePyramidError("fixture_media_scope_mismatch")
    async with fixture_media_execution.admitted(
        "pyramid",
        uuid.UUID(delivery_id),
        queue,
        item_id=uuid.UUID(owner_id),
        delegation=delegation,
    ) as started:
        if started:
            await _run_image_pyramid(
                owner_kind, owner_id, force=force, delegation=delegation
            )


@celery_app.task(
    bind=True,
    name="app.workers.image_pyramid.generate_image_pyramid",
    queue="image-pyramid",
    max_retries=0,
    soft_time_limit=settings.image_pyramid_job_timeout_seconds,
    time_limit=settings.image_pyramid_job_timeout_seconds + 60,
)
def generate_image_pyramid(
    self, owner_kind: OwnerKind, owner_id: str, force: bool = False, delegation=None
) -> None:
    from app.services.fixture_media_execution import request_soft_limit

    asyncio.run(
        _dispatch_image_pyramid(
            owner_kind,
            owner_id,
            force,
            delegation,
            self.request.id,
            self.request.delivery_info.get("routing_key"),
            soft_time_limit=request_soft_limit(self.request),
        )
    )


async def _reconcile_image_pyramids() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            deleted = await objects.reconcile(db, None)
            IMAGE_PYRAMID_GC_TOTAL.labels(outcome="deleted").inc(deleted)
            retained = await db.scalar(
                select(
                    func.coalesce(func.sum(ImagePyramidGeneration.retained_bytes), 0)
                ).where(ImagePyramidGeneration.status == "ready")
            )
            IMAGE_PYRAMID_ASSET_BYTES.set(float(retained or 0))
    finally:
        await storage_access.finish(asyncio.create_task(engine.dispose()))


@celery_app.task(
    bind=True,
    name="app.workers.image_pyramid.reconcile_image_pyramids",
    queue="cleanup",
    max_retries=0,
)
def reconcile_image_pyramids(self) -> None:
    asyncio.run(_reconcile_image_pyramids())
