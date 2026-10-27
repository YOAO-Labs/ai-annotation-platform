"""Independent durable admission; a receipt survives until physical I/O settles."""

import asyncio
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
import logging
import os
import re
import socket
import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)

logger = logging.getLogger(__name__)
SAFE_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")
SAFE_NODE = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")


def error(code, status=409):
    # content_snapshot also uses this owner, so import the shared error lazily.
    from app.services.content_snapshot import ContentError

    return ContentError(code, status)


def mark_unsettled(exc):
    """Preserve uncertainty when a physical constructor or close did not settle."""
    exc.storage_operation_unsettled = True
    return exc


def close_registered(resource):
    """A failed close is not proof that a registered physical handle ended."""
    try:
        resource.close()
    except BaseException as exc:
        raise mark_unsettled(exc)


@contextmanager
def closing_registered(resource):
    """Close a physical handle without losing evidence of uncertain closure."""
    try:
        yield resource
    finally:
        close_registered(resource)


def engine(db):
    bind = db.bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    if isinstance(bind, AsyncEngine):
        return bind
    raise TypeError("Storage admission requires an asynchronous engine or connection")


def unavailable(exc):
    original = exc.orig
    return getattr(original, "sqlstate", None) in {"55P03", "40P01"} or getattr(
        original, "pgcode", None
    ) in {"55P03", "40P01"}


async def ensure(db, location_id):
    """Commit initialization independently, never in the business transaction."""
    async with engine(db).begin() as connection:
        # Avoid ON CONFLICT touching an existing row held by the caller itself.
        if await connection.scalar(
            select(Gate.location_id).where(Gate.location_id == location_id)
        ):
            return
        try:
            await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
            await connection.execute(
                insert(Gate).values(location_id=location_id).on_conflict_do_nothing()
            )
        except DBAPIError as exc:
            if unavailable(exc):
                raise error("storage_maintenance_busy") from None
            raise


async def state(db, location_id):
    row = (
        (
            await db.execute(
                select(
                    Gate.state, Gate.revision, Gate.changed_at, Gate.changed_by
                ).where(Gate.location_id == location_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    return (
        dict(row)
        if row
        else {"state": "active", "revision": 1, "changed_at": None, "changed_by": None}
    )


def validate(current, expected_revision):
    if expected_revision is not None and current["revision"] != expected_revision:
        raise error("storage_maintenance_changed")
    if current["state"] != "active":
        raise error("storage_maintenance")
    return current["revision"]


async def check(db, location_id, *, expected_revision=None, lock=False):
    if not lock:
        return validate(await state(db, location_id), expected_revision)
    await ensure(db, location_id)
    try:
        async with db.begin_nested():
            row = (
                (
                    await db.execute(
                        select(Gate.state, Gate.revision)
                        .where(Gate.location_id == location_id)
                        .with_for_update(read=True, nowait=True)
                    )
                )
                .mappings()
                .one()
            )
    except DBAPIError as exc:
        if unavailable(exc):
            raise error("storage_maintenance_busy") from None
        raise
    return validate(row, expected_revision)


async def finish(task):
    """Repeated cancellation cannot abandon a thread or ledger transaction."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


@dataclass
class Permit:
    bind: AsyncEngine
    id: uuid.UUID
    location_id: uuid.UUID
    revision: int
    _settled: bool = False

    async def __aenter__(self):
        return self

    async def _settle(self):
        async with self.bind.begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.id == self.id,
                    StorageOperation.location_id == self.location_id,
                )
            )
        self._settled = True

    async def __aexit__(self, exc_type, exc, traceback):
        if getattr(exc, "storage_operation_unsettled", False):
            return False
        if not self._settled:
            task = asyncio.create_task(self._settle())
            cancelled = False
            try:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                    await finish(task)
            except Exception:
                # An unresolved record must remain visible; do not print paths,
                # exception payloads, connection strings, or driver credentials.
                logger.error(
                    "Storage operation settlement failed: location=%s operation=%s",
                    self.location_id,
                    self.id,
                )
                if exc_type is None:
                    raise error("storage_operation_unsettled", 503) from None
            if cancelled and exc_type is None:
                raise asyncio.CancelledError()
        return False


async def acquire(db, location_id, purpose, *, expected_revision=None, node_id=None):
    if not SAFE_LABEL.fullmatch(purpose):
        raise ValueError("Invalid storage operation purpose")
    node = node_id or f"{socket.gethostname()}:{os.getpid()}"
    if not SAFE_NODE.fullmatch(node):
        raise ValueError("Invalid storage operation node")
    await ensure(db, location_id)
    operation_id = uuid.uuid4()
    try:
        async with engine(db).begin() as connection:
            current = (
                (
                    await connection.execute(
                        select(Gate.state, Gate.revision)
                        .where(Gate.location_id == location_id)
                        .with_for_update(read=True, nowait=True)
                    )
                )
                .mappings()
                .one()
            )
            revision = validate(current, expected_revision)
            await connection.execute(
                insert(StorageOperation).values(
                    id=operation_id,
                    location_id=location_id,
                    revision=revision,
                    purpose=purpose,
                    node_id=node,
                )
            )
    except DBAPIError as exc:
        if unavailable(exc):
            raise error("storage_maintenance_busy") from None
        raise
    return Permit(engine(db), operation_id, location_id, revision)


async def run(
    db,
    location_id,
    purpose,
    call,
    *args,
    expected_revision=None,
    node_id=None,
    timeout=None,
    on_acquired=None,
    **kwargs,
):
    """Persist optional owner evidence before starting registered executor I/O."""
    async with await acquire(
        db, location_id, purpose, expected_revision=expected_revision, node_id=node_id
    ) as permit:
        if on_acquired is not None:
            await on_acquired(permit)
        future = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
        try:
            if timeout is None:
                return await asyncio.shield(future)
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except (asyncio.CancelledError, TimeoutError) as interrupted:
            # Cancellation of an await is not evidence the physical handle closed.
            try:
                await finish(future)
            except BaseException as late:
                if getattr(late, "storage_operation_unsettled", False):
                    mark_unsettled(interrupted)
            raise
