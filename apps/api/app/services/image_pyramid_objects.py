"""Independent pyramid outputs, durable admission and short publication guards."""

from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import timedelta
import asyncio
import uuid
import re
import sys

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.services import image_pyramid_data as data
from app.services import image_pyramid_originals as originals
from app.services import image_pyramid_storage as physical
from app.services import key_removal_accounting as removal
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service


def purpose(identifier):
    return "image_pyramid_" + uuid.UUID(str(identifier)).hex


def reject(code="image_pyramid_output_changed"):
    raise ContentError(code)


def url_expiry_seconds(generation):
    from app.services.image_pyramid import native_generation

    if native_generation(generation):
        return min(
            settings.image_pyramid_url_expiry_seconds,
            generation.policy["url_expiry_seconds"],
        )
    return settings.image_pyramid_url_expiry_seconds


@dataclass(frozen=True)
class Receipt:
    id: uuid.UUID
    asset_id: uuid.UUID
    generation: int
    lease_token: uuid.UUID
    storage_ref: dict
    inventory: dict | None
    state: str
    _read_storage: object = field(default=None, repr=False, compare=False)


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


@dataclass(frozen=True)
class ReadPlan:
    receipt: Receipt
    members: tuple
    source_revision: int | None

    @property
    def routes(self):
        return tuple(member[3] for member in self.members if member[3] is not None)

    @property
    def revision(self):
        return (
            self.source_revision
            if self.source_revision is not None
            else self.routes[0].maintenance_revision
        )


async def read_plan(db, output, keys):
    receipt = snapshot(output)
    if receipt.state != "ready":
        reject()
    owner = physical.resolve(receipt)
    inventory = physical.validate_inventory(receipt)
    members = []
    for key in sorted(set(keys)):
        facts = inventory.get(key)
        if facts is None:
            reject()
        route = await migration_reads.resolve(
            db, owner.ref, key, facts["sha256"], facts["size_bytes"]
        )
        members.append((key, facts["sha256"], facts["size_bytes"], route))
    if not members:
        reject()
    revision = None
    if any(member[3] is None for member in members):
        revision = await storage_access.check(db, uuid.UUID(owner.ref["location_id"]))
    plan = ReadPlan(receipt, tuple(members), revision)
    await check_reads(db, receipt, plan)
    return plan


async def check_reads(db, output, plan, *, lock=False):
    if (
        not isinstance(plan, ReadPlan)
        or snapshot(output) != plan.receipt
        or output.state != "ready"
    ):
        reject()
    if not plan.members or len({member[0] for member in plan.members}) != len(
        plan.members
    ):
        reject()
    if any(member[3] is None for member in plan.members) != (
        plan.source_revision is not None
    ):
        reject()
    if plan.routes:
        migration_reads.ReadClientSet(None, plan.routes)
    owner = physical.resolve(output)
    inventory = physical.validate_inventory(output)
    for key, sha, size, route in plan.members:
        if inventory.get(key) != {"sha256": sha, "size_bytes": size}:
            reject()
        if route is not None:
            if (
                route.source_location_id != uuid.UUID(owner.ref["location_id"])
                or route.source_endpoint_url != owner.ref["endpoint_url"]
                or route.source_bucket != owner.bucket
                or route.source_object_key != key
                or route.sha256 != sha
                or route.size_bytes != size
            ):
                reject()
            await read_publications.recheck(db, route, lock=lock)
    if any(member[3] is None for member in plan.members):
        if plan.source_revision is None:
            reject()
        await storage_access.check(
            db,
            uuid.UUID(owner.ref["location_id"]),
            expected_revision=plan.source_revision,
            lock=lock,
        )


@asynccontextmanager
async def read_access(db, receipt, plan, guard):
    """Close every selected physical handle before releasing its actual permits."""
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    session = factory()
    try:
        await check_reads(session, receipt, plan)
        source_target = (
            await physical.transport_target(session, receipt)
            if plan.source_revision is not None
            else None
        )
        await session.commit()
        async with AsyncExitStack() as admissions:
            if plan.source_revision is not None:
                await admissions.enter_async_context(
                    await storage_access.acquire(
                        session,
                        uuid.UUID(receipt.storage_ref["location_id"]),
                        purpose(receipt.id),
                        expected_revision=plan.source_revision,
                    )
                )
            admitted = None
            if plan.routes:
                admitted = await admissions.enter_async_context(
                    migration_reads.admit(session, plan.routes[0], purpose(receipt.id))
                )
            await guard(plan.revision)
            await check_reads(session, receipt, plan, lock=True)
            await session.commit()

            @contextmanager
            def opened():
                with ExitStack() as contexts:
                    source = None
                    if plan.source_revision is not None:
                        source = (
                            contexts.enter_context(
                                physical.open_storage(target=source_target)
                            )
                            if source_target is not None
                            else storage_service
                        )
                    readers = (
                        contexts.enter_context(
                            migration_reads.clients(plan.routes, admitted.target)
                        )
                        if admitted is not None
                        else None
                    )
                    yield physical.ReadStorage(
                        deepcopy(receipt.storage_ref), plan.members, source, readers
                    )

            context, entered = opened(), []

            def open_owned():
                value = context.__enter__()
                entered.append(True)
                return value

            try:
                storage = await originals.physical(open_owned)
                yield storage
            finally:
                if entered:
                    await originals.physical(context.__exit__, *sys.exc_info())
            await guard(plan.revision)
            await check_reads(session, receipt, plan, lock=True)
            await session.commit()
    finally:
        await storage_access.finish(asyncio.create_task(session.close()))


async def get_output(db, identifier, *, lock=False):
    return await data.read_output(db, identifier, lock=lock)


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
    read_plan=None,
    lock=True,
):
    """Never commit caller state or perform provider I/O under these locks."""
    if authority is not None:
        await originals.check(db, authority, lock=lock)
    current = await originals.pin(db, owner.kind, owner.id, lock=lock)
    output = await data.read_context(
        db, asset_id, generation_number, output_id, lock=lock
    )
    if output is None:
        reject()
    if (
        output.dataset_item_id != (owner.id if owner.kind == "dataset_item" else None)
        or output.task_id != (owner.id if owner.kind == "task" else None)
        or output.original_ref != originals.receipt(owner.source)
    ):
        reject()
    if originals.receipt(current) != output.original_ref:
        reject("source_changed")
    if lease_token is None:
        if (
            output.status != "ready"
            or output.active_generation != generation_number
            or output.state != "ready"
            or output.inventory is None
        ):
            reject("asset_not_ready")
    else:
        from app.services.image_pyramid import utcnow

        if (
            output.status != "building"
            or output.generation_token != lease_token
            or output.lease_token != lease_token
            or output.state != "building"
            or output.building_generation != generation_number
            or output.lease_expires_at is None
            or output.lease_expires_at <= utcnow()
        ):
            reject("lease_lost")
    ref = physical.resolve(output).ref
    if read_plan is not None:
        if lease_token is not None:
            reject()
        await check_reads(db, snapshot(output), read_plan, lock=lock)
        revision = read_plan.revision
    else:
        revision = await storage_access.check(
            db,
            uuid.UUID(ref["location_id"]),
            expected_revision=gate_revision,
            lock=lock,
        )
    if authority is not None:
        originals.clock(authority)
    return snapshot(output), revision


async def publish(
    db, asset, generation, output_id, token, facts, inventory, gate_revision
):
    output = await data.lock_output_for_publish(
        db, asset.id, generation.generation, token, output_id
    )
    if output is None:
        reject()
    owner = physical.resolve(output)
    await storage_access.check(
        db,
        uuid.UUID(owner.ref["location_id"]),
        expected_revision=gate_revision,
        lock=True,
    )
    validate_inventory(output, generation, inventory)
    try:
        return await data.publish(
            db,
            asset.id,
            generation.generation,
            token,
            output_id,
            facts,
            inventory,
            gate_revision,
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "P0500":
            from app.services.image_pyramid import ImagePyramidError

            raise ImagePyramidError("thumbnail_publication_rejected") from exc
        raise


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
async def reading(db, owner, generation, *, authority=None, keys=None):
    """Legacy physical reads only; saved Native outputs use the Images owner."""
    from app.services.image_pyramid import native_generation

    if native_generation(generation):
        reject("image_pyramid_native_owner_required")
    if generation.output_id is None:
        reject("image_pyramid_storage_unpinned")
    if keys is not None:
        factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
        async with factory() as selected:
            row = await get_output(selected, generation.output_id)
            if row is None:
                reject()
            plan = await read_plan(selected, row, keys)

        async def guard(_revision):
            async with factory() as fresh:
                await check(
                    fresh,
                    owner,
                    generation.asset_id,
                    generation.generation,
                    generation.output_id,
                    authority=authority,
                    read_plan=plan,
                )
                await fresh.commit()

        async with read_access(db, plan.receipt, plan, guard) as storage:
            yield replace(plan.receipt, _read_storage=storage)
        return
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
    if getattr(output, "_read_storage", None) is not None:
        yield output._read_storage
        return
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


async def thumbnail_url(db, owner_kind, owner_id, path, *, actor_id):
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
            generation = await data.read_generation(
                session, asset.id, asset.active_generation
            )
            if generation is None or generation.overview_key != path:
                return None
            from app.services.image_pyramid import native_generation

            if native_generation(generation):
                return None
            await session.commit()
            async with reading(session, owner, generation, keys=(path,)) as output:
                async with transport(session, output) as storage:
                    await originals.physical(
                        physical.verify_object, storage, output, path
                    )
                    url = physical.signed_url(
                        storage, output, path, url_expiry_seconds(generation)
                    )
            return url
    except (ContentError, physical.ImagePyramidStorageError, HTTPException):
        return None
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            return None
        raise


async def retire_fixture(db, control, identifier):
    """Retire this saved pyramid output under its sealed fixture owner."""
    from app.services.fixture_media_retirement import _retire_output

    def retire(target, output, fixture):
        with physical.open_storage(
            target=target, single_attempt=True, owned=True
        ) as storage:
            return physical.delete(storage, output, fixture=fixture)

    await _retire_output(db, control, "pyramid_output", identifier, retire)


async def reconcile(db, storage):
    """Retire known, unused outputs before exact saved-prefix physical deletion."""
    from app.services.image_pyramid import fail_generation, utcnow

    cutoff = utcnow() - timedelta(hours=settings.image_pyramid_gc_grace_hours)
    for asset_id, number in await data.expired(db):
        await fail_generation(
            db,
            asset_id=asset_id,
            generation_number=number,
            lease_token=None,
            error_code="lease_expired",
        )
        await db.commit()
    for asset_id, number in await data.stale(db, cutoff):
        await data.remove_stale(db, asset_id, number, cutoff)
        await db.commit()
    identifiers = await data.retirable(db, cutoff)
    await db.commit()
    removed = 0
    for identifier in identifiers:
        output = await data.begin_retirement(db, identifier, cutoff)
        if output is None:
            await db.rollback()
            continue
        physical.resolve(output)
        published = output.state == "ready"
        await db.commit()
        if published:
            continue
        receipt = replace(snapshot(output), state="retiring")
        try:
            target = await physical.transport_target(db, receipt, require_enabled=False)
            accounting = removal.recorder(
                db, receipt, producer="image_pyramid_removals", purpose="media_cache"
            )
            owned_permit = None

            async def admitted(permit):
                nonlocal owned_permit
                owned_permit = permit
                error = await data.check_retirement(
                    db, identifier, receipt.storage_ref, permit.id
                )
                if error is not None:
                    reject(error)
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
            if not await data.finish_retirement(db, identifier, receipt.storage_ref):
                reject()
            await db.commit()
            removed += count
        except (ContentError, physical.ImagePyramidStorageError):
            await db.rollback()
    return removed
