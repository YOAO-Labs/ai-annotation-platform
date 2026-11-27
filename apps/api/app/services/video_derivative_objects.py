"""Independent video outputs, durable client ownership and guarded collection."""

import asyncio
import sys
import uuid
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.services import key_removal_accounting as removal
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services import video_derivative_originals as originals
from app.services import video_derivative_storage as physical
from app.services.content_snapshot import ContentError


def utcnow():
    return datetime.now(timezone.utc)


def reject(code="video_derivative_output_changed"):
    raise ContentError(code)


def purpose(identifier):
    return "video_derivative_" + uuid.UUID(str(identifier)).hex


@dataclass(frozen=True)
class Receipt:
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    kind: str
    spec: dict = field(repr=False)
    original_ref: dict = field(repr=False)
    auth_ref: dict = field(repr=False)
    metadata_output_id: uuid.UUID | None
    storage_ref: dict = field(repr=False)
    state: str
    lease_token: uuid.UUID | None
    lease_expires_at: datetime | None
    inventory: dict | None = field(repr=False)
    result: dict | None = field(repr=False)
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    fixture_request_versions: dict | None = field(default=None, repr=False)


def snapshot(row):
    physical.resolve(row)
    return Receipt(
        **{
            name: deepcopy(getattr(row, name, None))
            if name == "fixture_request_versions"
            else deepcopy(getattr(row, name))
            for name in Receipt.__dataclass_fields__
        }
    )


@dataclass(frozen=True, slots=True)
class CacheOutput:
    id: uuid.UUID
    state: str
    storage_ref: dict = field(repr=False)
    inventory: dict | None = field(repr=False)
    fixture_request_versions: dict | None = field(repr=False)


async def get_output(db, identifier, *, lock=False) -> CacheOutput | None:
    if db.autoflush:
        await db.flush()
    row = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.legacy_video_cache_output(:output, :lock)"
                ),
                {"output": identifier, "lock": lock},
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else CacheOutput(**row)


async def read_attempt(db, item_id, kind, identifier, *, lock="read"):
    if db.autoflush:
        await db.flush()
    return await _facts(
        db,
        text(
            "SELECT * FROM yoao_assets_v1.legacy_video_attempt(:item, :kind, :output, :lock)"
        ),
        {"item": item_id, "kind": kind, "output": identifier, "lock": lock},
    )


async def _facts(db, statement, params):
    row = (await db.execute(statement, params)).mappings().one_or_none()
    return None if row is None else SimpleNamespace(**row)


async def locate_pending(db, identifier):
    return await _facts(
        db,
        text("SELECT * FROM yoao_assets_v1.legacy_video_locate_pending(:output)"),
        {"output": identifier},
    )


async def fail_attempt(db, identifier, token, code) -> bool:
    from app.services.video_asset_retry_jobs import sync_output

    current = await _facts(
        db,
        text(
            "SELECT * FROM yoao_assets_v1.legacy_video_fail_attempt(:output, :token, :error)"
        ),
        {"output": identifier, "token": token, "error": code},
    )
    if current is None:
        return False
    await sync_output(db, current)
    return True


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
        if receipt.fixture_request_versions is not None and route is not None:
            reject()
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
                source_permit = await admissions.enter_async_context(
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
                    migration_reads.admit(
                        session, plan.routes[0], purpose(receipt.id), routes=plan.routes
                    )
                )
            await guard(plan.revision)
            await check_reads(session, receipt, plan, lock=True)
            await session.commit()

            @contextmanager
            def opened():
                with ExitStack() as contexts:
                    source = None
                    if plan.source_revision is not None:
                        source = contexts.enter_context(
                            physical.open_storage(
                                output=receipt,
                                permit=source_permit,
                                **(
                                    {"target": source_target}
                                    if source_target is not None
                                    else {}
                                ),
                            )
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
        await originals.drain(asyncio.create_task(session.close()))


READS_INFO = "video_derivative_consumed_reads"


def begin_reads(db, authority):
    db.info[READS_INFO] = (authority, {})


def consumed(db, output, authority):
    scope, plans = getattr(db, "info", {}).get(READS_INFO, (None, {}))
    plan = plans.get(output.id) if scope == authority else None
    if plan is not None and plan.receipt != snapshot(output):
        reject()
    return plan


def remember_reads(db, output, authority, plan):
    if db.info.get(READS_INFO, (None,))[0] != authority:
        begin_reads(db, authority)
    previous = consumed(db, output, authority)
    if previous is not None:
        members = {member[0]: member for member in previous.members}
        for member in plan.members:
            if member[0] in members and members[member[0]] != member:
                reject("read_publication_changed")
            members[member[0]] = member
        if (
            previous.source_revision is not None
            and plan.source_revision is not None
            and previous.source_revision != plan.source_revision
        ):
            reject("storage_maintenance_changed")
        plan = ReadPlan(
            plan.receipt,
            tuple(members[key] for key in sorted(members)),
            previous.source_revision
            if previous.source_revision is not None
            else plan.source_revision,
        )
    db.info[READS_INFO][1][output.id] = plan


async def allocate(
    db, *, dataset_item_id, kind, spec, original_ref, auth_ref, metadata_output_id=None
):
    if (
        not isinstance(dataset_item_id, uuid.UUID)
        or kind not in {"metadata", "frame", "chunk"}
        or (metadata_output_id is None) != (kind == "metadata")
        or (
            metadata_output_id is not None
            and not isinstance(metadata_output_id, uuid.UUID)
        )
        or any(not isinstance(value, dict) for value in (spec, original_ref, auth_ref))
    ):
        reject("video_derivative_input_invalid")
    identifier = uuid.uuid4()
    ref = await physical.allocate(db, identifier)
    await storage_access.check(db, uuid.UUID(ref["location_id"]), lock=True)
    await db.flush()
    allocated = (
        (
            await db.execute(
                text(
                    "SELECT * FROM yoao_assets_v1.legacy_video_allocate_output("
                    ":output, :item, :kind, :spec, :original, :auth, :metadata, :target)"
                ).bindparams(
                    bindparam("spec", type_=JSONB),
                    bindparam("original", type_=JSONB),
                    bindparam("auth", type_=JSONB),
                    bindparam("target", type_=JSONB),
                ),
                {
                    "output": identifier,
                    "item": dataset_item_id,
                    "kind": kind,
                    "spec": spec,
                    "original": original_ref,
                    "auth": auth_ref,
                    "metadata": metadata_output_id,
                    "target": ref,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if allocated is None:
        reject("video_derivative_changed")
    output = SimpleNamespace(
        id=identifier,
        dataset_item_id=dataset_item_id,
        kind=kind,
        spec=deepcopy(spec),
        original_ref=deepcopy(original_ref),
        auth_ref=deepcopy(auth_ref),
        metadata_output_id=metadata_output_id,
        state="building",
        lease_token=None,
        lease_expires_at=None,
        inventory=None,
        result=None,
        error_code=None,
        **allocated,
    )
    if output.fixture_request_versions is not None:
        await storage_access.check(
            db, uuid.UUID(output.storage_ref["location_id"]), lock=True
        )
    return output


async def attach(db, output, *, slot_id, previous_output_id):
    if not await db.scalar(
        text(
            "SELECT yoao_assets_v1.legacy_video_attach_output(:output, :slot, :previous)"
        ),
        {"output": output.id, "slot": slot_id, "previous": previous_output_id},
    ):
        reject("video_derivative_changed")


async def claim(db, output, ttl_seconds=900):
    """Claim a caller-locked attempt exactly once; expired attempts never renew."""
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= originals.JOB_SECONDS:
        reject("video_derivative_lease_invalid")
    physical.resolve(output)
    if (
        output.state != "building"
        or output.lease_token is not None
        or output.lease_expires_at is not None
    ):
        return None
    await db.flush()
    token = uuid.uuid4()
    claimed = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_claim_attempt(:output, :token, :ttl)"
            ),
            {"output": output.id, "token": token, "ttl": ttl_seconds},
        )
    ).one_or_none()
    if claimed is None:
        return None
    output.lease_token = token
    output.lease_expires_at, output.updated_at = claimed
    from app.services.video_asset_retry_jobs import sync_output

    await sync_output(db, output)
    return token


@asynccontextmanager
async def access(
    db,
    receipt,
    guard,
    *,
    require_enabled=True,
    single_attempt=False,
    on_acquired=None,
    owned=False,
):
    """Own client construction through actual close under an independent permit."""
    owner = physical.resolve(receipt)
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    session = factory()
    try:
        location_id = uuid.UUID(owner.ref["location_id"])
        revision = await storage_access.check(session, location_id)
        target = await physical.transport_target(
            session, receipt, require_enabled=require_enabled
        )
        await session.commit()
        async with await storage_access.acquire(
            session, location_id, purpose(receipt.id), expected_revision=revision
        ) as permit:
            if on_acquired is not None:
                await on_acquired(permit)
            await guard(permit.revision)
            context = (
                physical.open_storage(
                    **({"single_attempt": True} if single_attempt else {}),
                    **({"owned": True} if owned else {}),
                )
                if target is None
                else physical.open_storage(
                    target=target,
                    output=receipt,
                    permit=permit,
                    **({"single_attempt": True} if single_attempt else {}),
                    **({"owned": True} if owned else {}),
                )
            )
            entered = []

            def open_owned():
                value = context.__enter__()
                entered.append(True)
                return value

            try:
                client = await originals.physical(
                    open_owned, timeout=originals.JOB_SECONDS
                )
                yield client
            finally:
                if entered:
                    await originals.physical(
                        context.__exit__, *sys.exc_info(), timeout=originals.JOB_SECONDS
                    )
            await guard(permit.revision)
    finally:
        await originals.drain(asyncio.create_task(session.close()))


async def _expire_cache(db, kind, cutoff, limit):
    candidates = (
        await db.execute(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_expired_slots(:kind, :cutoff, :limit)"
            ),
            {"kind": kind, "cutoff": cutoff, "limit": limit},
        )
    ).all()
    await db.commit()
    for item_id, row_id, identifier in candidates:
        try:
            output = await _facts(
                db,
                text(
                    "SELECT * FROM yoao_assets_v1.legacy_video_expire_slot("
                    ":kind, :item, :slot, :output, :cutoff)"
                ),
                {
                    "kind": kind,
                    "item": item_id,
                    "slot": row_id,
                    "output": identifier,
                    "cutoff": cutoff,
                },
            )
            if output is None:
                await db.rollback()
                continue
            physical.resolve(output)
            await db.commit()
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise


async def _expire_building(db, now, limit):
    from app.services.video_asset_retry_jobs import sync_output

    identifiers = (
        await db.scalars(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_abandoned_attempts(:now, :limit)"
            ),
            {"now": now, "limit": limit},
        )
    ).all()
    await db.commit()
    for identifier in identifiers:
        try:
            output = await _facts(
                db,
                text(
                    "SELECT * FROM yoao_assets_v1.legacy_video_expire_attempt(:output, :now)"
                ),
                {"output": identifier, "now": now},
            )
            if output is None:
                await db.rollback()
                continue
            physical.resolve(output)
            await sync_output(db, output)
            await db.commit()
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise


async def _sweeping(db, identifier, receipt):
    current = await _facts(
        db,
        text("SELECT * FROM yoao_assets_v1.legacy_video_sweep_attempt(:output)"),
        {"output": identifier},
    )
    if current is None or snapshot(current) != receipt:
        reject()


async def retire_fixture(db, control, identifier):
    """Retire this saved video output under its sealed fixture owner."""
    from app.services.fixture_media_output_retirement import _retire_output

    def retire(target, output, fixture):
        with physical.open_storage(
            target=target, single_attempt=True, owned=True
        ) as storage:
            return physical.delete_output(storage, output, fixture=fixture)

    await _retire_output(db, control, "video_output", identifier, retire)


async def reconcile(db, *, limit=100, grace_seconds=3600):
    """Use a GC-owned session to detach stale known refs, then retire exact owners."""
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(grace_seconds) is not int
        or grace_seconds < 3600
    ):
        reject("video_derivative_gc_invalid")
    now = utcnow()
    await _expire_cache(
        db,
        "frame",
        now - timedelta(days=settings.video_frame_cache_ttl_days),
        limit,
    )
    await _expire_cache(
        db,
        "chunk",
        now - timedelta(days=settings.video_chunk_cache_ttl_days),
        limit,
    )
    await _expire_building(db, now, limit)
    cutoff = now - timedelta(seconds=grace_seconds)
    identifiers = (
        await db.scalars(
            text(
                "SELECT * FROM yoao_assets_v1.legacy_video_retirement_candidates(:now, :cutoff, :limit)"
            ),
            {"now": now, "cutoff": cutoff, "limit": limit},
        )
    ).all()
    await db.commit()
    removed = 0
    for identifier in identifiers:
        try:
            output = await _facts(
                db,
                text(
                    "SELECT * FROM yoao_assets_v1.legacy_video_begin_sweep(:output, :now, :cutoff)"
                ),
                {"output": identifier, "now": now, "cutoff": cutoff},
            )
            if output is None:
                await db.rollback()
                continue
            receipt = snapshot(output)
            await db.commit()
            if output.published:
                continue
            accounting = removal.recorder(
                db, receipt, producer="video_derivative_removals", purpose="media_cache"
            )
            owned_permit = None

            async def acquired(permit):
                nonlocal owned_permit
                owned_permit = permit

            async def guarded(revision):
                await _sweeping(db, identifier, receipt)
                if not await db.scalar(
                    text(
                        "SELECT yoao_assets_v1.legacy_video_sweep_permit(:output, :revision)"
                    ),
                    {"output": identifier, "revision": revision},
                ):
                    reject("storage_operation_unsettled")
                await storage_access.check(
                    db,
                    uuid.UUID(receipt.storage_ref["location_id"]),
                    expected_revision=revision,
                    lock=True,
                )
                await db.commit()

            failure, count = None, None
            try:
                async with access(
                    db,
                    receipt,
                    guarded,
                    require_enabled=False,
                    on_acquired=acquired,
                    owned=True,
                ) as client:
                    count = await originals.physical(
                        physical.delete_output,
                        client,
                        receipt,
                        timeout=originals.JOB_SECONDS,
                        accounting=accounting,
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
            await _sweeping(db, identifier, receipt)
            if not await db.scalar(
                text(
                    "SELECT yoao_assets_v1.legacy_video_finish_sweep(:output, :updated_at)"
                ),
                {"output": identifier, "updated_at": receipt.updated_at},
            ):
                reject()
            await db.commit()
            removed += count
        except (ContentError, physical.VideoDerivativeStorageError):
            await db.rollback()
        except DBAPIError as exc:
            await db.rollback()
            if not storage_access.unavailable(exc):
                raise
    return removed
