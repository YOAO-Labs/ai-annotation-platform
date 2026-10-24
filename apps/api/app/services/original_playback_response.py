"""Single-range delivery from a pinned, already verified playback stage.

The registry retains the file and owns its final close. Each response owns only
one response pin, released after every physical offset read has finished.
"""

import asyncio
from collections.abc import Awaitable, Callable
import contextvars
from dataclasses import dataclass
import os
import re

from fastapi import HTTPException
from starlette.responses import StreamingResponse

from app.services.content_snapshot import ContentError
from app.services.original_binary_response import _drain, _response_error
from app.services.storage_access import mark_unsettled

CHUNK_BYTES = 256 * 1024
RESPONSE_SECONDS = 120
MAX_RANGE_HEADER = 512
_RANGE = re.compile(r"([0-9]*)-([0-9]*)", re.ASCII)


@dataclass(frozen=True)
class PlaybackRange:
    status_code: int
    start: int
    length: int
    content_range: str | None = None


def _invalid_range():
    raise HTTPException(400, detail={"code": "playback_range_invalid"})


def select_range(
    size: int,
    etag: str,
    *,
    request_method: str = "GET",
    range_header: str | None = None,
    if_range: str | None = None,
) -> PlaybackRange:
    """Select a range after authorization, without opening or reading the file."""
    if type(size) is not int or not 0 <= size <= 2**63 - 1:
        raise ValueError("Invalid verified stage size")
    method = request_method.upper()
    if method not in {"GET", "HEAD"}:
        raise ValueError("Playback supports only GET and HEAD")
    full = PlaybackRange(200, 0, size)
    if method == "HEAD" or range_header is None:
        return full
    if (
        not isinstance(range_header, str)
        or len(range_header) > MAX_RANGE_HEADER
        or any(
            (ord(char) < 32 and char != "\t") or ord(char) == 127
            for char in range_header
        )
    ):
        _invalid_range()
    if if_range is not None and if_range != etag:
        return full

    unit, separator, raw = range_header.strip().partition("=")
    if unit.strip().lower() != "bytes":
        return full
    if not separator:
        _invalid_range()
    parts = []
    for value in raw.split(","):
        match = _RANGE.fullmatch(value.strip())
        if match is None or not any(match.groups()):
            _invalid_range()
        left, right = match.groups()
        # The header bound limits each conversion to at most 511 ASCII digits;
        # offsets are clamped to the actual file before reaching os.pread.
        start = int(left) if left else None
        end = int(right) if right else None
        if start is not None and end is not None and end < start:
            _invalid_range()
        parts.append((start, end))
    if len(parts) > 1:
        return full

    start, end = parts[0]
    if start is None:
        start = max(0, size - end)
        end = size - 1
    else:
        end = size - 1 if end is None else min(end, size - 1)
    if start >= size or end < start:
        raise HTTPException(
            416,
            detail={"code": "playback_range_unsatisfiable"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    return PlaybackRange(206, start, end - start + 1, f"bytes {start}-{end}/{size}")


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
