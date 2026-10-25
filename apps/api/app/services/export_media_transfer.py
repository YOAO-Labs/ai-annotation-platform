"""Transfer one frozen original while retaining physical ownership to real close.

Use the reader's synchronous capture body, not its async cancellation wrapper:
this owner must keep the actual executor future visible through every ordinary
soft limit and BaseException. Authority and complete input checks stay with the
caller; each callback receives a fresh session which closes before physical I/O.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
import threading
import time
from types import SimpleNamespace
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services import export_media_bundles as bundles
from app.services import export_media_storage as physical
from app.services import managed_original_reader as reader, original_sources
from app.services import media_cache_accounting
from app.services import storage_access
from app.services.content_snapshot import ContentError

COPY_SECONDS = 300
_slots = threading.BoundedSemaphore(2)


def reject(code="export_media_source_changed", status=409):
    raise ContentError(code, status)


@dataclass(frozen=True)
class TransferReceipt:
    original_sha: str
    size: int
    gate_revision: int
    url: str = field(repr=False)
    _inspection: object = field(default=None, repr=False)

    @property
    def inspection(self):
        return deepcopy(self._inspection)


def purpose(snapshot):
    return "export_media_" + uuid.UUID(str(snapshot.id)).hex


async def drain(pending):
    """A second soft limit cannot turn a still-running operation into a result."""
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except BaseException:
            continue
    return pending.result()


def _uncertainty(original, later):
    if getattr(later, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(original)


async def _async(call, *, late=None):
    """Drain a known coroutine; clean a late allocation before rethrowing interruption."""
    pending = asyncio.create_task(call)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            result = await drain(pending)
        except BaseException as later:
            _uncertainty(interrupted, later)
        else:
            if late is not None:
                try:
                    await late(result)
                except BaseException as closing:
                    _uncertainty(interrupted, closing)
        raise


async def _close_async(call):
    """Distinguish an interrupted wait from an actually failed asynchronous close."""
    try:
        pending = asyncio.create_task(call)
    except BaseException as exc:
        call.close()
        raise storage_access.mark_unsettled(exc)
    try:
        await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await drain(pending)
        except BaseException:
            storage_access.mark_unsettled(interrupted)
        raise


def _submit(call, *args, **kwargs):
    try:
        return asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)


async def physical_work(call, *args, **kwargs):
    pending = _submit(call, *args, **kwargs)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await drain(pending)
        except BaseException as later:
            _uncertainty(interrupted, later)
        raise


class Admission:
    """The reader requests release; only the fully drained owner releases the slot."""

    def __init__(self):
        self.retained, self.released, self.requested = False, False, False
        self.lock = threading.Lock()
        if not _slots.acquire(blocking=False):
            reject("snapshot_capacity_exceeded", 429)

    def release(self):
        with self.lock:
            self.requested = True

    def finish(self, unsettled):
        with self.lock:
            self.retained = self.retained or unsettled
            if not self.retained and not self.released:
                self.released = True
                _slots.release()


@asynccontextmanager
async def _session(db):
    owned = AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    )
    failure = None
    try:
        yield owned
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await _close_async(owned.close())
        except BaseException as closing:
            if failure is not None:
                _uncertainty(closing, failure)
            raise


def _spec(source, *, maximum=physical.MAX_OBJECT_BYTES):
    if (
        not isinstance(source.location_id, uuid.UUID)
        or type(source.maintenance_revision) is not int
        or source.maintenance_revision < 1
        or type(source.size_bytes) is not int
        or not 0 <= source.size_bytes <= maximum
    ):
        reject()
    return reader.ObjectSpec(
        source.endpoint_url,
        source.bucket,
        source.object_key,
        source.sha256,
        source.size_bytes,
    )


def _object_record(snapshot, relative):
    # Copy one prevalidated entry, never the entire inventory for every object.
    inventory = getattr(snapshot, "_inventory", None)
    if inventory is None:
        inventory = snapshot.inventory
    record = SimpleNamespace(
        id=snapshot.id,
        storage_ref=snapshot.storage_ref,
        created_at=snapshot.created_at,
        expires_at=snapshot.expires_at,
        input_schema=physical.input_schema(snapshot),
    )
    owner = physical.resolve(record)
    key = owner.key(relative)
    if (
        not isinstance(inventory, dict)
        or set(inventory) != {"schema", "objects"}
        or type(inventory["schema"]) is not int
        or inventory["schema"] != 1
        or not isinstance(inventory["objects"], dict)
        or key not in inventory["objects"]
    ):
        reject()
    record.inventory = {
        "schema": 1,
        "objects": {key: deepcopy(inventory["objects"][key])},
    }
    expected = physical.validate_inventory(record)[key]
    return record, owner, expected


async def _fresh(
    db,
    snapshot,
    binding,
    resolve_source,
    guard,
    *,
    expected=None,
    output_revision=None,
    output_location_id=None,
):
    async with _session(db) as owned:
        await guard(owned)
        await bundles.check_claim(owned, snapshot)
        current = await resolve_source(owned, binding)
        _spec(current, maximum=physical.object_limit(snapshot))
        if expected is not None and current != expected:
            reject()
        await storage_access.check(
            owned, current.location_id, expected_revision=current.maintenance_revision
        )
        if output_revision is not None:
            await storage_access.check(
                owned,
                output_location_id or uuid.UUID(snapshot.storage_ref["location_id"]),
                expected_revision=output_revision,
            )
        return current


async def _io_target(db, record):
    if physical.resolve(record).ref["schema"] == 1:
        return None
    async with _session(db) as owned:
        return await physical.io_target(owned, record)


async def _settle(permit):
    await _close_async(permit._settle())


async def _acquire(db, snapshot, location_id, *, expected_revision=None):
    async def acquire():
        async with _session(db) as owned:
            return await storage_access.acquire(
                owned,
                location_id,
                purpose(snapshot),
                expected_revision=expected_revision,
            )

    return await _async(acquire(), late=_settle)


async def _capture(
    source, admission, *, maximum=physical.MAX_OBJECT_BYTES, client_context=None
):
    stop = threading.Event()
    pending = _submit(
        reader._capture,
        _spec(source, maximum=maximum),
        maximum,
        COPY_SECONDS,
        time.monotonic() + COPY_SECONDS,
        stop,
        admission.release,
        transport_timeout_seconds=30,
        retain_on_close_error=True,
        **({"client_context": client_context} if client_context is not None else {}),
    )
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        stop.set()
        try:
            stage = await drain(pending)
        except BaseException as later:
            _uncertainty(interrupted, later)
        else:
            try:
                await physical_work(stage.close)
            except BaseException as closing:
                _uncertainty(interrupted, closing)
        raise


async def copy_source(
    db, snapshot, *, binding, relative, resolve_source, guard, inspect=None
):
    """Copy one original; never return delivery while authority or close is unknown."""
    if (
        not callable(resolve_source)
        or not callable(guard)
        or (inspect is not None and not callable(inspect))
    ):
        reject("export_media_callbacks_required", 422)
    record, owner, expected = _object_record(snapshot, relative)
    source = await _async(_fresh(db, snapshot, binding, resolve_source, guard))
    if expected != {"sha256": source.sha256, "size_bytes": source.size_bytes}:
        reject()
    permits, admission, stage, failure, result = [], None, None, None, None
    recorder = None
    try:
        permits.append(
            await _acquire(
                db,
                snapshot,
                source.location_id,
                expected_revision=source.maintenance_revision,
            )
        )
        await _async(
            _fresh(db, snapshot, binding, resolve_source, guard, expected=source)
        )
        admission = Admission()
        read_options = await _async(original_sources.read_options(db, source))
        stage = await _capture(
            source, admission, maximum=physical.object_limit(record), **read_options
        )
        if stage.sha256 != source.sha256 or stage.size != source.size_bytes:
            reject()
        await _async(
            _fresh(db, snapshot, binding, resolve_source, guard, expected=source)
        )
        inspection = (
            None if inspect is None else await physical_work(inspect, stage.file)
        )
        await _async(
            _fresh(db, snapshot, binding, resolve_source, guard, expected=source)
        )
        permit = await _acquire(db, snapshot, uuid.UUID(owner.ref["location_id"]))
        permits.append(permit)
        revision = permit.revision
        await _async(
            _fresh(
                db,
                snapshot,
                binding,
                resolve_source,
                guard,
                expected=source,
                output_revision=revision,
                output_location_id=uuid.UUID(owner.ref["location_id"]),
            )
        )

        target = await _async(_io_target(db, record))
        recorder = media_cache_accounting.Recorder(
            async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
            "export_media_outputs",
            record.id,
            owner.ref["endpoint_url"],
            owner.bucket,
            purpose="temporary",
        )

        def write():
            with physical.open_storage(
                record,
                **({"target": target} if target is not None else {}),
                single_attempt=True,
            ) as client:
                physical.write_object(
                    client,
                    record,
                    relative,
                    stage.file.fileno(),
                    accounting=recorder,
                )
                return physical.signed_url(client, record, relative)

        url = await physical_work(write)
        await _async(
            _fresh(
                db,
                snapshot,
                binding,
                resolve_source,
                guard,
                expected=source,
                output_revision=revision,
                output_location_id=uuid.UUID(owner.ref["location_id"]),
            )
        )
        result = TransferReceipt(
            source.sha256, source.size_bytes, revision, url, deepcopy(inspection)
        )
    except BaseException as exc:
        failure = exc
    finally:
        if stage is not None:
            try:
                await physical_work(stage.close)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    _uncertainty(failure, closing)
        if failure is None:
            try:
                await _async(
                    _fresh(
                        db,
                        snapshot,
                        binding,
                        resolve_source,
                        guard,
                        expected=source,
                        output_revision=result.gate_revision,
                        output_location_id=uuid.UUID(owner.ref["location_id"]),
                    )
                )
            except BaseException as exc:
                failure = exc
        if not getattr(failure, "storage_operation_unsettled", False):
            for permit in reversed(permits):
                try:
                    await _settle(permit)
                except BaseException as closing:
                    if failure is None:
                        failure = closing
                    else:
                        _uncertainty(failure, closing)
                    # A known late success may continue closing the remaining permits.
                    if getattr(failure, "storage_operation_unsettled", False):
                        break
        if admission is not None:
            admission.finish(getattr(failure, "storage_operation_unsettled", False))
        if recorder is not None:
            try:
                await recorder.finish(
                    settled=not getattr(failure, "storage_operation_unsettled", False)
                )
            except BaseException as publishing:
                if failure is None:
                    failure = publishing
                else:
                    _uncertainty(failure, publishing)
    if failure is not None:
        raise failure
    return result


async def _fresh_copy(db, snapshot, guard, revision=None):
    async with _session(db) as owned:
        await guard(owned)
        await bundles.check_claim(owned, snapshot)
        if revision is not None:
            await storage_access.check(
                owned,
                uuid.UUID(snapshot.storage_ref["location_id"]),
                expected_revision=revision,
            )


async def read_copy(db, snapshot, *, relative, guard, read):
    """Read a saved building copy into a callback, retaining ownership to real close."""
    if not callable(guard) or not callable(read):
        reject("export_media_callbacks_required", 422)
    record, owner, expected = _object_record(snapshot, relative)
    await _async(_fresh_copy(db, snapshot, guard))
    permit, admission, stage, failure, result = None, None, None, None, None
    try:
        permit = await _acquire(db, snapshot, uuid.UUID(owner.ref["location_id"]))
        await _async(_fresh_copy(db, snapshot, guard, permit.revision))
        source = SimpleNamespace(
            endpoint_url=owner.ref["endpoint_url"],
            bucket=owner.bucket,
            object_key=owner.key(relative),
            sha256=expected["sha256"],
            size_bytes=expected["size_bytes"],
            location_id=permit.location_id,
            maintenance_revision=permit.revision,
        )
        admission = Admission()
        target = await _async(_io_target(db, record))
        context = (
            (lambda: physical.open_storage(record, target=target))
            if target is not None
            else None
        )
        stage = await _capture(
            source,
            admission,
            maximum=physical.object_limit(record),
            **({"client_context": context} if context is not None else {}),
        )
        if stage.sha256 != source.sha256 or stage.size != source.size_bytes:
            reject()
        await _async(_fresh_copy(db, snapshot, guard, permit.revision))
        result = await physical_work(read, stage.file)
        await _async(_fresh_copy(db, snapshot, guard, permit.revision))
    except BaseException as exc:
        failure = exc
    finally:
        if stage is not None:
            try:
                await physical_work(stage.close)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    _uncertainty(failure, closing)
        if failure is None:
            try:
                await _async(_fresh_copy(db, snapshot, guard, permit.revision))
            except BaseException as exc:
                failure = exc
        if permit is not None and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            try:
                await _settle(permit)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    _uncertainty(failure, closing)
        if admission is not None:
            admission.finish(getattr(failure, "storage_operation_unsettled", False))
    if failure is not None:
        raise failure
    return result
