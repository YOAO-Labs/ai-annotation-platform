"""Single-range delivery from a pinned, already verified playback stage.

The registry retains the file and owns its final close. Each response owns only
one response pin, released after every physical offset read has finished.
"""

import asyncio
import contextvars
import os
from collections.abc import Awaitable, Callable

from starlette.responses import StreamingResponse

from app.services.content_snapshot import ContentError
from app.services.original_binary_response import _response_error
from app.services.playback_range import (
    _RANGE as _RANGE,
)
from app.services.playback_range import (
    MAX_RANGE_HEADER as MAX_RANGE_HEADER,
)
from app.services.playback_range import (
    PlaybackRange as PlaybackRange,
)
from app.services.playback_range import (
    _invalid_range as _invalid_range,
)
from app.services.playback_range import (
    select_range as select_range,
)
from app.services.storage_access import mark_unsettled
from app.services.storage_io_settlement import _drain

CHUNK_BYTES = 256 * 1024
RESPONSE_SECONDS = 120


class PlaybackResponse(StreamingResponse):
    def __init__(
        self,
        stage,
        *,
        media_type: str,
        release: Callable[[], Awaitable[None]],
        request_method: str = "GET",
        range_header: str | None = None,
        if_range: str | None = None,
    ):
        """Transfer an acquired response pin only after construction succeeds.

        A caller whose construction fails still owns and must release its pin.
        Authorization and the registry pin always precede range selection.
        """
        etag = f'"sha256:{stage.sha256}"'
        selected = select_range(
            stage.size,
            etag,
            request_method=request_method,
            range_header=range_header,
            if_range=if_range,
        )
        self.stage = stage
        self.selected = selected
        self._head = request_method.upper() == "HEAD"
        self._release = release
        self._pending_read = None
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "Content-Length": str(selected.length),
            "ETag": etag,
            "X-Content-SHA256": stage.sha256,
            "Accept-Ranges": "bytes",
        }
        if selected.content_range is not None:
            headers["Content-Range"] = selected.content_range
        super().__init__(
            self._chunks(),
            status_code=selected.status_code,
            media_type=media_type,
            headers=headers,
        )

    async def _chunks(self):
        if self._head:
            return
        offset, remaining = self.selected.start, self.selected.length
        while remaining:
            requested = min(CHUNK_BYTES, remaining)
            context = contextvars.copy_context()
            retrieved = False
            try:
                self._pending_read = asyncio.get_running_loop().run_in_executor(
                    None,
                    context.run,
                    os.pread,
                    self.stage.file.fileno(),
                    requested,
                    offset,
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
            offset += len(chunk)
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
            cancelled, read_error = False, None
            if self._pending_read is not None:
                _, read_error, cancelled = await _drain(self._pending_read)
                self._pending_read = None
            try:
                release = asyncio.ensure_future(self._release())
            except BaseException as exc:
                release_error, release_cancelled = mark_unsettled(exc), False
            else:
                _, release_error, release_cancelled = await _drain(release)
                if release_error is not None:
                    mark_unsettled(release_error)
            cancelled = cancelled or release_cancelled
            if cancelled and not isinstance(failure, asyncio.CancelledError):
                failure = asyncio.CancelledError()
            if release_error is not None:
                if failure is None or not isinstance(failure, asyncio.CancelledError):
                    failure = ContentError("storage_operation_unsettled", 503)
            elif read_error is not None and not isinstance(
                failure, asyncio.CancelledError
            ):
                failure = _response_error(read_error)
            if (
                any(
                    getattr(exc, "storage_operation_unsettled", False)
                    for exc in (failure, read_error, release_error)
                )
                and failure is not None
            ):
                mark_unsettled(failure)
        if failure is not None:
            raise failure from None
