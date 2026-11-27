"""Independent durable admission; a receipt survives until physical I/O settles."""

import asyncio
import logging
import os
import re
import socket
import uuid
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.services.storage_io_settlement import (
    failure_unsettled as failure_unsettled,
)
from app.services.storage_io_settlement import (
    finish as finish,
)
from app.services.storage_io_settlement import (
    mark_unsettled as mark_unsettled,
)

logger = logging.getLogger(__name__)
SAFE_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")
SAFE_NODE = re.compile(r"[A-Za-z0-9_.:@-]{1,128}")

_INITIALIZE_GATE = text("SELECT yoao_assets_storage_v1.initialize_gate(:location)")
_GATE_FACTS = text("SELECT * FROM yoao_assets_storage_v1.gate_facts(:location, :mode)")
_BEGIN_IO = text(
    "SELECT yoao_assets_storage_v1.begin_physical_io"
    "(:operation, :location, :purpose, :node, :revision)"
)
_SETTLE_IO = text(
    "SELECT yoao_assets_storage_v1.settle_physical_io(:operation, :location, :revision)"
)
_IO_OPEN = text(
    "SELECT yoao_assets_storage_v1.physical_io_is_open(:purpose, :location, :excluding)"
)
_IO_RECEIPT = text(
    "SELECT * FROM yoao_assets_storage_v1.physical_io_receipt(:operation)"
)


def error(code, status=409):
    from app.services.content_errors import ContentError

    return ContentError(code, status)


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


def _sqlstate(exc):
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


def unavailable(exc):
    return _sqlstate(exc) in {"55P03", "40P01"}


async def ensure(db, location_id):
    """Commit initialization independently, never in the business transaction."""
    try:
        async with engine(db).begin() as connection:
            await connection.execute(_INITIALIZE_GATE, {"location": location_id})
    except DBAPIError as exc:
        if unavailable(exc):
            raise error("storage_maintenance_busy") from None
        raise


async def gate_facts(db, location_id, *, lock=None):
    result = await db.execute(_GATE_FACTS, {"location": location_id, "mode": lock})
    return result.mappings().one_or_none()


async def operation_is_open(db, purpose, *, location_id=None, excluding=None):
    return bool(
        await db.scalar(
            _IO_OPEN,
            {"purpose": purpose, "location": location_id, "excluding": excluding},
        )
    )


async def operation_receipt(db, operation_id):
    result = await db.execute(_IO_RECEIPT, {"operation": operation_id})
    return result.mappings().one_or_none()


async def state(db, location_id):
    row = await gate_facts(db, location_id)
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
            row = await gate_facts(db, location_id, lock="share_nowait")
    except DBAPIError as exc:
        if unavailable(exc):
            raise error("storage_maintenance_busy") from None
        raise
    return validate(row, expected_revision)


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
            settled = await connection.scalar(
                _SETTLE_IO,
                {
                    "operation": self.id,
                    "location": self.location_id,
                    "revision": self.revision,
                },
            )
            if not settled:
                raise mark_unsettled(error("storage_operation_unsettled", 503))
        self._settled = True

    async def __aexit__(self, exc_type, exc, traceback):
        if failure_unsettled(exc):
            return False
        if not self._settled:
            task = asyncio.create_task(self._settle())
            cancelled = None
            try:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as interrupted:
                    cancelled = interrupted
                    await finish(task)
            except BaseException:
                logger.error(
                    "Storage operation settlement failed: location=%s operation=%s",
                    self.location_id,
                    self.id,
                )
                unsettled = mark_unsettled(error("storage_operation_unsettled", 503))
                if exc is not None:
                    raise BaseExceptionGroup(
                        "Storage operation and permit settlement failed",
                        [exc, unsettled],
                    ) from None
                raise unsettled from None
            if cancelled is not None:
                if exc is not None:
                    if isinstance(exc, asyncio.CancelledError):
                        return False
                    raise BaseExceptionGroup(
                        "Storage operation and permit settlement were interrupted",
                        [exc, cancelled],
                    ) from None
                raise cancelled
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
            revision = await connection.scalar(
                _BEGIN_IO,
                {
                    "operation": operation_id,
                    "location": location_id,
                    "purpose": purpose,
                    "node": node,
                    "revision": expected_revision,
                },
            )
    except DBAPIError as exc:
        if unavailable(exc):
            raise error("storage_maintenance_busy") from None
        if _sqlstate(exc) == "YS001":
            raise error("storage_maintenance_changed") from None
        if _sqlstate(exc) == "YS002":
            raise error("storage_maintenance") from None
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
    from app.services import export_execution_context

    owner = export_execution_context.current_optional()
    permit = await acquire(
        db, location_id, purpose, expected_revision=expected_revision, node_id=node_id
    )
    entered, future, failure = False, None, None
    try:
        async with permit:
            if on_acquired is not None:
                await on_acquired(permit)
            if owner is not None:
                await owner.enter_io()
                entered = True
            try:
                future = asyncio.get_running_loop().run_in_executor(
                    None, copy_context().run, partial(call, *args, **kwargs)
                )
            except BaseException as exc:
                if owner is not None:
                    raise mark_unsettled(exc)
                raise
            try:
                if timeout is None:
                    return await asyncio.shield(future)
                return await asyncio.wait_for(asyncio.shield(future), timeout)
            except (asyncio.CancelledError, TimeoutError) as interrupted:
                try:
                    await finish(future)
                except BaseException as late:
                    if owner is not None and late is not interrupted:
                        raise BaseExceptionGroup(
                            "Storage operation and drained physical work failed",
                            [interrupted, late],
                        ) from None
                    if failure_unsettled(late):
                        mark_unsettled(interrupted)
                raise
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if (
            entered
            and permit._settled
            and future is not None
            and future.done()
            and (failure is None or not failure_unsettled(failure))
        ):
            await export_execution_context.settle_io(failure)
