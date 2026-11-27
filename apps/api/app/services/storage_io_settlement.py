"""Settlement of in-flight storage I/O under cancellation."""

import asyncio
from contextvars import copy_context
from functools import partial


async def _drain(pending):
    """Return the actual outcome without abandoning an executor operation."""
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        return pending.result(), None, cancelled
    except BaseException as exc:
        return None, exc, cancelled


def mark_unsettled(exc):
    """Preserve uncertainty when a physical constructor or close did not settle."""
    exc.storage_operation_unsettled = True
    return exc


def failure_unsettled(exc: BaseException) -> bool:
    """Keep an admission open when any retained physical failure is uncertain."""
    if getattr(exc, "storage_operation_unsettled", False):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(
        failure_unsettled(child) for child in exc.exceptions
    )


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


async def settled_io(call, *args, **kwargs):
    """Retain the real Future through every interruption, including soft limits."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise mark_unsettled(exc)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await finish(pending)
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                mark_unsettled(interrupted)
        raise
