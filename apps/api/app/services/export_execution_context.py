"""Bind physical export I/O to the current independently owned execution."""

import asyncio
from contextvars import ContextVar, copy_context
from functools import partial
from pathlib import Path

from sqlalchemy.engine import make_url


def database_url(settings):
    direct = settings.export_execution_database_url
    file_path = settings.export_execution_database_url_file
    if direct and file_path:
        raise ValueError("Export execution database selectors conflict")
    if file_path:
        try:
            with Path(file_path).open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError("Export execution database credential is invalid")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError(
                "Export execution database credential is invalid"
            ) from None
    else:
        value = direct
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("Export execution database URL is invalid") from None
    if url.get_backend_name() != "postgresql" or not url.host:
        raise ValueError("Export execution database URL is invalid")
    return url.set(drivername="postgresql+asyncpg")


_current = ContextVar("export_execution", default=None)


def bind(execution):
    return _current.set(execution)


def reset(token):
    _current.reset(token)


def current():
    execution = _current.get()
    if execution is None:
        raise RuntimeError("Export work has no execution owner")
    return execution


def current_optional():
    return _current.get()


async def enter_io():
    await current().enter_io()


async def settle_io(primary=None):
    from app.services import storage_access

    try:
        await current().settle_io()
    except BaseException as closing:
        storage_access.mark_unsettled(closing)
        if primary is None:
            raise
        raise BaseExceptionGroup(
            "Export operation and I/O settlement failed", [primary, closing]
        ) from None


async def run_sync(call, *args, **kwargs):
    """Drain a synchronous physical call before releasing its export checkpoint."""
    owner = current_optional()
    if owner is None:
        return await asyncio.to_thread(call, *args, **kwargs)
    from app.services import storage_access

    await owner.enter_io()
    future, failure = None, None
    try:
        try:
            future = asyncio.get_running_loop().run_in_executor(
                None, copy_context().run, partial(call, *args, **kwargs)
            )
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
        return await asyncio.shield(future)
    except BaseException as exc:
        failure = exc
        if future is not None:
            try:
                await storage_access.finish(future)
            except BaseException as later:
                if later is not exc:
                    failure = BaseExceptionGroup(
                        "Export operation and drained physical work failed",
                        [exc, later],
                    )
                    raise failure from None
        raise
    finally:
        if (
            future is not None
            and future.done()
            and (failure is None or not storage_access.failure_unsettled(failure))
        ):
            await settle_io(failure)
