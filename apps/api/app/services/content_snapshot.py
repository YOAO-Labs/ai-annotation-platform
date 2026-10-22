"""Bounded request snapshots; the temporary file owns its admission slot."""

import asyncio
from dataclasses import dataclass, field
import hashlib
import tempfile
import threading
import time
import warnings

from PIL import Image
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 32_000_000
COPY_SECONDS = 10
WAIT_SECONDS = 15
CHUNK_BYTES = 256 * 1024
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
    _closed: bool = False
    _lock: object = field(default_factory=threading.Lock)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
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


def _capture(factory, expected_sha256, stop):
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
        temporary.seek(0)
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
        except ContentError:
            raise
        except (
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise ContentError("invalid_image", 415) from None
        check()
        temporary.seek(0)
        result = Snapshot(
            temporary, sha256, size, observation, media_type, width, height
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


def _dispose(task):
    try:
        task.result().close()
    except BaseException:
        pass  # Errors have no returned snapshot; the reader owns cleanup.


async def capture(factory, expected_sha256=None):
    if not _slots.acquire(blocking=False):
        raise ContentError("snapshot_capacity_exceeded", 429)
    stop = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(_capture, factory, expected_sha256, stop)
    )
    try:
        return await asyncio.wait_for(asyncio.shield(task), WAIT_SECONDS)
    except (TimeoutError, asyncio.CancelledError) as exc:
        stop.set()
        task.add_done_callback(_dispose)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ContentError("content_read_timeout", 504) from None


class SnapshotResponse(StreamingResponse):
    def __init__(self, snapshot):
        self.snapshot = snapshot

        async def chunks():
            while chunk := await run_in_threadpool(snapshot.file.read, CHUNK_BYTES):
                yield chunk

        super().__init__(
            chunks(),
            media_type=snapshot.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(snapshot.size),
                "ETag": f'"sha256:{snapshot.sha256}"',
                "X-Content-SHA256": snapshot.sha256,
                "Accept-Ranges": "none",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(30):
                await super().__call__(scope, receive, send)
        finally:
            self.snapshot.close()
