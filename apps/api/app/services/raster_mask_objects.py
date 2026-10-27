"""Durable RLE owners, caller publication guards and exact-object retirement.

Business sessions retain their own transaction and lock order. Every allocation,
publication and physical-I/O receipt uses an independent short transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import delete, exists, func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.raster_mask_object import (
    RasterMaskObject as Object,
    RasterMaskPublication as Publication,
)
from app.db.models.raster_mask_upload import RasterMaskUpload
from app.db.models.storage_maintenance import StorageOperation
from app.services import raster_object_storage as physical, storage_access
from app.services import conditional_write_accounting as accounting
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.original_read_plan import ReadRoute
from app.services.storage import storage_service

GRACE = timedelta(hours=24)
PUBLICATIONS_INFO = "raster_mask_publications"
_CANONICAL = (
    "logical_key",
    "canonical_sha256",
    "canonical_size_bytes",
    "height",
    "width",
    "runs",
    "storage_encoding",
)


def now():
    return datetime.now(timezone.utc)


def _reject(code="raster_object_storage_owner_changed", status=409):
    raise physical.RasterObjectStorageError(code, status)


def purpose(operation, identifier):
    return "raster_object_" + operation + "_" + uuid.UUID(str(identifier)).hex


def _location(receipt):
    return uuid.UUID(receipt.storage_ref["location_id"])


async def _settled(awaitable):
    """A cancellation cannot abandon a commit whose outcome is still unknown."""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await storage_access.finish(task)
        except BaseException as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(cancelled)
        raise


@asynccontextmanager
async def _context(db):
    if db is not None:
        yield db
        return
    # Workers may enter through a different event loop on every invocation.
    from app.workers._db import task_session

    async with task_session() as owned:
        yield owned


@asynccontextmanager
async def _session(db):
    session = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)()
    try:
        await session.execute(text("SET LOCAL lock_timeout = '1s'"))
        yield session
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            _reject("raster_object_storage_busy")
        raise
    finally:
        await _settled(session.close())


async def _commit(db):
    await _settled(db.commit())


async def _publish_lock(db, key):
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": "aap:rle-publish:" + key},
    )


async def _content_try_lock(db, key):
    return bool(
        await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "aap:raster-mask:" + key},
        )
    )


async def _owners(db, identifiers, *, read=False):
    result = {}
    for identifier in sorted(set(identifiers)):
        row = await db.scalar(
            select(Object)
            .where(Object.id == identifier)
            .with_for_update(read=read, nowait=True)
            .execution_options(populate_existing=True)
        )
        if row is not None:
            result[identifier] = row
    return result


async def _gate(db, receipt, revision=None):
    return await storage_access.check(
        db, _location(receipt), expected_revision=revision, lock=True
    )


async def _read_plan(db, receipt):
    owner = physical.resolve(receipt)
    route = await migration_reads.resolve(
        db,
        owner.ref,
        owner.object_key,
        receipt.stored_sha256,
        receipt.stored_size_bytes,
    )
    if route is None:
        return await _gate(db, receipt), None
    # Persist the source Gate fact, never the target Permit revision. The
    # published route separately admits reads while the source is stopped.
    revision = (await storage_access.state(db, _location(receipt)))["revision"]
    await read_publications.recheck(db, route, lock=True)
    return revision, route


async def _read_gate(db, receipt, revision, read_route):
    if read_route is None:
        await _gate(db, receipt, revision)
    else:
        await read_publications.recheck(db, read_route, lock=True)


def _current(row, receipt, state):
    if row is None or row.state != state or not physical.same(row, receipt):
        _reject()


def _matching_facts(row, facts):
    if any(getattr(row, name) != facts[name] for name in _CANONICAL):
        _reject("raster_object_storage_integrity_mismatch")


def _matching_reference(reference, receipt):
    expected = physical.reference(receipt)
    size = reference.get("size") if isinstance(reference, dict) else None
    encoding = (
        reference.get("storage_encoding") if isinstance(reference, dict) else None
    )
    if encoding is None:
        encoding = (
            "gzip"
            if isinstance(reference, dict)
            and reference.get("encoding") == "coco_rle_gzip"
            and expected["object_key"].endswith(".json.gz")
            else "identity"
        )
    if (
        not isinstance(reference, dict)
        or reference.get("encoding") not in ("coco_rle_ref", "coco_rle_gzip")
        or reference.get("object_key") != expected["object_key"]
        or reference.get("sha256") != expected["sha256"]
        or type(reference.get("bytes")) is not int
        or reference["bytes"] != expected["bytes"]
        or type(reference.get("runs")) is not int
        or reference["runs"] != expected["runs"]
        or not isinstance(size, (list, tuple))
        or len(size) != 2
        or any(type(part) is not int for part in size)
        or list(size) != expected["size"]
        or encoding != receipt.storage_encoding
    ):
        _reject("raster_object_storage_integrity_mismatch")


@dataclass(frozen=True)
class _Guard:
    id: uuid.UUID
    receipt: object
    revision: int
    read_route: ReadRoute | None = None


def _remember(db, guard):
    if db is not None and guard is not None:
        guards = db.info.setdefault(PUBLICATIONS_INFO, {})
        guards.setdefault(guard.receipt.logical_key, {})[guard.id] = guard


async def _add_guard(db, receipt, revision, enabled, *, read_route=None):
    if not enabled:
        return None
    guard = _Guard(uuid.uuid4(), receipt, revision, read_route)
    db.add(
        Publication(
            id=guard.id,
            object_id=receipt.id,
            gate_revision=revision,
            created_at=now(),
        )
    )
    await db.flush()
    return guard


async def _require_guard(db, guard):
    if guard is not None and not await db.scalar(
        select(
            exists().where(
                Publication.id == guard.id,
                Publication.object_id == guard.receipt.id,
                Publication.gate_revision == guard.revision,
            )
        )
    ):
        _reject()


async def _initial(db, facts, guarded):
    async with _session(db) as session:
        await _publish_lock(session, facts["logical_key"])
        row = await session.scalar(
            select(Object)
            .where(Object.logical_key == facts["logical_key"], Object.state == "ready")
            .with_for_update(nowait=True)
        )
        candidate = row is None
        if candidate:
            row = Object(id=uuid.uuid4(), **facts, created_at=now(), state="pending")
            row.storage_ref = await physical.allocate(session, row)
        else:
            _matching_facts(row, facts)
        receipt = physical.snapshot(row)
        if candidate:
            revision, read_route = await _gate(session, receipt), None
        else:
            revision, read_route = await _read_plan(session, receipt)
        if candidate:
            session.add(row)
            await session.flush()
        guard = await _add_guard(
            session, receipt, revision, guarded, read_route=read_route
        )
        await _commit(session)
        return receipt, revision, guard, candidate, read_route


async def _io(
    db,
    receipt,
    revision,
    operation,
    storage,
    *args,
    state="ready",
    accounting=None,
    permits=None,
    read_route=None,
):
    if read_route is not None:
        if operation != "read" or state != "ready":
            _reject()

        async def read_acquired(permit):
            async with _session(db) as session:
                row = (await _owners(session, [receipt.id], read=True)).get(receipt.id)
                _current(row, receipt, "ready")
                await _read_gate(session, receipt, revision, read_route)
                await _commit(session)

        return await migration_reads.run(
            db,
            read_route,
            purpose("read", receipt.id),
            lambda reader: physical.read_routed(reader, receipt),
            on_acquired=read_acquired,
        )
    target = None

    async def acquired(permit):
        nonlocal target
        if permits is not None:
            permits.append(permit)
        async with _session(db) as session:
            row = (await _owners(session, [receipt.id], read=True)).get(receipt.id)
            _current(row, receipt, state)
            await _gate(session, receipt, revision)
            if permit.revision != revision:
                _reject()
            target = await physical.io_target(session, receipt)
            await _commit(session)

    return await storage_access.run(
        db,
        _location(receipt),
        purpose(operation, receipt.id),
        lambda: physical.run_io(
            target,
            storage.client,
            operation,
            receipt,
            *args,
            **({"accounting": accounting} if accounting is not None else {}),
        ),
        expected_revision=revision,
        on_acquired=acquired,
    )


async def _touch(db, receipt, revision, guard=None, *, read_route=None):
    async with _session(db) as session:
        await _publish_lock(session, receipt.logical_key)
        row = (await _owners(session, [receipt.id])).get(receipt.id)
        _current(row, receipt, "ready")
        await _read_gate(session, receipt, revision, read_route)
        await _require_guard(session, guard)
        row.last_used_at = max(now(), row.last_used_at or row.published_at)
        await _commit(session)


async def _publish(db, receipt, revision, guard, guarded, storage):
    async with _session(db) as session:
        await _publish_lock(session, receipt.logical_key)
        row = (await _owners(session, [receipt.id])).get(receipt.id)
        _current(row, receipt, "pending")
        await _gate(session, receipt, revision)
        await _require_guard(session, guard)
        winner = await session.scalar(
            select(Object).where(
                Object.logical_key == receipt.logical_key, Object.state == "ready"
            )
        )
        if winner is None:
            row.state = "ready"
            row.published_at = row.last_used_at = max(now(), row.created_at)
            await _commit(session)
            return receipt, guard
        winner_receipt = physical.snapshot(winner)
        _matching_facts(winner, {name: getattr(receipt, name) for name in _CANONICAL})
        winner_revision, winner_route = await _read_plan(session, winner_receipt)
        await _commit(session)

    # Never keep a publication or owner lock while verifying the competing owner.
    await _io(
        db, winner_receipt, winner_revision, "read", storage, read_route=winner_route
    )
    async with _session(db) as session:
        await _publish_lock(session, receipt.logical_key)
        owners = await _owners(session, [receipt.id, winner_receipt.id])
        row, winner = owners.get(receipt.id), owners.get(winner_receipt.id)
        _current(row, receipt, "pending")
        _current(winner, winner_receipt, "ready")
        await _gate(session, receipt, revision)
        await _read_gate(session, winner_receipt, winner_revision, winner_route)
        if guard is not None:
            deleted = await session.execute(
                delete(Publication).where(
                    Publication.id == guard.id,
                    Publication.object_id == receipt.id,
                    Publication.gate_revision == revision,
                )
            )
            if deleted.rowcount != 1:
                _reject()
        replacement = await _add_guard(
            session, winner_receipt, winner_revision, guarded, read_route=winner_route
        )
        row.state = "retired"
        row.retired_at = max(now(), row.created_at)
        winner.last_used_at = max(now(), winner.last_used_at or winner.published_at)
        await _commit(session)
        return winner_receipt, replacement


async def store(reference, encoded_data, *, db=None, storage=storage_service):
    """Publish exact content without committing or waiting on business locks."""
    facts = physical.content(reference, encoded_data)
    async with _context(db) as context:
        receipt, revision, guard, candidate, read_route = await _initial(
            context, facts, db is not None
        )
        if candidate:
            recorder = accounting.recorder(
                context,
                receipt,
                producer="raster_mask_objects",
                purpose="other_platform",
            )
            permits, failure = [], None
            try:
                await _io(
                    context,
                    receipt,
                    revision,
                    "write",
                    storage,
                    encoded_data,
                    state="pending",
                    accounting=recorder,
                    permits=permits,
                )
                receipt, guard = await _publish(
                    context, receipt, revision, guard, db is not None, storage
                )
            except BaseException as exc:
                failure = exc
            failure = await accounting.finish(
                recorder,
                failure,
                settled=bool(permits)
                and all(
                    getattr(permit, "_settled", False) is True for permit in permits
                ),
            )
            if failure is not None:
                raise failure
        else:
            await _io(
                context, receipt, revision, "read", storage, read_route=read_route
            )
            await _touch(context, receipt, revision, guard, read_route=read_route)
        _remember(db, guard)
        return physical.reference(receipt)


async def load(reference, *, db=None, storage=storage_service):
    """Resolve only a current ready owner; unknown legacy keys perform zero I/O."""
    key = reference.get("object_key") if isinstance(reference, dict) else None
    if not isinstance(key, str) or len(key) > 1024:
        _reject("raster_object_storage_unpinned")
    async with _context(db) as context:
        async with _session(context) as session:
            row = await session.scalar(
                select(Object).where(Object.logical_key == key, Object.state == "ready")
            )
            if row is None:
                _reject("raster_object_storage_unpinned")
            receipt = physical.snapshot(row)
            _matching_reference(reference, receipt)
            revision, read_route = await _read_plan(session, receipt)
            await _commit(session)
        result = await _io(
            context, receipt, revision, "read", storage, read_route=read_route
        )
        async with _session(context) as session:
            # A business caller may already hold a SHARE owner lock from a prior
            # binding in the same transaction. Reads never upgrade that lock.
            row = (await _owners(session, [receipt.id], read=True)).get(receipt.id)
            _current(row, receipt, "ready")
            await _read_gate(session, receipt, revision, read_route)
            await _commit(session)
        return result


async def bind_publications(db, value):
    """Delete only this caller's guards, atomically with its final references.

    Keep the in-memory UUIDs even after DELETE. A later rollback restores the
    database rows; an acknowledged commit makes their absence safe to observe.
    Progress commits have no effect on either the rows or this tracking map.
    """
    from app.services.raster_mask_storage import collect_mask_references

    references = collect_mask_references(value)
    tracked = db.info.get(PUBLICATIONS_INFO, {})
    selected = {}
    for reference in references:
        key = reference.get("object_key")
        for identifier, guard in tracked.get(key, {}).items():
            _matching_reference(reference, guard.receipt)
            selected[identifier] = guard
    if not selected:
        return 0
    with db.no_autoflush:
        present = list(
            (
                await db.scalars(
                    select(Publication)
                    .where(Publication.id.in_(selected))
                    .order_by(Publication.id)
                    .with_for_update(nowait=True)
                )
            ).all()
        )
        owners = {
            row.id: row
            for row in (
                await db.scalars(
                    select(Object)
                    .where(Object.id.in_([row.object_id for row in present]))
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
        # Until the business transaction commits, its uncommitted guard DELETE
        # remains visible to retirement. Do not retain owner SHARE locks here:
        # a later store in this batch needs an independent UPDATE on that owner.
        for row in present:
            guard = selected[row.id]
            if row.object_id != guard.receipt.id or row.gate_revision != guard.revision:
                _reject()
            _current(owners.get(row.object_id), guard.receipt, "ready")
            await _read_gate(db, guard.receipt, guard.revision, guard.read_route)
        if present:
            await db.execute(
                delete(Publication).where(
                    Publication.id.in_([row.id for row in present])
                )
            )
    return len(present)


async def _expired_reservations(db, cutoff):
    async with _session(db) as session:
        removed = await session.execute(
            delete(RasterMaskUpload).where(
                RasterMaskUpload.linked_at.is_(None),
                RasterMaskUpload.created_at <= cutoff,
            )
        )
        await _commit(session)
        return removed.rowcount


async def _referenced_keys(db):
    from app.services.raster_mask_references import referenced_keys

    return await referenced_keys(db)


async def _is_referenced(db, key):
    from app.services.raster_mask_references import is_referenced

    return await is_referenced(db, key)


async def _guarded(db, receipt, *, exclude_operation=None):
    if await db.scalar(select(exists().where(Publication.object_id == receipt.id))):
        return True
    conditions = [
        or_(
            StorageOperation.purpose == purpose("read", receipt.id),
            (StorageOperation.location_id == _location(receipt))
            & StorageOperation.purpose.in_(
                [purpose(operation, receipt.id) for operation in ("write", "delete")]
            ),
        )
    ]
    if exclude_operation is not None:
        conditions.append(StorageOperation.id != exclude_operation)
    return bool(await db.scalar(select(exists().where(*conditions))))


async def _business_retains(db, receipt, state):
    if not await _is_referenced(db, receipt.logical_key):
        return False
    if state != "retired":
        return True
    winner = await db.scalar(
        select(Object).where(
            Object.logical_key == receipt.logical_key,
            Object.state == "ready",
            Object.id != receipt.id,
        )
    )
    if winner is None:
        return True
    # A newer generation may carry the same portable business reference. Its
    # physical UUID is distinct; deleting this retired UUID cannot affect it.
    physical.resolve(winner)
    return any(getattr(winner, name) != getattr(receipt, name) for name in _CANONICAL)


def _aged(row, cutoff):
    return row.state == "retired" or (
        row.state == "ready" and (row.last_used_at or row.published_at) <= cutoff
    )


async def _gc_owner(db, receipt, cutoff, *, excluded=None):
    if not await _content_try_lock(db, receipt.logical_key):
        return None
    await _publish_lock(db, receipt.logical_key)
    row = (await _owners(db, [receipt.id])).get(receipt.id)
    if (
        row is None
        or not physical.same(row, receipt)
        or not _aged(row, cutoff)
        or await _guarded(db, receipt, exclude_operation=excluded)
        or await _business_retains(db, receipt, row.state)
    ):
        return None
    return row


async def _gc_one(db, receipt, cutoff, storage):
    async with _session(db) as session:
        row = await _gc_owner(session, receipt, cutoff)
        if row is None:
            return "retained"
        revision = await _gate(session, receipt)
        if row.state == "ready":
            row.state = "retired"
            row.retired_at = max(now(), row.last_used_at or row.published_at)
            await _commit(session)
            return "retired"
        await _commit(session)

    target = None

    async def acquired(permit):
        nonlocal target
        async with _session(db) as session:
            row = await _gc_owner(session, receipt, cutoff, excluded=permit.id)
            if row is None or row.state != "retired":
                _reject()
            await _gate(session, receipt, revision)
            _attempt(row)
            target = await physical.io_target(session, receipt)
            await _commit(session)

    await storage_access.run(
        db,
        _location(receipt),
        purpose("delete", receipt.id),
        lambda: physical.run_io(target, storage.client, "delete", receipt),
        expected_revision=revision,
        on_acquired=acquired,
    )
    async with _session(db) as session:
        row = await _gc_owner(session, receipt, cutoff)
        if row is None or row.state != "retired":
            _reject()
        await _gate(session, receipt, revision)
        await session.delete(row)
        await _commit(session)
    return "deleted"


def _attempt(row):
    current = now()
    row.last_attempted_at = (
        max(current, row.last_attempted_at + timedelta(microseconds=1))
        if row.last_attempted_at is not None
        else current
    )


async def cleanup(db, *, dry_run=False, limit=1000, storage=storage_service):
    """Retire first, delete on a later run; never infer ownership from a prefix."""
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("Invalid raster object cleanup limit")
    result = {
        "dry_run": dry_run,
        "referenced": 0,
        "scanned": 0,
        "eligible": 0,
        "deleted": 0,
        "errors": 0,
        "retired": 0,
        "retained": 0,
        "expired_uploads": 0,
    }
    cutoff = now() - GRACE
    if not dry_run:
        try:
            result["expired_uploads"] = await _expired_reservations(db, cutoff)
        except Exception:
            result["errors"] += 1
    async with _session(db) as session:
        keys = await _referenced_keys(session)
        result["referenced"] = len(keys)
        query = (
            select(Object)
            .where(
                or_(
                    Object.state == "retired",
                    (Object.state == "ready")
                    & (
                        func.coalesce(Object.last_used_at, Object.published_at)
                        <= cutoff
                    ),
                )
            )
            .order_by(
                Object.last_attempted_at.asc().nulls_first(),
                Object.created_at,
                Object.id,
            )
            .limit(limit)
        )
        if not dry_run:
            # Scan bookkeeping takes no content/publication/business locks and
            # skips contended owners. Commit attempts for retained rows too,
            # before starting the ordered lifetime protocol or physical I/O.
            query = query.with_for_update(skip_locked=True)
        rows = list((await session.scalars(query)).all())
        candidates = []
        for row in rows:
            result["scanned"] += 1
            if not dry_run:
                _attempt(row)
            try:
                receipt = physical.snapshot(row)
                if (
                    not _aged(row, cutoff)
                    or (
                        row.logical_key in keys
                        and await _business_retains(session, receipt, row.state)
                    )
                    or await _guarded(session, receipt)
                ):
                    result["retained"] += 1
                    continue
                candidates.append(receipt)
                result["eligible"] += 1
            except (ValueError, TypeError):
                result["errors"] += 1
        if not dry_run:
            await _commit(session)
    if dry_run:
        return result
    for receipt in candidates:
        try:
            outcome = await _gc_one(db, receipt, cutoff, storage)
            result[outcome] += 1
        except Exception:
            # Keep the owner, guard and unresolved operation as independent facts.
            result["errors"] += 1
    return result
