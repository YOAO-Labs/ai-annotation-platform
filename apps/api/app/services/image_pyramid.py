from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.db.models.task import Task
from app.observability.metrics import IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL
from app.services.storage import StorageService, storage_service
from app.services.media_materialization import MediaMaterializationWriter
from app.schemas.image_pyramid import (
    ImagePyramidAssetUrl,
    ImagePyramidAssetUrlsRequest,
    ImagePyramidAssetUrlsResponse,
    ImagePyramidManifest,
    ImagePyramidOverviewUrl,
    ImagePyramidResponse,
    ImagePyramidSummary,
)
from app.services.image_pyramid_contract import (
    ImagePyramidApiPort,
    ImagePyramidDispatchTarget,
    ImagePyramidFailure,
    ImagePyramidManifestResult,
    ImagePyramidOwnerKind,
    ImagePyramidRetryPlan,
    ImagePyramidTaskSource,
)

PYRAMID_SCHEMA = "aap-image-pyramid/v1"
PYRAMID_TILE_SIZE = 512
PYRAMID_OVERLAP = 1
PYRAMID_FORMAT = "webp"
PYRAMID_PREFIX = "image-pyramids"


class ImagePyramidError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImagePyramidOwner:
    kind: ImagePyramidOwnerKind
    id: uuid.UUID
    file_path: str
    bucket: str
    width: int | None
    height: int | None
    file_type: str
    file_size: int | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pixels_for_dimensions(width: int | None, height: int | None) -> int | None:
    if not width or not height or width <= 0 or height <= 0:
        return None
    return width * height


def pyramid_required(width: int | None, height: int | None) -> bool:
    pixels = pixels_for_dimensions(width, height)
    return pixels is not None and pixels >= settings.image_pyramid_required_pixels


def pyramid_eligible(width: int | None, height: int | None) -> bool:
    pixels = pixels_for_dimensions(width, height)
    return pixels is not None and pixels >= settings.image_pyramid_optional_pixels


def validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImagePyramidError("unsupported_dimensions")
    if (
        width > settings.image_pyramid_max_dimension
        or height > settings.image_pyramid_max_dimension
        or width * height > settings.image_pyramid_max_pixels
    ):
        raise ImagePyramidError("unsupported_dimensions")
    if total_tile_count(width, height) > settings.image_pyramid_max_tiles:
        raise ImagePyramidError("resource_limit")


def pyramid_levels(width: int, height: int) -> list[dict[str, int]]:
    """Full-resolution-first powers-of-two grid with explicit ceil rounding."""
    levels: list[dict[str, int]] = []
    level = 0
    while True:
        scale = 1 << level
        level_width = max(1, math.ceil(width / scale))
        level_height = max(1, math.ceil(height / scale))
        levels.append(
            {
                "level": level,
                "scaleFactor": scale,
                "width": level_width,
                "height": level_height,
                "columns": math.ceil(level_width / PYRAMID_TILE_SIZE),
                "rows": math.ceil(level_height / PYRAMID_TILE_SIZE),
            }
        )
        if level_width == 1 and level_height == 1:
            return levels
        level += 1


def total_tile_count(width: int, height: int) -> int:
    return sum(
        level["columns"] * level["rows"] for level in pyramid_levels(width, height)
    )


def expected_tile_dimensions(level: dict[str, int], x: int, y: int) -> tuple[int, int]:
    if not (0 <= x < level["columns"] and 0 <= y < level["rows"]):
        raise ImagePyramidError("invalid_coordinate")
    core_width = min(PYRAMID_TILE_SIZE, level["width"] - x * PYRAMID_TILE_SIZE)
    core_height = min(PYRAMID_TILE_SIZE, level["height"] - y * PYRAMID_TILE_SIZE)
    stored_width = core_width + (x > 0) + (x + 1 < level["columns"])
    stored_height = core_height + (y > 0) + (y + 1 < level["rows"])
    return int(stored_width), int(stored_height)


def generation_prefix(asset_id: uuid.UUID, generation: int) -> str:
    return f"{PYRAMID_PREFIX}/{asset_id}/g{generation}"


def tile_key(asset_id: uuid.UUID, generation: int, level: int, x: int, y: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/tiles/{level}/{x}/{y}.webp"


def overview_key(asset_id: uuid.UUID, generation: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/overview.webp"


def manifest_key(asset_id: uuid.UUID, generation: int) -> str:
    return f"{generation_prefix(asset_id, generation)}/manifest.json"


def build_manifest(
    *,
    generation: int,
    source_fingerprint: str,
    width: int,
    height: int,
    overview_width: int,
    overview_height: int,
    overview_digest: str,
) -> dict[str, Any]:
    return {
        "schema": PYRAMID_SCHEMA,
        "generation": generation,
        "sourceFingerprint": source_fingerprint,
        "normalizationVersion": settings.image_pyramid_normalization_version,
        "width": width,
        "height": height,
        "tileSize": PYRAMID_TILE_SIZE,
        "overlap": PYRAMID_OVERLAP,
        "format": PYRAMID_FORMAT,
        "levels": pyramid_levels(width, height),
        "overview": {
            "width": overview_width,
            "height": overview_height,
            "contentDigest": overview_digest,
        },
    }


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


async def _owner_for_task(
    db: AsyncSession, task: ImagePyramidTaskSource
) -> ImagePyramidOwner:
    if task.dataset_item_id:
        item = await db.get(DatasetItem, task.dataset_item_id)
        if item is None:
            raise ImagePyramidError("source_missing")
        return _owner_for_dataset_item(item)
    return ImagePyramidOwner(
        kind="task",
        id=task.task_id,
        file_path=task.file_path,
        bucket=storage_service.bucket,
        width=None,
        height=None,
        file_type=task.file_type,
        file_size=None,
    )


def _owner_for_dataset_item(item: DatasetItem) -> ImagePyramidOwner:
    return ImagePyramidOwner(
        kind="dataset_item",
        id=item.id,
        file_path=item.file_path,
        bucket=storage_service.datasets_bucket,
        width=item.width,
        height=item.height,
        file_type=item.file_type,
        file_size=item.file_size,
    )


async def load_owner(
    db: AsyncSession, owner_kind: ImagePyramidOwnerKind, owner_id: uuid.UUID
) -> ImagePyramidOwner:
    if owner_kind == "dataset_item":
        item = await db.get(DatasetItem, owner_id)
        if item is None:
            raise ImagePyramidError("source_missing")
        return _owner_for_dataset_item(item)
    task = await db.get(Task, owner_id)
    if task is None:
        raise ImagePyramidError("source_missing")
    if task.dataset_item_id is not None:
        raise ImagePyramidError("owner_changed")
    return ImagePyramidOwner(
        kind="task",
        id=task.id,
        file_path=task.file_path,
        bucket=storage_service.bucket,
        width=None,
        height=None,
        file_type=task.file_type,
        file_size=None,
    )


def source_identity(
    owner: ImagePyramidOwner, storage: StorageService = storage_service
) -> tuple[str, int]:
    try:
        metadata = storage.client.head_object(Bucket=owner.bucket, Key=owner.file_path)
    except Exception as exc:  # noqa: BLE001 - storage adapters surface varied errors
        raise ImagePyramidError("source_missing") from exc
    size = int(metadata.get("ContentLength") or 0)
    if size <= 0:
        raise ImagePyramidError("source_missing")
    if size > settings.image_pyramid_max_source_bytes:
        raise ImagePyramidError("resource_limit")
    etag = str(metadata.get("ETag") or "").strip('"')
    version_id = str(metadata.get("VersionId") or "")
    identity = f"etag:{etag}:bytes:{size}"
    if version_id:
        identity += f":version:{version_id}"
    return identity, size


def _asset_owner_values(owner: ImagePyramidOwner) -> dict[str, Any]:
    return {
        "dataset_item_id": owner.id if owner.kind == "dataset_item" else None,
        "task_id": owner.id if owner.kind == "task" else None,
        "profile_version": settings.image_pyramid_profile_version,
    }


def _asset_owner_clause(owner: ImagePyramidOwner):
    if owner.kind == "dataset_item":
        return ImagePyramidAsset.dataset_item_id == owner.id
    return ImagePyramidAsset.task_id == owner.id


async def _get_asset_for_owner(
    db: AsyncSession,
    owner: ImagePyramidOwner,
    *,
    for_update: bool = False,
) -> ImagePyramidAsset | None:
    statement = select(ImagePyramidAsset).where(
        _asset_owner_clause(owner),
        ImagePyramidAsset.profile_version == settings.image_pyramid_profile_version,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _invalidate_ready_generation(
    db: AsyncSession,
    asset_id: uuid.UUID,
    generation_number: int,
    error_code: str,
) -> None:
    locked_asset = (
        await db.execute(
            select(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == asset_id)
            .with_for_update()
        )
    ).scalar_one()
    locked_generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset_id,
                ImagePyramidGeneration.generation == generation_number,
            )
            .with_for_update()
        )
    ).scalar_one()
    if locked_asset.active_generation == generation_number:
        locked_asset.active_generation = None
    locked_generation.status = "failed"
    locked_generation.error_code = error_code
    await db.commit()


async def prepare_generation(
    db: AsyncSession,
    owner: ImagePyramidOwner,
    *,
    force: bool,
) -> ImagePyramidGeneration | None:
    if owner.file_type != "image":
        raise ImagePyramidError("not_image")
    if not force and not pyramid_eligible(owner.width, owner.height):
        return None

    identity, source_size = source_identity(owner)
    if source_size > settings.image_pyramid_max_source_bytes:
        raise ImagePyramidError("resource_limit")

    await db.execute(
        pg_insert(ImagePyramidAsset)
        .values(id=uuid.uuid4(), **_asset_owner_values(owner))
        .on_conflict_do_nothing()
    )
    asset = await _get_asset_for_owner(db, owner, for_update=True)
    if asset is None:  # pragma: no cover - defensive after INSERT/ON CONFLICT
        raise ImagePyramidError("state_conflict")

    inflight = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset.id,
                ImagePyramidGeneration.status.in_(("pending", "building")),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = utcnow()
    if inflight is not None:
        if (
            inflight.status == "building"
            and inflight.lease_expires_at is not None
            and inflight.lease_expires_at <= now
        ):
            inflight.status = "failed"
            inflight.error_code = "lease_expired"
            inflight.lease_token = None
            inflight.lease_expires_at = None
            if asset.building_generation == inflight.generation:
                asset.building_generation = None
            await db.flush()
        else:
            return inflight

    active: ImagePyramidGeneration | None = None
    if asset.active_generation is not None:
        active = await db.get(
            ImagePyramidGeneration, (asset.id, asset.active_generation)
        )
        if (
            active is None
            or active.status != "ready"
            or active.source_identity != identity
        ):
            asset.active_generation = None
            active = None
    if active is not None and not force:
        return active

    latest = (
        await db.execute(
            select(func.max(ImagePyramidGeneration.generation)).where(
                ImagePyramidGeneration.asset_id == asset.id
            )
        )
    ).scalar_one()
    generation_number = int(latest or 0) + 1
    generation = ImagePyramidGeneration(
        asset_id=asset.id,
        generation=generation_number,
        source_identity=identity,
        status="pending",
        normalization_version=settings.image_pyramid_normalization_version,
        tile_size=PYRAMID_TILE_SIZE,
        overlap=PYRAMID_OVERLAP,
    )
    db.add(generation)
    asset.building_generation = generation_number
    await db.flush()
    return generation


async def acquire_generation_lease(
    db: AsyncSession, asset_id: uuid.UUID, generation_number: int
) -> uuid.UUID | None:
    generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset_id,
                ImagePyramidGeneration.generation == generation_number,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if generation is None or generation.status in {"ready", "failed"}:
        return None
    now = utcnow()
    if (
        generation.status == "building"
        and generation.lease_expires_at is not None
        and generation.lease_expires_at > now
    ):
        return None
    token = uuid.uuid4()
    generation.status = "building"
    generation.error_code = None
    generation.lease_token = token
    generation.lease_expires_at = now + timedelta(
        seconds=settings.image_pyramid_lease_seconds
    )
    generation.attempts += 1
    await db.flush()
    return token


async def publish_generation(
    db: AsyncSession,
    *,
    asset_id: uuid.UUID,
    generation_number: int,
    lease_token: uuid.UUID,
    source_fingerprint: str,
    width: int,
    height: int,
    max_level: int,
    manifest_storage_key: str,
    manifest_digest: str,
    overview_storage_key: str,
    overview_width: int,
    overview_height: int,
    overview_digest: str,
    tile_count: int,
    retained_bytes: int,
    task_thumbnail_writer: MediaMaterializationWriter,
) -> bool:
    asset = (
        await db.execute(
            select(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == asset_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset_id,
                ImagePyramidGeneration.generation == generation_number,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        asset is None
        or generation is None
        or generation.status != "building"
        or generation.lease_token != lease_token
    ):
        return False
    generation.source_fingerprint = source_fingerprint
    generation.status = "ready"
    generation.lease_token = None
    generation.lease_expires_at = None
    generation.width = width
    generation.height = height
    generation.max_level = max_level
    generation.format = PYRAMID_FORMAT
    generation.manifest_key = manifest_storage_key
    generation.manifest_digest = manifest_digest
    generation.overview_key = overview_storage_key
    generation.overview_width = overview_width
    generation.overview_height = overview_height
    generation.overview_digest = overview_digest
    generation.tile_count = tile_count
    generation.retained_bytes = retained_bytes
    generation.error_code = None
    asset.active_generation = generation_number
    if asset.building_generation == generation_number:
        asset.building_generation = None
    if asset.dataset_item_id is not None:
        owner_item = await db.get(DatasetItem, asset.dataset_item_id)
        if owner_item is not None:
            owner_item.thumbnail_path = overview_storage_key
    elif asset.task_id is not None:
        await task_thumbnail_writer.set_task_thumbnail(
            asset.task_id, overview_storage_key, None
        )
    await db.flush()
    return True


async def fail_generation(
    db: AsyncSession,
    *,
    asset_id: uuid.UUID,
    generation_number: int,
    lease_token: uuid.UUID | None,
    error_code: str,
) -> None:
    asset = (
        await db.execute(
            select(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == asset_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset_id,
                ImagePyramidGeneration.generation == generation_number,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if generation is None:
        return
    if lease_token is not None and generation.lease_token not in {None, lease_token}:
        return
    if generation.status != "ready":
        generation.status = "failed"
        generation.error_code = error_code[:64]
        generation.lease_token = None
        generation.lease_expires_at = None
    if asset is not None and asset.building_generation == generation_number:
        asset.building_generation = None
    await db.flush()


async def _task_pyramid_summaries(
    db: AsyncSession,
    tasks: tuple[ImagePyramidTaskSource, ...],
    dimensions: Mapping[uuid.UUID, tuple[int | None, int | None]],
) -> dict[uuid.UUID, ImagePyramidSummary]:
    if not tasks:
        return {}
    item_ids = {task.dataset_item_id for task in tasks if task.dataset_item_id}
    direct_task_ids = {task.task_id for task in tasks if task.dataset_item_id is None}
    owner_filters = []
    if item_ids:
        owner_filters.append(ImagePyramidAsset.dataset_item_id.in_(item_ids))
    if direct_task_ids:
        owner_filters.append(ImagePyramidAsset.task_id.in_(direct_task_ids))
    if not owner_filters:
        return {}
    assets = list(
        (
            await db.execute(
                select(ImagePyramidAsset).where(
                    or_(*owner_filters),
                    ImagePyramidAsset.profile_version
                    == settings.image_pyramid_profile_version,
                )
            )
        )
        .scalars()
        .all()
    )
    if not assets:
        return {}
    asset_ids = [asset.id for asset in assets]
    generations = list(
        (
            await db.execute(
                select(ImagePyramidGeneration)
                .where(ImagePyramidGeneration.asset_id.in_(asset_ids))
                .order_by(
                    ImagePyramidGeneration.asset_id,
                    ImagePyramidGeneration.generation.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    by_asset: dict[uuid.UUID, list[ImagePyramidGeneration]] = {}
    for generation in generations:
        by_asset.setdefault(generation.asset_id, []).append(generation)
    owner_assets: dict[tuple[str, uuid.UUID], ImagePyramidAsset] = {}
    for asset in assets:
        if asset.dataset_item_id:
            owner_assets[("dataset_item", asset.dataset_item_id)] = asset
        elif asset.task_id:
            owner_assets[("task", asset.task_id)] = asset

    result: dict[uuid.UUID, ImagePyramidSummary] = {}
    for task in tasks:
        owner_key = (
            ("dataset_item", task.dataset_item_id)
            if task.dataset_item_id
            else ("task", task.task_id)
        )
        asset = owner_assets.get(owner_key)
        if asset is None:
            continue
        candidates = by_asset.get(asset.id, [])
        selected = next(
            (
                row
                for row in candidates
                if asset.active_generation is not None
                and row.generation == asset.active_generation
                and row.status == "ready"
            ),
            None,
        )
        if selected is None and asset.building_generation is not None:
            selected = next(
                (
                    row
                    for row in candidates
                    if row.generation == asset.building_generation
                ),
                None,
            )
        if selected is None and candidates:
            selected = candidates[0]
        width, height = dimensions.get(task.task_id, (None, None))
        summary = _pyramid_summary(selected, width=width, height=height)
        if summary is not None:
            result[task.task_id] = summary
    return result


async def _active_generation_for_task(
    db: AsyncSession, task: ImagePyramidTaskSource
) -> tuple[ImagePyramidOwner, ImagePyramidAsset | None, ImagePyramidGeneration | None]:
    owner = await _owner_for_task(db, task)
    asset = await _get_asset_for_owner(db, owner)
    if asset is None:
        return owner, None, None
    generation = (
        await db.get(ImagePyramidGeneration, (asset.id, asset.active_generation))
        if asset.active_generation is not None
        else None
    )
    return owner, asset, generation


async def _touch_generation_access(
    db: AsyncSession, generation: ImagePyramidGeneration
) -> None:
    """Commit a rate-limited access timestamp for a served ready generation."""
    if (
        generation.last_accessed_at is None
        or generation.last_accessed_at < utcnow() - timedelta(minutes=10)
    ):
        generation.last_accessed_at = utcnow()
        await db.commit()


def _generation_manifest(
    generation: ImagePyramidGeneration,
    storage: StorageService = storage_service,
) -> dict[str, Any]:
    if not generation.manifest_key or not generation.manifest_digest:
        raise ImagePyramidError("inconsistent_ready")
    try:
        response = storage.client.get_object(
            Bucket=storage.media_cache_bucket,
            Key=generation.manifest_key,
        )
        body = response["Body"].read(1024 * 1024 + 1)
    except Exception as exc:  # noqa: BLE001
        raise ImagePyramidError("object_missing") from exc
    if len(body) > 1024 * 1024:
        raise ImagePyramidError("inconsistent_ready")
    if sha256_digest(body) != generation.manifest_digest:
        raise ImagePyramidError("inconsistent_ready")
    try:
        manifest = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ImagePyramidError("inconsistent_ready") from exc
    if (
        manifest.get("schema") != PYRAMID_SCHEMA
        or manifest.get("generation") != generation.generation
    ):
        raise ImagePyramidError("inconsistent_ready")
    return manifest


def _manifest_etag(generation: ImagePyramidGeneration) -> str:
    digest = (generation.manifest_digest or "missing").replace("sha256:", "")
    return f'"pyramid-{generation.generation}-{digest}"'


def _source_matches_generation(
    owner: ImagePyramidOwner,
    generation: ImagePyramidGeneration,
    storage: StorageService = storage_service,
) -> bool:
    try:
        identity, _ = source_identity(owner, storage)
    except ImagePyramidError:
        return False
    return identity == generation.source_identity


def _pyramid_summary(
    generation: ImagePyramidGeneration | None,
    *,
    width: int | None,
    height: int | None,
) -> ImagePyramidSummary | None:
    if generation is None:
        return None
    logical_width = generation.width or width
    logical_height = generation.height or height
    return ImagePyramidSummary(
        status=generation.status,  # type: ignore[arg-type]
        generation=generation.generation,
        width=logical_width,
        height=logical_height,
        tile_size=generation.tile_size,
        format=generation.format,
        required=pyramid_required(logical_width, logical_height),
    )


async def _latest_generation(
    db: AsyncSession, asset: ImagePyramidAsset
) -> ImagePyramidGeneration | None:
    return (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(ImagePyramidGeneration.asset_id == asset.id)
            .order_by(ImagePyramidGeneration.generation.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _building_generation(
    db: AsyncSession, asset: ImagePyramidAsset
) -> ImagePyramidGeneration | None:
    if asset.building_generation is None:
        return None
    return await db.get(ImagePyramidGeneration, (asset.id, asset.building_generation))


def _status_response(
    *,
    task_id: uuid.UUID,
    status_value: str,
    required: bool,
    generation: ImagePyramidGeneration | None = None,
    retryable: bool = False,
    retry_after_ms: int | None = None,
    error_code: str | None = None,
) -> ImagePyramidResponse:
    return ImagePyramidResponse(
        task_id=task_id,
        status=status_value,  # type: ignore[arg-type]
        required=required,
        retryable=retryable,
        retry_after_ms=retry_after_ms,
        generation=generation.generation if generation else None,
        error_code=error_code,
    )


class SqlAlchemyImagePyramidApi(ImagePyramidApiPort):
    """Bind task-scoped image-pyramid API behavior to one SQL transaction."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def task_summaries(
        self,
        tasks: tuple[ImagePyramidTaskSource, ...],
        dimensions: Mapping[uuid.UUID, tuple[int | None, int | None]],
    ) -> dict[uuid.UUID, ImagePyramidSummary]:
        return await _task_pyramid_summaries(self._db, tasks, dimensions)

    async def manifest(
        self, task: ImagePyramidTaskSource, *, if_none_match: str | None
    ) -> ImagePyramidManifestResult:
        owner, asset, generation = await _active_generation_for_task(self._db, task)
        required = pyramid_required(owner.width, owner.height)
        cache_control = "private, no-store"
        if owner.file_type != "image":
            return ImagePyramidManifestResult(
                body=_status_response(
                    task_id=task.task_id,
                    status_value="not_available",
                    required=False,
                ),
                etag=None,
                cache_control=cache_control,
                outcome="not_available",
            )

        if asset is None:
            outcome = (
                "missing"
                if pyramid_eligible(owner.width, owner.height)
                else "not_available"
            )
            return ImagePyramidManifestResult(
                body=_status_response(
                    task_id=task.task_id,
                    status_value=outcome,
                    required=required,
                    retryable=outcome == "missing",
                ),
                etag=None,
                cache_control=cache_control,
                outcome=outcome,
            )

        if generation is None or generation.status != "ready":
            latest = await _latest_generation(self._db, asset)
            if latest is None:
                status_value = "missing"
                retryable = True
                retry_after_ms = None
                error_code = None
            elif latest.status in {"pending", "building"}:
                status_value = latest.status
                retryable = False
                retry_after_ms = 2_000
                error_code = None
            else:
                status_value = "failed"
                retryable = True
                elapsed = max(0.0, (utcnow() - latest.updated_at).total_seconds())
                retry_after_ms = max(
                    0,
                    int(
                        (settings.image_pyramid_retry_cooldown_seconds - elapsed) * 1000
                    ),
                )
                error_code = latest.error_code
            return ImagePyramidManifestResult(
                body=_status_response(
                    task_id=task.task_id,
                    status_value=status_value,
                    required=required,
                    generation=latest,
                    retryable=retryable,
                    retry_after_ms=retry_after_ms,
                    error_code=error_code,
                ),
                etag=None,
                cache_control=cache_control,
                outcome=status_value,
            )

        if not _source_matches_generation(owner, generation):
            await _invalidate_ready_generation(
                self._db, asset.id, generation.generation, "source_changed"
            )
            dispatch = (
                ImagePyramidDispatchTarget(owner.kind, owner.id, True)
                if settings.image_pyramid_auto_generate
                else None
            )
            return ImagePyramidManifestResult(
                body=_status_response(
                    task_id=task.task_id,
                    status_value="stale",
                    required=required,
                    generation=generation,
                    retryable=True,
                    error_code="source_changed",
                ),
                etag=None,
                cache_control=cache_control,
                outcome="stale",
                dispatch=dispatch,
            )

        try:
            manifest = _generation_manifest(generation)
            if not generation.overview_key or not storage_service.verify_upload(
                generation.overview_key,
                bucket=storage_service.media_cache_bucket,
            ):
                raise ImagePyramidError("object_missing")
        except ImagePyramidError as exc:
            await _invalidate_ready_generation(
                self._db, asset.id, generation.generation, exc.code
            )
            dispatch = (
                ImagePyramidDispatchTarget(owner.kind, owner.id, True)
                if settings.image_pyramid_auto_generate
                else None
            )
            return ImagePyramidManifestResult(
                body=_status_response(
                    task_id=task.task_id,
                    status_value="inconsistent",
                    required=required,
                    generation=generation,
                    retryable=True,
                    error_code=exc.code,
                ),
                etag=None,
                cache_control=cache_control,
                outcome="inconsistent",
                dispatch=dispatch,
            )

        etag = _manifest_etag(generation)
        cache_control = "private, max-age=60"
        if if_none_match == etag:
            return ImagePyramidManifestResult(
                body=None,
                etag=etag,
                cache_control=cache_control,
                outcome="not_modified",
            )

        expiry = utcnow() + timedelta(seconds=settings.image_pyramid_url_expiry_seconds)
        overview_url = storage_service.generate_download_url(
            generation.overview_key,
            expires_in=settings.image_pyramid_url_expiry_seconds,
            bucket=storage_service.media_cache_bucket,
            align=False,
        )
        building = await _building_generation(self._db, asset)
        await _touch_generation_access(self._db, generation)
        return ImagePyramidManifestResult(
            body=ImagePyramidResponse(
                task_id=task.task_id,
                status="ready",
                required=pyramid_required(generation.width, generation.height),
                generation=generation.generation,
                building_generation=building.generation if building else None,
                building_status=(
                    building.status
                    if building is not None
                    and building.status in {"pending", "building"}
                    else None
                ),
                manifest=ImagePyramidManifest.model_validate(manifest),
                overview=ImagePyramidOverviewUrl(url=overview_url, expires_at=expiry),
            ),
            etag=etag,
            cache_control=cache_control,
            outcome="ready",
        )

    async def asset_urls(
        self, task: ImagePyramidTaskSource, payload: ImagePyramidAssetUrlsRequest
    ) -> ImagePyramidAssetUrlsResponse:
        owner, asset, generation = await _active_generation_for_task(self._db, task)
        if asset is None or generation is None or generation.status != "ready":
            raise ImagePyramidFailure(
                409,
                {"reason": "asset_not_ready"},
                metric_outcome="not_ready",
            )
        if not _source_matches_generation(owner, generation):
            await _invalidate_ready_generation(
                self._db, asset.id, generation.generation, "source_changed"
            )
            raise ImagePyramidFailure(
                409,
                {"reason": "stale_generation", "active_generation": None},
            )

        try:
            manifest = _generation_manifest(generation)
        except ImagePyramidError as exc:
            await _invalidate_ready_generation(
                self._db, asset.id, generation.generation, exc.code
            )
            raise ImagePyramidFailure(409, {"reason": "inconsistent_ready"}) from exc
        levels = {int(level["level"]): level for level in manifest["levels"]}
        unique: list[Any] = []
        seen: set[tuple[Any, ...]] = set()
        for item in payload.items:
            if item.kind == "overview":
                identity = ("overview", item.generation)
            else:
                identity = ("tile", item.generation, item.level, item.x, item.y)
            if identity not in seen:
                seen.add(identity)
                unique.append(item)

        expiry = utcnow() + timedelta(seconds=settings.image_pyramid_url_expiry_seconds)
        output: list[ImagePyramidAssetUrl] = []
        for item in unique:
            if item.generation != generation.generation:
                raise ImagePyramidFailure(
                    409,
                    {
                        "reason": "stale_generation",
                        "active_generation": generation.generation,
                    },
                )
            if item.kind == "overview":
                key = generation.overview_key
                if not key:
                    raise ImagePyramidFailure(409, {"reason": "inconsistent_ready"})
                output_item = {
                    "kind": "overview",
                    "generation": generation.generation,
                }
            else:
                level = levels.get(item.level)
                if (
                    level is None
                    or item.x >= int(level["columns"])
                    or item.y >= int(level["rows"])
                ):
                    raise ImagePyramidFailure(422, {"reason": "invalid_coordinate"})
                key = tile_key(
                    asset.id,
                    generation.generation,
                    item.level,
                    item.x,
                    item.y,
                )
                output_item = {
                    "kind": "tile",
                    "generation": generation.generation,
                    "level": item.level,
                    "x": item.x,
                    "y": item.y,
                }
            if not storage_service.verify_upload(
                key, bucket=storage_service.media_cache_bucket
            ):
                await _invalidate_ready_generation(
                    self._db, asset.id, generation.generation, "object_missing"
                )
                raise ImagePyramidFailure(
                    409,
                    {"reason": "inconsistent_ready"},
                    metric_outcome="object_missing",
                )
            url = storage_service.generate_download_url(
                key,
                expires_in=settings.image_pyramid_url_expiry_seconds,
                bucket=storage_service.media_cache_bucket,
                align=False,
            )
            output.append(ImagePyramidAssetUrl(**output_item, url=url))
            IMAGE_PYRAMID_ASSET_URL_ITEMS_TOTAL.labels(kind=item.kind).inc()

        return ImagePyramidAssetUrlsResponse(
            task_id=task.task_id,
            generation=generation.generation,
            expires_at=expiry,
            items=output,
        )

    async def retry(self, task: ImagePyramidTaskSource) -> ImagePyramidRetryPlan:
        owner, asset, generation = await _active_generation_for_task(self._db, task)
        if owner.file_type != "image":
            raise ImagePyramidFailure(409, {"reason": "not_image"})
        if (
            owner.width is not None
            and owner.height is not None
            and not pyramid_eligible(owner.width, owner.height)
        ):
            raise ImagePyramidFailure(409, {"reason": "not_eligible"})

        invalidated_stale = False
        if generation is not None and generation.status == "ready":
            if _source_matches_generation(owner, generation):
                raise ImagePyramidFailure(409, {"reason": "already_ready"})
            if asset is not None:
                await _invalidate_ready_generation(
                    self._db, asset.id, generation.generation, "source_changed"
                )
                invalidated_stale = True

        if asset is not None:
            latest = await _latest_generation(self._db, asset)
            if latest is not None and latest.status in {"pending", "building"}:
                return ImagePyramidRetryPlan(status=latest.status)
            if (
                latest is not None
                and latest.status == "failed"
                and not invalidated_stale
            ):
                next_retry = latest.updated_at + timedelta(
                    seconds=settings.image_pyramid_retry_cooldown_seconds
                )
                if next_retry > utcnow():
                    retry_after = max(1, int((next_retry - utcnow()).total_seconds()))
                    raise ImagePyramidFailure(
                        429,
                        {
                            "reason": "retry_cooldown",
                            "retry_after_seconds": retry_after,
                        },
                        headers={"Retry-After": str(retry_after)},
                    )

        return ImagePyramidRetryPlan(
            status="queued", owner_kind=owner.kind, owner_id=owner.id
        )
