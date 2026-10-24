"""Bounded full downloads; each response owns its verified temporary file."""

import asyncio
import contextvars
import threading

from starlette.responses import StreamingResponse

from app.services import managed_original_reader as reader
from app.services.content_snapshot import ContentError
from app.services.storage_access import mark_unsettled

DOWNLOAD_MAX_BYTES = 512 * 1024**2
COPY_SECONDS = 120
RESPONSE_SECONDS = 120
CHUNK_BYTES = 256 * 1024
_slots = threading.BoundedSemaphore(2)


async def capture_download(spec):
    """Keep download admission separate from the existing image reader pool."""
    return await reader.capture(
        spec,
        max_bytes=DOWNLOAD_MAX_BYTES,
        timeout_seconds=COPY_SECONDS,
        admission=_slots,
        transport_timeout_seconds=min(COPY_SECONDS, 30),
    )


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


def _response_error(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return mark_unsettled(ContentError("storage_operation_unsettled", 503))
    return ContentError("content_source_unavailable", 503)


async def close_stage(stage):
    """Close a retired stage off-loop and retain ownership through cancellation."""
    context = contextvars.copy_context()
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, context.run, stage.close
        )
    except BaseException as exc:
        raise mark_unsettled(exc)
    _, failure, cancelled = await _drain(pending)
    if cancelled:
        error = asyncio.CancelledError()
        if getattr(failure, "storage_operation_unsettled", False):
            mark_unsettled(error)
        raise error
    if failure is not None:
        raise failure


class BinaryResponse(StreamingResponse):
    def __init__(self, stage):
        self.stage = stage
        self._pending_read = None
        super().__init__(
            self._chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="original.bin"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(stage.size),
                "ETag": f'"sha256:{stage.sha256}"',
                "X-Content-SHA256": stage.sha256,
                "Accept-Ranges": "none",
            },
        )

    async def _chunks(self):
        remaining = self.stage.size
        while remaining:
            requested = min(CHUNK_BYTES, remaining)
            context = contextvars.copy_context()
            retrieved = False
            try:
                self._pending_read = asyncio.get_running_loop().run_in_executor(
                    None, context.run, self.stage.file.read, requested
                )
                chunk = await asyncio.shield(self._pending_read)
                retrieved = True
            except Exception as exc:
                retrieved = True
                raise _response_error(exc) from None
            finally:
                if retrieved:
                    self._pending_read = None
            if type(chunk) is not bytes or not 0 < len(chunk) <= requested:
                raise ContentError("content_mismatch")
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send):
        failure = None
        try:
            async with asyncio.timeout(RESPONSE_SECONDS):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            failure = ContentError("content_response_timeout", 504)
        except BaseException as exc:
            failure = exc
        finally:
            cancelled, cleanup_error = False, None
            if self._pending_read is not None:
                _, cleanup_error, cancelled = await _drain(self._pending_read)
                self._pending_read = None
            context = contextvars.copy_context()
            try:
                closing = asyncio.get_running_loop().run_in_executor(
                    None, context.run, self.stage.close
                )
            except BaseException as exc:
                close_error, close_cancelled = mark_unsettled(exc), False
            else:
                _, close_error, close_cancelled = await _drain(closing)
            cancelled = cancelled or close_cancelled
            cleanup_error = close_error or cleanup_error
            unsettled = any(
                getattr(exc, "storage_operation_unsettled", False)
                for exc in (failure, cleanup_error)
            )
            if cancelled and not isinstance(failure, asyncio.CancelledError):
                failure = asyncio.CancelledError()
            if cleanup_error is not None:
                if isinstance(failure, asyncio.CancelledError):
                    if getattr(cleanup_error, "storage_operation_unsettled", False):
                        mark_unsettled(failure)
                else:
                    failure = _response_error(cleanup_error)
            if unsettled and failure is not None:
                mark_unsettled(failure)
        if failure is not None:
            raise failure from None
