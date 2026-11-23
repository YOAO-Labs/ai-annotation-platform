"""Durable RLE owners, caller publication guards and exact-object retirement.

Business sessions retain their own transaction and lock order. Every allocation,
publication and physical-I/O receipt uses an independent short transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import raster_object_storage as physical, storage_access
from app.services import conditional_write_accounting as accounting
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.original_read_plan import ReadRoute
from app.services.raster_mask_references import is_referenced, referenced_keys
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
            combined = BaseExceptionGroup(
                "Raster owner operation and cancellation failed", [cancelled, exc]
            )
            if storage_access.failure_unsettled(exc):
                storage_access.mark_unsettled(combined)
            raise combined from None
        raise


@asynccontextmanager
async def _context(db):
    if db is not None:
        yield db
        return
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
    rows = await db.execute(
        text("SELECT * FROM yoao_labeling_v1.lock_raster_owners(:ids, :share)"),
        {"ids": sorted(set(identifiers)), "share": read},
    )
    return {row.id: row for row in rows}


async def ready_owner(db, key, *, lock=False):
    return (
        await db.execute(
            text("SELECT * FROM yoao_labeling_v1.ready_raster_owner(:key, :share)"),
            {"key": key, "share": lock},
        )
    ).one_or_none()


async def _lock_ready_write(db, identifier):
    if not await db.scalar(
        text("SELECT yoao_labeling_v1.lock_raster_ready_for_publish(:id)"),
        {"id": identifier},
    ):
        _reject()


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
    await db.execute(
        text(
            "SELECT yoao_labeling_v1.open_raster_publication(:id, :owner, :revision, :now)"
        ),
        {"id": guard.id, "owner": receipt.id, "revision": revision, "now": now()},
    )
    return guard


async def _require_guard(db, guard):
    if guard is not None and not await db.scalar(
        text(
            "SELECT yoao_labeling_v1.raster_publication_matches(:id, :owner, :revision)"
        ),
        {"id": guard.id, "owner": guard.receipt.id, "revision": guard.revision},
    ):
        _reject()


async def _bind_prediction_producer(db, object_id):
    from app.application.prediction_mask_producers import (
        current_prediction_mask_producer,
    )

    producer_id = current_prediction_mask_producer()
    if producer_id is not None:
        admitted = await db.scalar(
            text("SELECT yoao_jobs_v1.bind_prediction_mask_object(:producer, :object)"),
            {"producer": producer_id, "object": object_id},
        )
        if admitted is not True:
            _reject("prediction_mask_producer_not_current")


async def _initial(db, facts, guarded):
    async with _session(db) as session:
        await _publish_lock(session, facts["logical_key"])
        row = await ready_owner(session, facts["logical_key"], lock=True)
        candidate = row is None
        if candidate:
            row = SimpleNamespace(id=uuid.uuid4(), **facts, created_at=now())
            row.storage_ref = await physical.allocate(session, row)
        else:
            _matching_facts(row, facts)
        receipt = physical.snapshot(row)
        if guarded and not candidate:
            await _lock_ready_write(session, receipt.id)
        if candidate:
            revision, read_route = await _gate(session, receipt), None
        else:
            revision, read_route = await _read_plan(session, receipt)
        if candidate:
            await session.execute(
                text(
                    "SELECT yoao_labeling_v1.stage_raster_object("
                    ":id, :logical_key, :canonical_sha256, :canonical_size_bytes, "
                    ":height, :width, :runs, :storage_encoding, :stored_sha256, "
                    ":stored_size_bytes, CAST(:storage_ref AS jsonb), :created_at)"
                ),
                {**vars(row), "storage_ref": json.dumps(row.storage_ref)},
            )
        await _bind_prediction_producer(session, receipt.id)
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
    target = physical_permit = None

    async def acquired(permit):
        nonlocal target, physical_permit
        physical_permit = permit
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
            permit=physical_permit,
            **({"accounting": accounting} if accounting is not None else {}),
        ),
        expected_revision=revision,
        on_acquired=acquired,
    )


async def _touch(db, receipt, revision, guard=None, *, read_route=None):
    async with _session(db) as session:
        await _publish_lock(session, receipt.logical_key)
        row = (await _owners(session, [receipt.id], read=True)).get(receipt.id)
        _current(row, receipt, "ready")
        await _lock_ready_write(session, receipt.id)
        await _read_gate(session, receipt, revision, read_route)
        await _require_guard(session, guard)
        await _record_use(session, receipt)
        await _commit(session)


async def _record_use(db, receipt):
    if not await db.scalar(
        text(
            "SELECT yoao_labeling_v1.touch_ready_raster_object(:id, CAST(:ref AS jsonb), :now)"
        ),
        {"id": receipt.id, "ref": json.dumps(receipt.storage_ref), "now": now()},
    ):
        _reject()


async def _retire(db, receipt, state, *, cutoff=None):
    if not await db.scalar(
        text(
            "SELECT yoao_labeling_v1.retire_raster_object("
            ":id, :state, CAST(:ref AS jsonb), :now, :cutoff)"
        ),
        {
            "id": receipt.id,
            "state": state,
            "ref": json.dumps(receipt.storage_ref),
            "now": now(),
            "cutoff": cutoff,
        },
    ):
        _reject()


async def _publish(db, receipt, revision, guard, guarded, storage):
    async with _session(db) as session:
        await _publish_lock(session, receipt.logical_key)
        row = (await _owners(session, [receipt.id])).get(receipt.id)
        _current(row, receipt, "pending")
        await _gate(session, receipt, revision)
        await _require_guard(session, guard)
        winner = await ready_owner(session, receipt.logical_key)
        if winner is None:
            if not await session.scalar(
                text(
                    "SELECT yoao_labeling_v1.publish_raster_object("
                    ":id, CAST(:ref AS jsonb), :now)"
                ),
                {
                    "id": receipt.id,
                    "ref": json.dumps(receipt.storage_ref),
                    "now": now(),
                },
            ):
                _reject()
            await _commit(session)
            return receipt, guard
        winner_receipt = physical.snapshot(winner)
        await _bind_prediction_producer(session, winner_receipt.id)
        _matching_facts(winner, {name: getattr(receipt, name) for name in _CANONICAL})
        winner_revision, winner_route = await _read_plan(session, winner_receipt)
        await _commit(session)

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
            if not await session.scalar(
                text(
                    "SELECT yoao_labeling_v1.release_raster_publication(:id, :owner, :revision)"
                ),
                {"id": guard.id, "owner": receipt.id, "revision": revision},
            ):
                _reject()
        replacement = await _add_guard(
            session, winner_receipt, winner_revision, guarded, read_route=winner_route
        )
        await _retire(session, receipt, "pending")
        await _record_use(session, winner_receipt)
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
            row = await ready_owner(session, key)
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
        present = (
            await db.execute(
                text("SELECT * FROM yoao_labeling_v1.lock_raster_publications(:ids)"),
                {"ids": list(selected)},
            )
        ).all()
        for row in present:
            guard = selected[row.publication_id]
            if row.object_id != guard.receipt.id or row.gate_revision != guard.revision:
                _reject()
            _current(row, guard.receipt, "ready")
            await _read_gate(db, guard.receipt, guard.revision, guard.read_route)
        if present:
            consumed = await db.scalar(
                text(
                    "SELECT yoao_labeling_v1.consume_raster_publications("
                    ":ids, :objects, :revisions)"
                ),
                {
                    "ids": [row.publication_id for row in present],
                    "objects": [row.object_id for row in present],
                    "revisions": [row.gate_revision for row in present],
                },
            )
            if consumed != len(present):
                _reject()
    return len(present)


async def _expired_reservations(db, cutoff):
    async with _session(db) as session:
        removed = await session.scalar(
            text("SELECT yoao_labeling_v1.expire_unlinked_raster_uploads(:cutoff)"),
            {"cutoff": cutoff},
        )
        await _commit(session)
        return int(removed)


async def _guarded(db, receipt, *, exclude_operation=None):
    if await db.scalar(
        text("SELECT yoao_labeling_v1.raster_object_has_publication(:object)"),
        {"object": receipt.id},
    ):
        return True
    if await db.scalar(
        text("SELECT yoao_jobs_v1.prediction_mask_object_retained(:object)"),
        {"object": receipt.id},
    ):
        return True
    return bool(
        await db.scalar(
            text(
                "SELECT yoao_assets_storage_v1.physical_io_is_open(:read, NULL, :excluded) "
                "OR yoao_assets_storage_v1.physical_io_is_open(:write, :location, :excluded) "
                "OR yoao_assets_storage_v1.physical_io_is_open(:delete, :location, :excluded)"
            ),
            {
                "read": purpose("read", receipt.id),
                "write": purpose("write", receipt.id),
                "delete": purpose("delete", receipt.id),
                "location": _location(receipt),
                "excluded": exclude_operation,
            },
        )
    )


async def _business_retains(db, receipt, state):
    if not await is_referenced(db, receipt.logical_key):
        return False
    if state != "retired":
        return True
    winner = await ready_owner(db, receipt.logical_key)
    if winner is None or winner.id == receipt.id:
        return True
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
            await _retire(session, receipt, "ready", cutoff=cutoff)
            await _commit(session)
            return "retired"
        await _commit(session)

    target = physical_permit = None

    async def acquired(permit):
        nonlocal target, physical_permit
        physical_permit = permit
        async with _session(db) as session:
            row = await _gc_owner(session, receipt, cutoff, excluded=permit.id)
            if row is None or row.state != "retired":
                _reject()
            await _gate(session, receipt, revision)
            await _note_attempts(session, [row.id], [now()])
            target = await physical.io_target(session, receipt)
            await _commit(session)

    await storage_access.run(
        db,
        _location(receipt),
        purpose("delete", receipt.id),
        lambda: physical.run_io(
            target, storage.client, "delete", receipt, permit=physical_permit
        ),
        expected_revision=revision,
        on_acquired=acquired,
    )
    async with _session(db) as session:
        row = await _gc_owner(session, receipt, cutoff)
        if row is None or row.state != "retired":
            _reject()
        await _gate(session, receipt, revision)
        if not await session.scalar(
            text("SELECT yoao_labeling_v1.settle_raster_gc(:id, CAST(:ref AS jsonb))"),
            {"id": receipt.id, "ref": json.dumps(receipt.storage_ref)},
        ):
            _reject()
        await _commit(session)
    return "deleted"


async def _note_attempts(db, identifiers, timestamps):
    updated = await db.scalar(
        text("SELECT yoao_labeling_v1.note_raster_gc_attempts(:ids, :timestamps)"),
        {"ids": identifiers, "timestamps": timestamps},
    )
    if updated != len(identifiers):
        _reject()


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
        keys = await referenced_keys(session)
        result["referenced"] = len(keys)
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM yoao_labeling_v1.raster_physical_gc_candidates("
                    ":cutoff, :limit, :claim)"
                ),
                {"cutoff": cutoff, "limit": limit, "claim": not dry_run},
            )
        ).all()
        candidates = []
        attempted_ids, attempted_at = [], []
        for row in rows:
            result["scanned"] += 1
            if not dry_run:
                attempted_ids.append(row.id)
                attempted_at.append(now())
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
            if attempted_ids:
                await _note_attempts(session, attempted_ids, attempted_at)
            await _commit(session)
    if dry_run:
        return result
    for receipt in candidates:
        try:
            outcome = await _gc_one(db, receipt, cutoff, storage)
            result[outcome] += 1
        except Exception:
            result["errors"] += 1
    return result
