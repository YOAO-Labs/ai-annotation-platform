from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.image_pyramid import (
    ImagePyramidAsset,
    ImagePyramidGeneration,
)
from app.db.models.task import Task
from app.services.storage import StorageService, storage_service
from app.services import image_pyramid_originals as originals
from app.services import media_item_reads
from app.services.content_snapshot import ContentError
from app.schemas.image_pyramid import ImagePyramidSummary

PYRAMID_SCHEMA = "aap-image-pyramid/v1"
PYRAMID_TILE_SIZE = 512
PYRAMID_OVERLAP = 1
PYRAMID_FORMAT = "webp"
PYRAMID_PREFIX = "image-pyramids"

OwnerKind = Literal["dataset_item", "task"]


class ImagePyramidError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ImagePyramidOwner:
    kind: OwnerKind
    id: uuid.UUID
    file_path: str
    bucket: str
    width: int | None
    height: int | None
    file_type: str
    file_size: int | None
    source: object = None


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


async def owner_for_task(db: AsyncSession, task: Task) -> ImagePyramidOwner:
    try:
        source = await originals.pin(db, "task", task.id)
    except ContentError as exc:
        if exc.code == "directory_cache_not_supported":
            raise ImagePyramidError(exc.code) from None
        raise
    if source.item_id is not None:
        item = await media_item_reads.read_item(db, source.item_id)
        if item is None:
            raise ImagePyramidError("source_missing")
        return _owner("dataset_item", item.id, source, item.width, item.height)
    return _owner("task", task.id, source, None, None)


def _owner(kind, identifier, source, width, height):
    return ImagePyramidOwner(
        kind=kind,
        id=identifier,
        file_path=source.object_key,
        bucket=source.bucket,
        width=width,
        height=height,
        file_type=source.media_type,
        file_size=source.size_bytes,
        source=source,
    )


async def owner_for_dataset_item(db: AsyncSession, item: Any) -> ImagePyramidOwner:
    try:
        source = await originals.pin(db, "dataset_item", item.id)
    except ContentError as exc:
        if exc.code == "directory_cache_not_supported":
            raise ImagePyramidError(exc.code) from None
        raise
    return _owner("dataset_item", item.id, source, item.width, item.height)


async def load_owner(
    db: AsyncSession, owner_kind: OwnerKind, owner_id: uuid.UUID
) -> ImagePyramidOwner:
    if owner_kind == "dataset_item":
        item = await media_item_reads.read_item(db, owner_id)
        if item is None:
            raise ImagePyramidError("source_missing")
        return await owner_for_dataset_item(db, item)
    if owner_kind != "task":
        raise ImagePyramidError("owner_changed")
    from app.services.labeling_core_read_relations import get_core_row, task_row

    task = await get_core_row(db, task_row, owner_id)
    if task is None:
        raise ImagePyramidError("source_missing")
    return await owner_for_task(db, task)


def source_identity(
    owner: ImagePyramidOwner, storage: StorageService = storage_service
) -> tuple[str, int]:
    """The registered identity is metadata; no provider I/O in business locks."""
    ref = originals.receipt(owner.source)
    return "managed:" + hashlib.sha256(canonical_manifest_bytes(ref)).hexdigest(), ref[
        "size_bytes"
    ]


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


async def get_asset_for_owner(
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
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    return (await db.execute(statement)).scalar_one_or_none()


async def prepare_generation(
    db: AsyncSession,
    owner: ImagePyramidOwner,
    *,
    force: bool,
    delegation: dict,
) -> ImagePyramidGeneration | None:
    from app.services.directory_policy import owns_media

    if owner.kind == "dataset_item" and await owns_media(db, owner.id):
        raise ImagePyramidError("directory_cache_not_supported")
    if owner.file_type != "image":
        raise ImagePyramidError("not_image")
    if not force and not pyramid_eligible(owner.width, owner.height):
        return None

    authority = await originals.delegated(db, delegation)
    await originals.check(db, authority, lock=True)
    if originals.receipt(owner.source) != authority.original:
        raise ImagePyramidError("source_changed")
    identity, source_size = source_identity(owner)
    if source_size > settings.image_pyramid_max_source_bytes:
        raise ImagePyramidError("resource_limit")

    await db.execute(
        pg_insert(ImagePyramidAsset)
        .values(id=uuid.uuid4(), **_asset_owner_values(owner))
        .on_conflict_do_nothing()
    )
    asset = await get_asset_for_owner(db, owner, for_update=True)
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
            .execution_options(populate_existing=True)
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
        original_ref=originals.receipt(owner.source),
        auth_ref=delegation,
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
    asset = await db.scalar(
        select(ImagePyramidAsset)
        .where(ImagePyramidAsset.id == asset_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if asset is None:
        return None
    generation = (
        await db.execute(
            select(ImagePyramidGeneration)
            .where(
                ImagePyramidGeneration.asset_id == asset_id,
                ImagePyramidGeneration.generation == generation_number,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
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
    from app.services import image_pyramid_objects as objects

    await objects.allocate(db, asset, generation, token)
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
    output_id: uuid.UUID,
    inventory: dict,
    gate_revision: int,
) -> bool:
    asset = (
        await db.execute(
            select(ImagePyramidAsset)
            .where(ImagePyramidAsset.id == asset_id)
            .with_for_update()
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        asset is None
        or generation is None
        or generation.status != "building"
        or generation.lease_token != lease_token
        or generation.lease_expires_at is None
        or generation.lease_expires_at <= utcnow()
        or generation.output_id != output_id
        or asset.building_generation != generation_number
    ):
        return False
    from app.services import image_pyramid_objects as objects

    authority = await originals.delegated(db, generation.auth_ref)
    await originals.check(db, authority, lock=True)
    owner = await load_owner(
        db,
        "dataset_item" if asset.dataset_item_id else "task",
        asset.dataset_item_id or asset.task_id,
    )
    if (
        generation.original_ref != originals.receipt(owner.source)
        or generation.original_ref != authority.original
        or source_fingerprint != "sha256:" + owner.source.sha256
    ):
        raise ImagePyramidError("source_changed")
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
    await objects.publish(
        db, asset, generation, output_id, lease_token, inventory, gate_revision
    )
    asset.active_generation = generation_number
    if asset.building_generation == generation_number:
        asset.building_generation = None
    # The owner command re-reads this receipt under its own locks.
    await db.flush()
    receipt = await db.scalar(
        text(
            "SELECT aap_assets_v1.publish_image_pyramid_thumbnail("
            "CAST(:asset_id AS uuid), CAST(:generation AS integer), "
            "CAST(:output_id AS uuid))"
        ),
        {
            "asset_id": asset_id,
            "generation": generation_number,
            "output_id": output_id,
        },
    )
    if not isinstance(receipt, dict) or receipt.get("applied") is not True:
        raise ImagePyramidError("thumbnail_publication_rejected")
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
            .execution_options(populate_existing=True)
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
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if generation is None:
        return
    if (
        lease_token is None
        and error_code == "lease_expired"
        and (
            generation.status != "building"
            or generation.lease_expires_at is None
            or generation.lease_expires_at > utcnow()
        )
    ):
        return
    if lease_token is not None and generation.lease_token != lease_token:
        return
    if generation.status != "ready":
        generation.status = "failed"
        generation.error_code = error_code[:64]
        generation.lease_token = None
        generation.lease_expires_at = None
    if asset is not None and asset.building_generation == generation_number:
        asset.building_generation = None
    await db.flush()


async def task_pyramid_assets(
    db: AsyncSession, tasks: list[Task]
) -> dict[uuid.UUID, tuple[ImagePyramidAsset, ImagePyramidGeneration | None]]:
    if not tasks:
        return {}
    from app.services.original_sources import resolve_task_relation
    from app.services.content_snapshot import ContentError

    owners = {}
    for task in tasks:
        try:
            relation = await resolve_task_relation(db, task.id)
            owners[task.id] = (
                ("dataset_item", relation.item_id)
                if relation.item_id
                else ("task", task.id)
            )
        except ContentError:
            owners[task.id] = (
                ("dataset_item", task.dataset_item_id)
                if task.dataset_item_id
                else ("task", task.id)
            )
    item_ids = {
        identifier for kind, identifier in owners.values() if kind == "dataset_item"
    }
    direct_task_ids = {
        identifier for kind, identifier in owners.values() if kind == "task"
    }
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

    result: dict[
        uuid.UUID, tuple[ImagePyramidAsset, ImagePyramidGeneration | None]
    ] = {}
    for task in tasks:
        owner_key = owners[task.id]
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
        result[task.id] = (asset, selected)
    return result


async def get_active_generation_for_task(
    db: AsyncSession, task: Task
) -> tuple[ImagePyramidOwner, ImagePyramidAsset | None, ImagePyramidGeneration | None]:
    owner = await owner_for_task(db, task)
    asset = await get_asset_for_owner(db, owner)
    if asset is None:
        return owner, None, None
    generation = None
    if asset.active_generation is not None:
        generation = await db.get(
            ImagePyramidGeneration, (asset.id, asset.active_generation)
        )
    return owner, asset, generation


def manifest_etag(generation: ImagePyramidGeneration) -> str:
    digest = (generation.manifest_digest or "missing").replace("sha256:", "")
    return f'"pyramid-{generation.generation}-{digest}"'


def source_matches_generation(
    owner: ImagePyramidOwner,
    generation: ImagePyramidGeneration,
    storage: StorageService = storage_service,
) -> bool:
    return (
        generation.original_ref is not None
        and generation.original_ref == originals.receipt(owner.source)
        and generation.source_identity == source_identity(owner)[0]
        and generation.source_fingerprint == "sha256:" + owner.source.sha256
    )


def pyramid_summary(
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
        status=(
            "failed"
            if generation.status == "ready"
            and (generation.original_ref is None or generation.output_id is None)
            else generation.status
        ),  # type: ignore[arg-type]
        generation=generation.generation,
        width=logical_width,
        height=logical_height,
        tile_size=generation.tile_size,
        format=generation.format,
        required=pyramid_required(logical_width, logical_height),
    )
