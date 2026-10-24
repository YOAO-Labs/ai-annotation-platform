"""Retain snapshot ownership when the client leaves before response streaming."""

import asyncio

from app.services.original_binary_response import close_stage
from app.services.storage_access import mark_unsettled


async def connected_snapshot(request, operation, *, close_result=True):
    """Cancel and drain capture on disconnect; a late stage still has an owner.

    GET request bodies are unused here. The watcher owns the receive channel only
    until capture settles, before StreamingResponse starts its own lifecycle.
    Materialized JSON operations may own all cleanup internally and explicitly
    set close_result=False; they still drain their work before returning.
    """
    caller = asyncio.current_task()
    cancellation_count = caller.cancelling()
    disconnected = False
    receive_error = None

    async def watch():
        nonlocal disconnected, receive_error
        try:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    caller.cancel()
                    return
        except asyncio.CancelledError:
            return  # Planned watcher shutdown is a successful, drained outcome.
        except Exception as exc:
            receive_error = exc
            caller.cancel()

    stage, failure = None, None
    watcher = asyncio.create_task(watch())
    try:
        stage = await operation
    except BaseException as exc:
        failure = exc
    finally:
        watcher.cancel()
        cancelled_again = False
        while not watcher.done():
            try:
                await asyncio.shield(watcher)
            except asyncio.CancelledError:
                if caller.cancelling() > cancellation_count:
                    cancelled_again = True
        if not watcher.cancelled():
            watcher.result()
        if cancelled_again and not isinstance(failure, asyncio.CancelledError):
            failure = asyncio.CancelledError()

    if caller.cancelling() > cancellation_count and failure is None:
        failure = asyncio.CancelledError()
    if failure is not None or disconnected or receive_error is not None:
        if stage is not None and close_result:
            try:
                await close_stage(stage)
            except BaseException as closing:
                if isinstance(failure, asyncio.CancelledError):
                    if getattr(closing, "storage_operation_unsettled", False):
                        mark_unsettled(failure)
                    raise failure from None
                raise
        if receive_error is not None:
            raise receive_error
        if disconnected and (
            failure is None or isinstance(failure, asyncio.CancelledError)
        ):
            return None
        raise failure
    return stage
