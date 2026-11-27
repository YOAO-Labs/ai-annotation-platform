"""Run a blocking storage operation and drain it after interruptions."""

import asyncio
from contextvars import copy_context
from functools import partial

from app.services import storage_access
from app.services.content_snapshot import ContentError

THREAD_SECONDS = 30


async def bounded_thread(call, *args, **kwargs):
    """A timeout or cancellation waits for the actual executor to settle."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return await asyncio.wait_for(asyncio.shield(pending), THREAD_SECONDS)
    except (asyncio.CancelledError, TimeoutError) as interrupted:
        try:
            await storage_access.finish(pending)
        except BaseException as closing:
            if getattr(closing, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        if isinstance(interrupted, TimeoutError):
            error = ContentError("content_read_timeout", 504)
            if getattr(interrupted, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(error)
            raise error from None
        raise
