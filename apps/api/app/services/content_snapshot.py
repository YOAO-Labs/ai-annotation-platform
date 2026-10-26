"""Bounded request snapshots; the temporary file owns its admission slot."""

import asyncio
from dataclasses import dataclass, field
import contextvars
from functools import partial
import hashlib
import re
import tempfile
import threading
import time
import warnings

from PIL import Image
from fastapi import HTTPException
from starlette.responses import StreamingResponse

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32_000_000
COPY_SECONDS = 10
WAIT_SECONDS = 15
CHUNK_BYTES = 256 * 1024
RESPONSE_SECONDS = 30
_slots = threading.BoundedSemaphore(4)
IMAGE_RESPONSES = {
    200: {
        "description": "Verified full image snapshot",
        "content": {
            media: {"schema": {"type": "string", "format": "binary"}}
            for media in ("image/png", "image/jpeg", "image/webp")
        },
    }
}
VERIFIED_IMAGE_RESPONSES = {
    **IMAGE_RESPONSES,
    206: {
        "description": "Single byte range from a fully verified Agent image snapshot",
        "content": IMAGE_RESPONSES[200]["content"],
        "headers": {
            "Content-Range": {"schema": {"type": "string"}},
            "Accept-Ranges": {"schema": {"type": "string", "const": "bytes"}},
        },
    },
    416: {"description": "Unsupported or unsatisfiable image range"},
}


class ContentError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass
class Snapshot:
    file: object
    sha256: str
    size: int
    observation: dict
    media_type: str
    width: int
    height: int
    inspection_error: str | None = None
    _closed: bool = False
    _lock: object = field(default_factory=threading.Lock)
    _cleanup: object = field(default=None, repr=False)
    _range_supported: bool = field(default=False, repr=False)

    @property
    def supports_ranges(self):
        return (
            self._range_supported or getattr(self, "_agent_receipt", None) is not None
        )

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._cleanup is not None:
                    self._cleanup()
                    return
                try:
                    self.file.close()
                finally:
                    _slots.release()

    def public(self):
        return {
            "sha256": self.sha256,
            "size_bytes": self.size,
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
        }


def _inspect_image(temporary, check, inspect_quality=False):
    """Inspect bytes in place; file and admission ownership stay with the caller."""
    check()
    temporary.seek(0)
    media_type, width, height, inspection_error = (
        "application/octet-stream",
        0,
        0,
        None,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(temporary) as image:
                if (
                    image.format not in {"JPEG", "PNG", "WEBP"}
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ContentError("image_format_unsupported", 415)
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise ContentError("image_pixel_budget_exceeded", 413)
                media_type = Image.MIME[image.format]
                from app.services.storage import StorageService

                width, height = StorageService._logical_image_dimensions(image)
                image.load()
    except ContentError as exc:
        if not inspect_quality:
            raise
        inspection_error = exc.code
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        if not inspect_quality:
            raise ContentError("invalid_image", 415) from None
        inspection_error = "image_pixel_budget_exceeded"
    except (OSError, ValueError):
        if not inspect_quality:
            raise ContentError("invalid_image", 415) from None
        inspection_error = "invalid_image"
    check()
    temporary.seek(0)
    return media_type, width, height, inspection_error


def _capture(factory, expected_sha256, stop, inspect_quality=False):
    temporary = None
    transferred = False
    try:
        temporary = tempfile.TemporaryFile(mode="w+b", prefix="aap-content-")
        digest, size = hashlib.sha256(), 0
        deadline = time.monotonic() + COPY_SECONDS

        def check():
            if stop.is_set() or time.monotonic() > deadline:
                raise ContentError("content_read_timeout", 504)

        with factory() as (body, observation):
            if observation.get("size", 0) > MAX_BYTES:
                raise ContentError("content_too_large", 413)
            while True:
                check()
                chunk = body.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ContentError("content_too_large", 413)
                temporary.write(chunk)
                digest.update(chunk)
            if size != observation["size"]:
                raise ContentError("source_changed")
        check()
        sha256 = digest.hexdigest()
        if expected_sha256 and expected_sha256 != sha256:
            raise ContentError("content_mismatch")
        media_type, width, height, inspection_error = _inspect_image(
            temporary, check, inspect_quality
        )
        result = Snapshot(
            temporary,
            sha256,
            size,
            observation,
            media_type,
            width,
            height,
            inspection_error=inspection_error,
        )
        transferred = True
        return result
    finally:
        if not transferred:
            try:
                if temporary is not None:
                    temporary.close()
            finally:
                _slots.release()


async def _capture_bounded(factory, expected_sha256=None, *, inspect_quality=False):
    if not _slots.acquire(blocking=False):
        raise ContentError("snapshot_capacity_exceeded", 429)
    stop = threading.Event()
    # Executor Futures survive asyncio.run's Task cancellation sweep.
    context = contextvars.copy_context()
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None,
            context.run,
            partial(_capture, factory, expected_sha256, stop, inspect_quality),
        )
    except BaseException:
        _slots.release()
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(pending), WAIT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as exc:
        stop.set()
        cancelled = isinstance(exc, asyncio.CancelledError)
        # Deadlines are cooperative. A blocked SDK read must physically end
        # before its handle, snapshot slot and source permit can be released.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        unsettled = False
        try:
            pending.result().close()
        except BaseException as late:
            unsettled = getattr(late, "storage_operation_unsettled", False)
        error = (
            asyncio.CancelledError()
            if cancelled
            else ContentError("content_read_timeout", 504)
        )
        if unsettled:
            error.storage_operation_unsettled = True
        raise error from None


async def capture_from_location(
    db,
    location_id,
    factory,
    expected_sha256=None,
    *,
    expected_revision=None,
    node_id=None,
    purpose="reference_read",
    inspect_quality=False,
    on_acquired=None,
):
    """Account for physical reads, including cancellation and late cleanup."""
    from app.services import storage_access

    snapshot = None
    try:
        async with await storage_access.acquire(
            db,
            location_id,
            purpose,
            expected_revision=expected_revision,
            node_id=node_id,
        ):
            if on_acquired is not None:
                await on_acquired()
            snapshot = await (
                capture_for_quality(factory, expected_sha256)
                if inspect_quality
                else capture(factory, expected_sha256)
            )
        return snapshot
    except BaseException as exc:
        if snapshot is not None:
            snapshot.close()
        if isinstance(exc, Exception) and getattr(
            exc, "storage_operation_unsettled", False
        ):
            error = ContentError("storage_operation_unsettled", 503)
            error.storage_operation_unsettled = True
            raise error from None
        raise


async def capture(factory, expected_sha256=None):
    return await _capture_bounded(factory, expected_sha256)


async def capture_for_quality(factory, expected_sha256):
    """Keep the full-byte proof when bounded image inspection cannot decode it."""
    return await _capture_bounded(factory, expected_sha256, inspect_quality=True)


def _adopt_stage(stage, stop, inspect_quality):
    transferred = False
    deadline = time.monotonic() + COPY_SECONDS

    def check():
        if stop.is_set() or time.monotonic() > deadline:
            raise ContentError("content_read_timeout", 504)

    try:
        if (
            type(stage.size) is not int
            or stage.size < 0
            or not isinstance(stage.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", stage.sha256) is None
            or not isinstance(stage.observation, dict)
        ):
            raise ContentError("content_mismatch")
        if stage.size > MAX_BYTES:
            raise ContentError("content_too_large", 413)
        if stage.file.seek(0, 2) != stage.size:
            raise ContentError("source_changed")
        media_type, width, height, inspection_error = _inspect_image(
            stage.file, check, inspect_quality
        )
        result = Snapshot(
            stage.file,
            stage.sha256,
            stage.size,
            stage.observation,
            media_type,
            width,
            height,
            inspection_error=inspection_error,
            _cleanup=stage.close,
        )
        transferred = True
        return result
    finally:
        if not transferred:
            stage.close()


async def adopt_stage(stage, *, inspect_quality=False):
    """Consume one verified binary stage without copying it or claiming an image slot."""
    stop = threading.Event()
    try:
        context = contextvars.copy_context()
        pending = asyncio.get_running_loop().run_in_executor(
            None, context.run, partial(_adopt_stage, stage, stop, inspect_quality)
        )
    except BaseException:
        stage.close()
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(pending), WAIT_SECONDS)
    except BaseException as exc:
        stop.set()
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except BaseException:
                continue
        unsettled = False
        try:
            result = pending.result()
        except BaseException as late:
            unsettled = getattr(late, "storage_operation_unsettled", False)
        else:
            try:
                closing = asyncio.get_running_loop().run_in_executor(None, result.close)
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except BaseException:
                        continue
                closing.result()
            except BaseException:
                unsettled = True
        error = (
            ContentError("content_read_timeout", 504)
            if isinstance(exc, TimeoutError)
            else exc
        )
        if unsettled:
            error.storage_operation_unsettled = True
        raise error from None


class SnapshotResponse(StreamingResponse):
    def __init__(self, snapshot, *, range_header=None, if_range=None):
        self.snapshot = snapshot
        etag = f'"sha256:{snapshot.sha256}"'
        try:
            if range_header is not None and not snapshot.supports_ranges:
                raise HTTPException(
                    416, "Range is not supported for verified snapshots"
                )
            from app.services.original_playback_response import select_range

            try:
                selected = select_range(
                    snapshot.size, etag, range_header=range_header, if_range=if_range
                )
            except HTTPException as exc:
                raise HTTPException(
                    exc.status_code,
                    detail={
                        "code": "content_range_unsatisfiable"
                        if exc.status_code == 416
                        else "content_range_invalid"
                    },
                    headers=exc.headers,
                ) from None
            # Playback may ignore a valid multipart request. Image snapshots
            # deliberately expose only a single interval, including on retries.
            if (
                range_header is not None
                and (if_range is None or if_range == etag)
                and range_header.partition("=")[0].strip().lower() == "bytes"
                and "," in range_header.partition("=")[2]
            ):
                raise HTTPException(
                    416,
                    detail={"code": "content_range_multiple_unsupported"},
                    headers={"Content-Range": f"bytes */{snapshot.size}"},
                )
        except BaseException:
            snapshot.close()
            raise

        async def chunks():
            await self._io(snapshot.file.seek, selected.start)
            remaining = selected.length
            while remaining:
                chunk = await self._io(snapshot.file.read, min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ContentError("content_snapshot_truncated")
                remaining -= len(chunk)
                yield chunk

        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(selected.length),
            "ETag": etag,
            "X-Content-SHA256": snapshot.sha256,
            "Accept-Ranges": "bytes" if snapshot.supports_ranges else "none",
        }
        if selected.content_range is not None:
            headers["Content-Range"] = selected.content_range
        try:
            super().__init__(
                chunks(),
                status_code=selected.status_code,
                media_type=snapshot.media_type,
                headers=headers,
            )
        except BaseException:
            snapshot.close()
            raise

    async def _io(self, call, *args):
        context = contextvars.copy_context()
        pending = asyncio.get_running_loop().run_in_executor(
            None, context.run, partial(call, *args)
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            from app.services.original_binary_response import _drain

            await _drain(pending)
            raise

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(RESPONSE_SECONDS):
                await super().__call__(scope, receive, send)
        finally:
            self.snapshot.close()
