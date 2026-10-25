"""Exact-object browser delivery with durable admission through physical close.

Capabilities are issued by authorized output readers. This transport validates
the saved owner and current target, then pins each request with HEAD/IfMatch.
"""

import asyncio
import contextvars
from copy import deepcopy
from dataclasses import dataclass
import threading

from fastapi import HTTPException
from sqlalchemy import exists, select
from starlette.responses import StreamingResponse

from app.db.base import async_session
from app.db.models.image_pyramid import ImagePyramidOutput
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.thumbnail_output import ThumbnailOutput
from app.db.models.video_derivative import VideoDerivativeOutput
from app.services import media_cache_storage, media_cache_urls, storage_access
from app.services.original_binary_response import _drain
from app.services.original_playback_response import select_range

CHUNK_BYTES = 256 * 1024
RESPONSE_SECONDS = 120
READ_SECONDS = 30
ADMISSION_SECONDS = READ_SECONDS
THUMBNAIL_MAX_BYTES = 2 * 1024**2
_slots = asyncio.BoundedSemaphore(4)
_waiting = threading.BoundedSemaphore(64)
_FAMILIES = {
    "thumbnail": (ThumbnailOutput, "thumbnails"),
    "image-pyramid": (ImagePyramidOutput, "image-pyramids"),
    "video-derivative": (VideoDerivativeOutput, "video-derivatives"),
}


def _error(code="media_cache_unavailable", status=503):
    return HTTPException(status, detail={"code": code})


def _missing():
    raise _error("media_cache_not_found", 404)


def _purpose(capability):
    if capability.kind == "thumbnail":
        from app.services.thumbnail_outputs import purpose
    elif capability.kind == "image-pyramid":
        from app.services.image_pyramid_objects import purpose
    else:
        from app.services.video_derivative_objects import purpose
    return purpose(capability.output_id)


def _plain(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and all(ord(char) > 32 and ord(char) != 127 for char in value)
    )


def _version(metadata):
    value = metadata.get("VersionId")
    if value in (None, "null"):
        return None
    if not _plain(value):
        raise _error("media_cache_mismatch")
    return value


@dataclass(frozen=True)
class PinnedObject:
    reference: dict
    key: str
    sha256: str
    size: int
    media_type: str
    version_id: str | None = None
    saved_etag: str | None = None


async def _object(db, capability, *, permit=None):
    model, family = _FAMILIES[capability.kind]
    if permit is None:
        row = await db.get(model, capability.output_id, populate_existing=True)
    else:
        row = await db.scalar(
            select(model)
            .where(model.id == capability.output_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    if row is None or row.state not in {"ready", "retiring"}:
        _missing()
    if permit is not None and row.state == "retiring":
        # Collectors hold this owner's exclusive row lock while rechecking all
        # exact-purpose operations. A new read must also exclude a collector
        # that passed that check before this permit was inserted. Conservatively
        # reject other retiring reads too; ready reads remain concurrent.
        if await db.scalar(
            select(
                exists().where(
                    StorageOperation.purpose == _purpose(capability),
                    StorageOperation.id != permit.id,
                )
            )
        ):
            raise _error("media_cache_busy")
    reference = deepcopy(row.storage_ref)
    try:
        owner = media_cache_storage.resolve_ref(
            reference,
            expected_prefix=f"{family}/objects/{capability.output_id}/",
        )
        if media_cache_urls.reference_digest(reference) != capability.reference:
            _missing()
        key = owner.key(capability.relative)
        version_id, saved_etag = None, None
        if capability.kind == "thumbnail":
            if (
                row.uncertain_write
                or row.owner_kind
                not in {"directory_preview", "dataset_thumbnail", "task_thumbnail"}
                or capability.relative != "thumbnail.webp"
                or type(row.size_bytes) is not int
                or not 0 < row.size_bytes <= THUMBNAIL_MAX_BYTES
            ):
                _missing()
            receipt = {"sha256": row.sha256, "size_bytes": row.size_bytes}
            version_id = _version({"VersionId": row.version_id})
            saved_etag = row.etag
            if saved_etag is not None and not _plain(saved_etag):
                _missing()
        elif capability.kind == "image-pyramid":
            from app.services.image_pyramid_storage import validate_inventory

            receipt = validate_inventory(row).get(key)
        else:
            from app.services.video_derivative_storage import validate_inventory

            receipt = validate_inventory(row).get(key)
        if receipt != {
            "sha256": capability.sha256,
            "size_bytes": capability.size_bytes,
        }:
            _missing()
    except (ValueError, KeyError, TypeError, AttributeError):
        _missing()
    suffix = capability.relative.rsplit(".", 1)[-1]
    media_type = {
        "webp": "image/webp",
        "jpeg": "image/jpeg",
        "mp4": "video/mp4",
        "json": "application/json",
    }.get(suffix)
    if media_type is None:
        _missing()
    return PinnedObject(
        reference,
        key,
        capability.sha256,
        capability.size_bytes,
        media_type,
        version_id,
        saved_etag,
    )


class Transfer:
    """Adopt handles inside their executor, including results arriving late."""

    def __init__(self, pinned, target, permit):
        self.pinned, self.target, self.permit = pinned, target, permit
        self.context = self.client = self.body = None
        self.entered = False
        self.unsettled = False

    def open(self, selected, *, head=False):
        self.context = self.target.client(read_timeout=READ_SECONDS)
        try:
            self.client = self.context.__enter__()
            self.entered = True
        except BaseException as exc:
            self.unsettled = True
            raise storage_access.mark_unsettled(exc)
        params = {"Bucket": self.target.bucket, "Key": self.pinned.key}
        if self.pinned.version_id is not None:
            params["VersionId"] = self.pinned.version_id
        observed = self.client.head_object(**params)
        etag, version = observed.get("ETag"), _version(observed)
        if (
            not _plain(etag)
            or (
                observed.get("DeleteMarker") is not None
                and observed["DeleteMarker"] is not False
            )
            or observed.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or observed.get("ContentRange") is not None
            or type(observed.get("ContentLength")) is not int
            or observed["ContentLength"] != self.pinned.size
            or (
                self.pinned.version_id is not None and version != self.pinned.version_id
            )
            or (self.pinned.saved_etag is not None and etag != self.pinned.saved_etag)
        ):
            raise _error("media_cache_mismatch")
        if head:
            return
        condition = {"IfMatch": etag}
        if version is not None:
            condition["VersionId"] = version
        if selected.status_code == 206:
            condition["Range"] = (
                f"bytes={selected.start}-{selected.start + selected.length - 1}"
            )
        result = self.client.get_object(**(params | condition))
        # Assign before validating, so malformed responses still have an owner.
        self.body = result.get("Body")
        if (
            self.body is None
            or (
                result.get("DeleteMarker") is not None
                and result["DeleteMarker"] is not False
            )
            or result.get("ResponseMetadata", {}).get("HTTPStatusCode")
            != selected.status_code
            or type(result.get("ContentLength")) is not int
            or result["ContentLength"] != selected.length
            or result.get("ContentRange") != selected.content_range
            or result.get("ETag") != etag
            or _version(result) != version
        ):
            raise _error("media_cache_mismatch")

    def close(self):
        failure = None
        if self.body is not None:
            try:
                storage_access.close_registered(self.body)
            except BaseException as exc:
                failure = exc
            self.body = None
        if self.entered:
            try:
                self.context.__exit__(None, None, None)
            except BaseException as exc:
                failure = storage_access.mark_unsettled(exc)
            self.entered = False
        if self.unsettled or failure is not None:
            self.unsettled = True
            raise storage_access.mark_unsettled(_error("storage_operation_unsettled"))


async def _metadata(capability):
    """Own a short session, committing every metadata transaction before I/O."""
    permit = None
    try:
        async with async_session() as db:
            pinned = await _object(db, capability)
            target = await media_cache_storage.target_for_ref(db, pinned.reference)
            revision = await storage_access.check(db, target.location_id)
            await db.commit()
            permit = await storage_access.acquire(
                db,
                target.location_id,
                _purpose(capability),
                expected_revision=revision,
            )
            # Refresh current credentials and Gate after durable admission. No
            # business row lock or metadata transaction survives into the client.
            if await _object(db, capability, permit=permit) != pinned:
                _missing()
            target = await media_cache_storage.target_for_ref(db, pinned.reference)
            await storage_access.check(
                db, target.location_id, expected_revision=permit.revision, lock=True
            )
            await db.commit()
        return Transfer(pinned, target, permit)
    except BaseException as exc:
        if permit is not None:
            await permit.__aexit__(type(exc), exc, exc.__traceback__)
        raise


async def _executor(call, *args):
    return await asyncio.get_running_loop().run_in_executor(
        None, contextvars.copy_context().run, call, *args
    )


async def _release(transfer, failure=None):
    """Physical closes precede settling the independent operation record."""
    closing = asyncio.create_task(_executor(transfer.close))
    _, close_error, cancelled = await _drain(closing)
    if close_error is not None:
        failure = storage_access.mark_unsettled(_error("storage_operation_unsettled"))
    settling = asyncio.create_task(
        transfer.permit.__aexit__(
            type(failure) if failure is not None else None,
            failure,
            failure.__traceback__ if failure is not None else None,
        )
    )
    _, settle_error, settle_cancelled = await _drain(settling)
    unsettled = any(
        getattr(exc, "storage_operation_unsettled", False)
        for exc in (failure, close_error, settle_error)
    )
    failure = close_error or settle_error
    if cancelled or settle_cancelled:
        interrupted = asyncio.CancelledError()
        if unsettled:
            storage_access.mark_unsettled(interrupted)
        raise interrupted
    if failure is not None:
        raise failure


class MediaCacheResponse(StreamingResponse):
    def __init__(self, transfer, selected, *, head=False):
        self.transfer, self.selected, self.head = transfer, selected, head
        self.pending = None
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "Content-Length": str(selected.length),
            "ETag": f'"sha256:{transfer.pinned.sha256}"',
            "X-Content-SHA256": transfer.pinned.sha256,
            "Accept-Ranges": "bytes",
        }
        if selected.content_range is not None:
            headers["Content-Range"] = selected.content_range
        super().__init__(
            self._chunks(),
            status_code=selected.status_code,
            media_type=transfer.pinned.media_type,
            headers=headers,
        )

    async def _chunks(self):
        if self.head:
            return
        remaining = self.selected.length
        while remaining:
            requested = min(CHUNK_BYTES, remaining)
            self.pending = asyncio.create_task(
                _executor(self.transfer.body.read, requested)
            )
            try:
                chunk = await asyncio.shield(self.pending)
            except Exception as exc:
                failure = _error()
                if getattr(exc, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(failure)
                raise failure from None
            self.pending = None
            if type(chunk) is not bytes or not 0 < len(chunk) <= requested:
                raise _error("media_cache_mismatch")
            remaining -= len(chunk)
            yield chunk

    async def __call__(self, scope, receive, send):
        failure = None
        try:
            async with asyncio.timeout(RESPONSE_SECONDS):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            failure = _error("media_cache_timeout", 504)
        except BaseException as exc:
            failure = exc
        finally:
            if self.pending is not None:
                _, late_error, cancelled = await _drain(self.pending)
                if cancelled and failure is None:
                    failure = asyncio.CancelledError()
                if getattr(late_error, "storage_operation_unsettled", False):
                    failure = storage_access.mark_unsettled(
                        failure or _error("storage_operation_unsettled")
                    )
                self.pending = None
            try:
                await _release(self.transfer, failure)
            except BaseException as exc:
                if isinstance(failure, asyncio.CancelledError):
                    if getattr(exc, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(failure)
                else:
                    failure = exc
            _slots.release()
        if failure is not None:
            raise failure from None


async def _admit():
    """Queue normal image-grid bursts without opening metadata or provider I/O."""
    if not _waiting.acquire(blocking=False):
        raise _error("media_cache_busy")
    try:
        async with asyncio.timeout(ADMISSION_SECONDS):
            await _slots.acquire()
    except TimeoutError:
        raise _error("media_cache_busy") from None
    finally:
        _waiting.release()


async def response(capability, *, method="GET", range_header=None, if_range=None):
    """Transfer ownership to the response only after a pinned transport opens."""
    method = method.upper()
    await _admit()
    transfer = None
    pending = asyncio.create_task(_metadata(capability))
    try:
        async with asyncio.timeout(READ_SECONDS):
            transfer = await asyncio.shield(pending)
            selected = select_range(
                transfer.pinned.size,
                f'"sha256:{transfer.pinned.sha256}"',
                request_method=method,
                range_header=range_header,
                if_range=if_range,
            )
            pending = asyncio.create_task(
                _executor(lambda: transfer.open(selected, head=method == "HEAD"))
            )
            await asyncio.shield(pending)
        return MediaCacheResponse(transfer, selected, head=method == "HEAD")
    except BaseException as exc:
        late_result, late_error, cancelled = await _drain(pending)
        if transfer is None and isinstance(late_result, Transfer):
            transfer = late_result
        if getattr(late_error, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(exc)
        try:
            if transfer is not None:
                await _release(transfer, exc)
        except BaseException as close_error:
            if getattr(close_error, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(exc)
        finally:
            _slots.release()
        if isinstance(exc, asyncio.CancelledError) or cancelled:
            interrupted = (
                exc
                if isinstance(exc, asyncio.CancelledError)
                else asyncio.CancelledError()
            )
            if getattr(exc, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
            raise interrupted
        if isinstance(exc, HTTPException):
            raise
        error = _error()
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(error)
        raise error from None
