from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import image_pyramid_data as pyramid_data
from app.services.image_pyramid_data import AssetFacts, GenerationFacts
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


async def get_asset_for_owner(
    db: AsyncSession, owner: ImagePyramidOwner
) -> AssetFacts | None:
    return await pyramid_data.read_asset(
        db, owner.kind, owner.id, settings.image_pyramid_profile_version
    )


async def prepare_generation(
    db: AsyncSession,
    owner: ImagePyramidOwner,
    *,
    force: bool,
    delegation: dict,
) -> GenerationFacts | None:
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

    from app.services import fixture_media_execution

    execution = fixture_media_execution.current()
    generation, reused_inflight = await pyramid_data.prepare(
        db,
        asset=uuid.uuid4(),
        kind=owner.kind,
        owner=owner.id,
        profile=settings.image_pyramid_profile_version,
        identity=identity,
        original=originals.receipt(owner.source),
        authority=delegation,
        normalization=settings.image_pyramid_normalization_version,
        force=force,
        keep_inflight=execution is not None,
    )
    if reused_inflight and execution is not None:
        if execution.kind not in {"pyramid", "independent"}:
            raise ImagePyramidError("fixture_media_scope_mismatch")
        await execution.bind_generation(generation.asset_id, generation.generation)
        return None
    return generation


async def acquire_generation_lease(
    db: AsyncSession, asset_id: uuid.UUID, generation_number: int
) -> uuid.UUID | None:
    if not await pyramid_data.lock_claim(db, asset_id, generation_number):
        return None
    from app.services import image_pyramid_storage as physical, storage_access

    identifier, token = uuid.uuid4(), uuid.uuid4()
    reference = await physical.allocate(db, identifier)
    await storage_access.check(db, uuid.UUID(reference["location_id"]), lock=True)
    if not await pyramid_data.claim(
        db,
        asset_id,
        generation_number,
        identifier,
        token,
        settings.image_pyramid_lease_seconds,
        reference,
    ):
        return None
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
    selected = await pyramid_data.lock_publication(
        db, asset_id, generation_number, lease_token, output_id
    )
    if selected is None:
        return False
    asset, generation = selected
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
    facts = {
        "source_fingerprint": source_fingerprint,
        "width": width,
        "height": height,
        "max_level": max_level,
        "manifest_key": manifest_storage_key,
        "manifest_digest": manifest_digest,
        "overview_key": overview_storage_key,
        "overview_width": overview_width,
        "overview_height": overview_height,
        "overview_digest": overview_digest,
        "tile_count": tile_count,
        "retained_bytes": retained_bytes,
    }
    return await objects.publish(
        db,
        asset,
        replace(generation, format=PYRAMID_FORMAT, **facts),
        output_id,
        lease_token,
        facts,
        inventory,
        gate_revision,
    )


async def fail_generation(
    db: AsyncSession,
    *,
    asset_id: uuid.UUID,
    generation_number: int,
    lease_token: uuid.UUID | None,
    error_code: str,
) -> None:
    await pyramid_data.fail(db, asset_id, generation_number, lease_token, error_code)


async def task_pyramid_assets(
    db: AsyncSession, tasks: list[Task]
) -> dict[uuid.UUID, tuple[AssetFacts, GenerationFacts | None]]:
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
    task_ids = {identifier for kind, identifier in owners.values() if kind == "task"}
    profile = settings.image_pyramid_profile_version
    selected = {}
    for item_batch, task_batch in _owner_batches(item_ids, task_ids):
        pairs = await pyramid_data.summaries(db, item_batch, task_batch, profile)
        for asset, generation in pairs:
            key = (
                ("dataset_item", asset.dataset_item_id)
                if asset.dataset_item_id
                else ("task", asset.task_id)
            )
            selected[key] = (asset, generation)
    return {
        task_id: selected[owner]
        for task_id, owner in owners.items()
        if owner in selected
    }


def _owner_batches(item_ids, task_ids):
    from itertools import islice

    items, tasks = iter(item_ids), iter(task_ids)
    while (item_batch := list(islice(items, 5000))) or (
        task_batch := list(islice(tasks, 5000))
    ):
        if item_batch:
            task_batch = list(islice(tasks, 5000))
        yield item_batch, task_batch


async def get_active_generation_for_task(
    db: AsyncSession, task: Task
) -> tuple[ImagePyramidOwner, AssetFacts | None, GenerationFacts | None]:
    owner = await owner_for_task(db, task)
    asset = await get_asset_for_owner(db, owner)
    if asset is None:
        return owner, None, None
    generation = None
    if asset.active_generation is not None:
        generation = await pyramid_data.read_generation(
            db, asset.id, asset.active_generation
        )
    return owner, asset, generation


def manifest_etag(generation: GenerationFacts) -> str:
    digest = (generation.manifest_digest or "missing").replace("sha256:", "")
    return f'"pyramid-{generation.generation}-{digest}"'


def source_matches_generation(
    owner: ImagePyramidOwner,
    generation: GenerationFacts,
    storage: StorageService = storage_service,
) -> bool:
    return (
        generation.original_ref is not None
        and generation.original_ref == originals.receipt(owner.source)
        and generation.source_identity == source_identity(owner)[0]
        and generation.source_fingerprint == "sha256:" + owner.source.sha256
    )


def native_generation(generation: GenerationFacts | None) -> bool:
    """A generation admitted and published by the Images owner.

    The owner records the work generation that admitted a pyramid attempt.  The
    local legacy publisher creates generations without one, and its receipt is
    the only admission authority it records.  Only the Images owner knows the
    source snapshot, generation and object identity behind a native row.
    """
    return generation is not None and generation.job_id is not None


async def owner_generation_read(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None,
    dataset_item_id: uuid.UUID | None,
    generation: int,
):
    """Read one exact generation from the Images owner's public projection.

    The owner revalidates the actor, source snapshot/source generation, object
    identity, width/height and the publication lease record.  It raises
    `yoao_assets_images.contract.ImageFailure` instead of guessing, so callers
    must never translate a refusal into source drift.
    """
    from app.services.assets_images import image_gateway
    from yoao_assets_images.contract import ReadImageGenerationRequest

    request = ReadImageGenerationRequest(
        actor_id=actor_id,
        task_id=task_id,
        dataset_item_id=dataset_item_id,
        generation=generation,
        profile_version=settings.image_pyramid_profile_version,
    )
    async with image_gateway(db) as images:
        return await images.call("read_image_generation", request)


async def owner_generation_reference(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None,
    dataset_item_id: uuid.UUID | None,
    generation: int,
    expected_source_snapshot: str | None,
    profile_version: str | None = None,
):
    """Read one exact saved Native generation through the Images owner.

    The owner validates the pinned source, admits its durable saved-output read
    permit, verifies the manifest/overview receipts and publishes finite URLs.
    The API never resolves target credentials or signs saved members itself.
    """
    from app.services.assets_images import image_gateway
    from yoao_assets_images.transport import ImageGenerationReferenceRequest

    request = ImageGenerationReferenceRequest(
        actor_id=actor_id,
        task_id=task_id,
        dataset_item_id=dataset_item_id,
        generation=generation,
        profile_version=profile_version or settings.image_pyramid_profile_version,
        expected_source_snapshot=expected_source_snapshot,
    )
    async with image_gateway(db) as images:
        return await images.generation_reference(request)


async def owner_asset_references(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    task_id: uuid.UUID | None,
    dataset_item_id: uuid.UUID | None,
    generation: int,
    expected_source_snapshot: str | None,
    items: list[Any],
    profile_version: str | None = None,
):
    """Read exact saved Native tiles through one owner-scoped read permit."""
    from app.services.assets_images import image_gateway
    from yoao_assets_images.transport import ImageAssetReferencesRequest

    request = ImageAssetReferencesRequest(
        actor_id=actor_id,
        task_id=task_id,
        dataset_item_id=dataset_item_id,
        expected_source_snapshot=expected_source_snapshot,
        profile_version=profile_version or settings.image_pyramid_profile_version,
        items=items,
    )
    async with image_gateway(db) as images:
        return await images.asset_references(request)


def pyramid_summary(
    generation: GenerationFacts | None,
    *,
    width: int | None,
    height: int | None,
    profile_version: str | None = None,
) -> ImagePyramidSummary | None:
    if generation is None:
        return None
    logical_width = generation.width or width
    logical_height = generation.height or height
    return ImagePyramidSummary(
        status=(
            "failed"
            if generation.status == "ready" and generation.output_id is None
            else generation.status
        ),  # type: ignore[arg-type]
        source_snapshot=generation.source_snapshot,
        profile_version=profile_version,
        generation=generation.generation,
        width=logical_width,
        height=logical_height,
        tile_size=generation.tile_size,
        format=generation.format,
        required=pyramid_required(logical_width, logical_height),
    )
