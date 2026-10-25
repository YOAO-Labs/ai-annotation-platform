"""Independent pyramid outputs, durable admission and short publication guards."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import uuid
import re
import sys

from sqlalchemy import String, cast, exists, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.image_pyramid import (
    ImagePyramidAsset as Asset,
    ImagePyramidGeneration as Generation,
    ImagePyramidOutput as Output,
)
from app.db.models.storage_maintenance import StorageOperation
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from app.services import key_removal_accounting as removal
from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service


def purpose(identifier):
    return "image_pyramid_" + uuid.UUID(str(identifier)).hex


def reject(code="image_pyramid_output_changed"):
    raise ContentError(code)


@dataclass(frozen=True)
class Receipt:
    id: uuid.UUID
    asset_id: uuid.UUID
    generation: int
    lease_token: uuid.UUID
    storage_ref: dict
    inventory: dict | None
    state: str


def snapshot(row):
    physical.resolve(row)
    return Receipt(
        row.id,
        row.asset_id,
        row.generation,
        row.lease_token,
        deepcopy(row.storage_ref),
        deepcopy(row.inventory),
        row.state,
    )


async def allocate(db, asset, generation, token):
    identifier = uuid.uuid4()
    ref = await physical.allocate(db, identifier)
    await storage_access.check(db, uuid.UUID(ref["location_id"]), lock=True)
    output = Output(
        id=identifier,
        asset_id=asset.id,
        generation=generation.generation,
        lease_token=token,
        storage_ref=ref,
        state="building",
    )
    db.add(output)
    await db.flush()
    generation.output_id = identifier
    return snapshot(output)


async def _row(db, model, predicate, *, lock=False):
    statement = select(model).where(predicate)
    if lock:
        statement = statement.with_for_update(read=True, nowait=True)
    return await db.scalar(statement.execution_options(populate_existing=True))


async def check(
    db,
    owner,
    asset_id,
    generation_number,
    output_id,
    *,
    authority=None,
    lease_token=None,
    gate_revision=None,
    lock=True,
):
    """Never commit caller state or perform provider I/O under these locks."""
    if authority is not None:
        await originals.check(db, authority, lock=lock)
    asset = await _row(db, Asset, Asset.id == asset_id, lock=lock)
    generation = await _row(
        db,
        Generation,
        (Generation.asset_id == asset_id)
        & (Generation.generation == generation_number),
        lock=lock,
    )
    output = await _row(db, Output, Output.id == output_id, lock=lock)
    if asset is None or generation is None or output is None:
        reject()
    if (
        asset.dataset_item_id != (owner.id if owner.kind == "dataset_item" else None)
        or asset.task_id != (owner.id if owner.kind == "task" else None)
        or generation.output_id != output_id
        or output.asset_id != asset_id
        or output.generation != generation_number
        or generation.original_ref != originals.receipt(owner.source)
    ):
        reject()
    current = await originals.pin(db, owner.kind, owner.id, lock=lock)
    if originals.receipt(current) != generation.original_ref:
        reject("source_changed")
    if lease_token is None:
        if (
            generation.status != "ready"
            or asset.active_generation != generation_number
            or output.state != "ready"
            or output.inventory is None
        ):
            reject("asset_not_ready")
    else:
        from app.services.image_pyramid import utcnow

        if (
            generation.status != "building"
            or generation.lease_token != lease_token
            or output.lease_token != lease_token
            or output.state != "building"
            or asset.building_generation != generation_number
            or generation.lease_expires_at is None
            or generation.lease_expires_at <= utcnow()
        ):
            reject("lease_lost")
    ref = physical.resolve(output).ref
    revision = await storage_access.check(
        db,
        uuid.UUID(ref["location_id"]),
        expected_revision=gate_revision,
        lock=lock,
    )
    if authority is not None:
        originals.clock(authority)
    return snapshot(output), revision


async def publish(db, asset, generation, output_id, token, inventory, gate_revision):
    output = await db.scalar(
        select(Output)
        .where(Output.id == output_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if (
        output is None
        or output.state != "building"
        or output.asset_id != asset.id
        or output.generation != generation.generation
        or output.lease_token != token
        or generation.output_id != output_id
    ):
        reject()
    owner = physical.resolve(output)
    await storage_access.check(
        db,
        uuid.UUID(owner.ref["location_id"]),
        expected_revision=gate_revision,
        lock=True,
    )
    validate_inventory(output, generation, inventory)
    output.inventory = deepcopy(inventory)
    output.state = "ready"
    physical.resolve(output)
    await db.flush()


def validate_inventory(output, generation, inventory):
    from app.services.image_pyramid import (
        ImagePyramidError,
        pyramid_levels,
        validate_dimensions,
    )

    owner = physical.resolve(output)
    if type(generation.width) is not int or type(generation.height) is not int:
        reject("image_pyramid_storage_invalid_receipt")
    try:
        validate_dimensions(generation.width, generation.height)
    except ImagePyramidError:
        reject("image_pyramid_storage_invalid_receipt")
    levels = pyramid_levels(generation.width, generation.height)
    tiles = {
        owner.key(f"tiles/{level['level']}/{x}/{y}.webp")
        for level in levels
        for y in range(level["rows"])
        for x in range(level["columns"])
    }
    manifest_key, overview_key = owner.key("manifest.json"), owner.key("overview.webp")
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"schema", "objects"}
        or type(inventory["schema"]) is not int
        or inventory["schema"] != 1
        or not isinstance(inventory["objects"], dict)
        or set(inventory["objects"]) != tiles | {manifest_key, overview_key}
        or generation.manifest_key != manifest_key
        or generation.overview_key != overview_key
        or generation.tile_count != len(tiles)
        or generation.max_level != len(levels) - 1
        or generation.format != "webp"
        or generation.tile_size != 512
        or generation.overlap != 1
    ):
        reject("image_pyramid_storage_invalid_receipt")
    total = 0
    for value in inventory["objects"].values():
        if (
            not isinstance(value, dict)
            or set(value) != {"sha256", "size_bytes"}
            or not isinstance(value["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            or type(value["size_bytes"]) is not int
            or not 0 < value["size_bytes"] <= settings.image_pyramid_max_derived_bytes
        ):
            reject("image_pyramid_storage_invalid_receipt")
        total += value["size_bytes"]
    overview_level = next(
        level for level in levels if level["width"] <= 512 and level["height"] <= 512
    )
    if (
        total != generation.retained_bytes
        or total > settings.image_pyramid_max_derived_bytes
        or generation.manifest_digest
        != "sha256:" + inventory["objects"][manifest_key]["sha256"]
        or generation.overview_digest
        != "sha256:" + inventory["objects"][overview_key]["sha256"]
        or (generation.overview_width, generation.overview_height)
        != (overview_level["width"], overview_level["height"])
    ):
        reject("image_pyramid_storage_invalid_receipt")


async def manifest(storage, output, generation):
    from app.services.image_pyramid import build_manifest

    validate_inventory(output, generation, output.inventory)
    value = await originals.physical(
        physical.read_manifest, storage, output, generation.manifest_digest
    )
    expected = build_manifest(
        generation=generation.generation,
        source_fingerprint=generation.source_fingerprint,
        width=generation.width,
        height=generation.height,
        overview_width=generation.overview_width,
        overview_height=generation.overview_height,
        overview_digest=generation.overview_digest,
    )
    expected["normalizationVersion"] = generation.normalization_version
    if value != expected:
        reject("image_pyramid_storage_invalid_receipt")
    return value


@asynccontextmanager
async def reading(db, owner, generation, *, authority=None):
    if generation.output_id is None:
        reject("image_pyramid_storage_unpinned")
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as session:
        output, revision = await check(
            session,
            owner,
            generation.asset_id,
            generation.generation,
            generation.output_id,
            authority=authority,
        )
        await session.commit()
        async with await storage_access.acquire(
            session,
            uuid.UUID(output.storage_ref["location_id"]),
            purpose(output.id),
            expected_revision=revision,
        ):
            output, _ = await check(
                session,
                owner,
                generation.asset_id,
                generation.generation,
                output.id,
                authority=authority,
                gate_revision=revision,
            )
            await session.commit()
            yield output
            await check(
                session,
                owner,
                generation.asset_id,
                generation.generation,
                output.id,
                authority=authority,
                gate_revision=revision,
            )
            await session.commit()


@asynccontextmanager
async def transport(db, output):
    """Use live credentials while the enclosing reading permit owns all I/O."""
    target = await physical.transport_target(db, output)
    if target is None:
        yield storage_service
        return
    context, entered = physical.open_storage(target=target), []

    def open_owned():
        value = context.__enter__()
        entered.append(True)
        return value

    try:
        yield await originals.physical(open_owned)
    finally:
        if entered:
            await originals.physical(context.__exit__, *sys.exc_info())


async def thumbnail_url(db, owner_kind, owner_id, path):
    """A cache key alone never grants physical ownership to a common serializer."""
    if not path:
        return None
    from app.services.image_pyramid import get_asset_for_owner, load_owner
    from fastapi import HTTPException
    from sqlalchemy.exc import DBAPIError

    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    try:
        async with factory() as session:
            owner = await load_owner(session, owner_kind, owner_id)
            asset = await get_asset_for_owner(session, owner)
            if asset is None or asset.active_generation is None:
                return None
            generation = await session.get(
                Generation, (asset.id, asset.active_generation)
            )
            if generation is None or generation.overview_key != path:
                return None
            await session.commit()
            async with reading(session, owner, generation) as output:
                async with transport(session, output) as storage:
                    await originals.physical(
                        physical.verify_object, storage, output, path
                    )
                    url = physical.signed_url(
                        storage, output, path, settings.image_pyramid_url_expiry_seconds
                    )
            return url
    except (ContentError, physical.ImagePyramidStorageError, HTTPException):
        return None
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            return None
        raise


async def reconcile(db, storage):
    """Retire known, unused outputs before exact saved-prefix physical deletion."""
    from app.services.image_pyramid import fail_generation, utcnow

    cutoff = utcnow() - timedelta(hours=settings.image_pyramid_gc_grace_hours)
    candidates = list(
        (
            await db.execute(
                select(Generation.asset_id, Generation.generation)
                .where(
                    Generation.status == "building",
                    Generation.lease_expires_at <= utcnow(),
                )
                .limit(100)
            )
        ).all()
    )
    for asset_id, number in candidates:
        await fail_generation(
            db,
            asset_id=asset_id,
            generation_number=number,
            lease_token=None,
            error_code="lease_expired",
        )
        await db.commit()
    stale = list(
        (
            await db.execute(
                select(Generation.asset_id, Generation.generation)
                .join(Asset, Asset.id == Generation.asset_id)
                .where(
                    Generation.output_id.is_not(None),
                    Generation.updated_at < cutoff,
                    Generation.status.in_(("ready", "failed")),
                    Generation.generation.is_distinct_from(Asset.active_generation),
                    Generation.generation.is_distinct_from(Asset.building_generation),
                )
                .order_by(
                    Generation.updated_at, Generation.asset_id, Generation.generation
                )
                .limit(100)
            )
        ).all()
    )
    for asset_id, number in stale:
        asset = await db.scalar(
            select(Asset).where(Asset.id == asset_id).with_for_update()
        )
        generation = await db.scalar(
            select(Generation)
            .where(Generation.asset_id == asset_id, Generation.generation == number)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            asset is not None
            and generation is not None
            and number not in {asset.active_generation, asset.building_generation}
            and generation.status in {"ready", "failed"}
            and generation.updated_at < cutoff
        ):
            await db.delete(generation)
        await db.commit()
    identifiers = list(
        await db.scalars(
            select(Output.id)
            .where(
                Output.state != "deleted",
                Output.updated_at < cutoff,
                ~exists().where(Generation.output_id == Output.id),
                ~exists().where(
                    StorageOperation.purpose
                    == "image_pyramid_" + func.replace(cast(Output.id, String), "-", "")
                ),
            )
            .order_by(Output.updated_at, Output.id)
            .limit(100)
        )
    )
    await db.commit()
    removed = 0
    for identifier in identifiers:
        output = await db.scalar(
            select(Output)
            .where(Output.id == identifier)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if output is None or output.state == "deleted":
            await db.rollback()
            continue
        if await db.scalar(
            select(exists().where(Generation.output_id == identifier))
        ) or await db.scalar(
            select(exists().where(StorageOperation.purpose == purpose(identifier)))
        ):
            await db.rollback()
            continue
        physical.resolve(output)
        published = output.state == "ready"
        output.state = "retiring"
        await db.commit()
        if published:
            # Cascading business deletion does not timestamp the last reference.
            # Start grace here so an already signed URL can finish its lifetime.
            continue
        receipt = snapshot(output)
        try:
            target = await physical.transport_target(db, receipt, require_enabled=False)
            accounting = removal.recorder(
                db, receipt, producer="image_pyramid_removals", purpose="media_cache"
            )
            owned_permit = None

            async def admitted(permit):
                nonlocal owned_permit
                owned_permit = permit
                current = await db.scalar(
                    select(Output)
                    .where(Output.id == identifier)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    current is None
                    or current.state != "retiring"
                    or current.storage_ref != receipt.storage_ref
                    or await db.scalar(
                        select(exists().where(Generation.output_id == identifier))
                    )
                ):
                    reject()
                # Unknown earlier physical operations remain a durable barrier.
                if await db.scalar(
                    select(
                        exists().where(
                            StorageOperation.purpose == purpose(identifier),
                            StorageOperation.id != permit.id,
                        )
                    )
                ):
                    reject("storage_operation_unsettled")
                await storage_access.check(
                    db, permit.location_id, expected_revision=permit.revision, lock=True
                )
                await db.commit()

            def remove():
                if target is not None:
                    with physical.open_storage(target=target, owned=True) as owned:
                        return physical.delete(owned, receipt, accounting=accounting)
                owned = None
                try:
                    from app.services.storage import StorageService

                    try:
                        owned = StorageService(
                            **(
                                {"client_config": storage.client.meta.config}
                                if storage is not None
                                else {}
                            )
                        )
                    except BaseException as exc:
                        raise storage_access.mark_unsettled(exc)
                    return physical.delete(owned, receipt, accounting=accounting)
                finally:
                    if owned is not None:
                        storage_access.close_registered(owned.client)

            failure, count = None, None
            try:
                count = await storage_access.run(
                    db,
                    uuid.UUID(receipt.storage_ref["location_id"]),
                    purpose(identifier),
                    remove,
                    on_acquired=admitted,
                )
            except BaseException as exc:
                failure = exc
            failure = await removal.finish(
                accounting,
                failure,
                settled=owned_permit is not None
                and getattr(owned_permit, "_settled", False) is True,
            )
            if failure is not None:
                raise failure
            current = await db.scalar(
                select(Output)
                .where(Output.id == identifier)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                current is None
                or current.state != "retiring"
                or current.storage_ref != receipt.storage_ref
            ):
                reject()
            current.state = "deleted"
            await db.commit()
            removed += count
        except (ContentError, physical.ImagePyramidStorageError):
            await db.rollback()
    return removed
