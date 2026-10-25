"""Verify a saved original into an owned binary file, without business I/O.

The caller owns authorization, maintenance admission and the immutable source
snapshot. Only observations of the current target select transport conditions;
historical creation ETag/version receipts are deliberately absent from ObjectSpec.
"""

import asyncio
import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import hashlib
import math
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from app.services.content_snapshot import ContentError
from app.services.filesystem_storage import FilesystemStorageError
from app.services.storage_access import close_registered, mark_unsettled

CHUNK_BYTES = 256 * 1024
_slots = threading.BoundedSemaphore(4)
_SHA = re.compile(r"[0-9a-f]{64}")


def _reject(code="source_changed", status=409):
    raise ContentError(code, status)


def _header(value, maximum=1024):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and value.isascii()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _endpoint(value):
    if (
        not _header(value, 2048)
        or any(char.isspace() for char in value)
        or "\\" in value
        or value.endswith("/")
    ):
        _reject("managed_original_read_invalid")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        _reject("managed_original_read_invalid")
    return value


@dataclass(frozen=True)
class ObjectSpec:
    endpoint_url: str
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int

    def __post_init__(self):
        _endpoint(self.endpoint_url)
        try:
            valid_key = (
                isinstance(self.object_key, str)
                and bool(self.object_key)
                and len(self.object_key.encode("utf-8")) <= 1024
                and not any(
                    ord(char) < 32 or ord(char) == 127 for char in self.object_key
                )
            )
        except UnicodeError:
            valid_key = False
        if (
            not _header(self.bucket, 255)
            or any(char.isspace() or char in "/\\?#@" for char in self.bucket)
            or not valid_key
            or not isinstance(self.sha256, str)
            or _SHA.fullmatch(self.sha256) is None
            or type(self.size_bytes) is not int
            or not 0 <= self.size_bytes <= 2**63 - 1
        ):
            _reject("managed_original_read_invalid")


@dataclass
class BinaryStage:
    file: object
    sha256: str
    size: int
    observation: dict
    _release: object = field(default=lambda: None, repr=False)
    _retain_on_close_error: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    close_registered(self.file)
                except BaseException:
                    if not self._retain_on_close_error:
                        self._release()
                    raise
                else:
                    self._release()


def create_client(*, timeout_seconds):
    """Reuse the one-attempt same-origin transport, never its creation checks."""
    from app.services.managed_original_creation import create_client as transport

    return transport(timeout_seconds=timeout_seconds)


def _version(response):
    value = response.get("VersionId")
    if value is None or value == "null":
        return None
    if not _header(value):
        _reject()
    return value


def _observation(response, spec):
    if not isinstance(response, dict):
        _reject()
    metadata = response.get("ResponseMetadata")
    modified = response.get("LastModified")
    marker = response.get("DeleteMarker")
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("HTTPStatusCode")) is not int
        or metadata.get("HTTPStatusCode") != 200
        or response.get("Error") is not None
        or response.get("ContentRange") is not None
        or (marker is not None and (type(marker) is not bool or marker))
        or type(response.get("ContentLength")) is not int
        or response["ContentLength"] != spec.size_bytes
        or not _header(response.get("ETag"))
        or not isinstance(modified, datetime)
        or modified.utcoffset() is None
        or not _header(response.get("ContentType", "application/octet-stream"), 255)
    ):
        _reject()
    return {
        "kind": "s3",
        "bucket": spec.bucket,
        "key": spec.object_key,
        "size": spec.size_bytes,
        "etag": response["ETag"],
        "version_id": _version(response),
        "last_modified": modified.astimezone(timezone.utc).isoformat(),
        "content_type": response.get("ContentType", "application/octet-stream"),
    }


def _body(response):
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        # A malformed constructor result cannot prove whether its stream ended.
        raise mark_unsettled(ContentError("content_source_unavailable", 503))
    return body


def _conditions(client, params, etag, check):
    check()
    impossible = '"aap-impossible-etag"'
    if etag == impossible:
        impossible = '"aap-impossible-etag-2"'
    try:
        response = client.get_object(**params, IfMatch=impossible)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"PreconditionFailed", "412"} and status == 412:
            check()
            return
        _reject("content_conditions_unverified")
    else:
        close_registered(_body(response))
        _reject("content_conditions_unsupported")


def _failure(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return mark_unsettled(ContentError("storage_operation_unsettled", 503))
    if isinstance(exc, ContentError):
        return exc
    if isinstance(exc, FilesystemStorageError):
        if exc.code == "path_unavailable":
            return ContentError("content_source_missing", 404)
        if exc.code == "source_changed":
            return ContentError("source_changed", 409)
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"NoSuchKey", "NoSuchVersion", "NotFound", "404"}:
            return ContentError("content_source_missing", 404)
        if code in {"PreconditionFailed", "412"}:
            return ContentError("source_changed", 409)
    return ContentError("content_source_unavailable", 503)


def _capture(
    spec,
    max_bytes,
    timeout_seconds,
    deadline,
    stop,
    release,
    *,
    transport_timeout_seconds=None,
    retain_on_close_error=False,
    client_context=None,
):
    temporary, transferred, unsettled = None, False, False

    def check():
        if stop.is_set() or time.monotonic() >= deadline:
            _reject("content_read_timeout", 504)

    try:
        check()
        temporary = tempfile.TemporaryFile(mode="w+b", prefix="aap-original-")
        client, context = None, None
        try:
            try:
                if client_context is not None:
                    context = client_context()
                    client = context.__enter__()
                else:
                    client = create_client(
                        timeout_seconds=(
                            timeout_seconds
                            if transport_timeout_seconds is None
                            else transport_timeout_seconds
                        )
                    )
            except BaseException as exc:
                raise mark_unsettled(_failure(exc)) from None
            if client is None:
                raise mark_unsettled(ContentError("content_source_unavailable", 503))
            actual_endpoint = getattr(
                getattr(client, "meta", None), "endpoint_url", None
            )
            if (
                not isinstance(actual_endpoint, str)
                or actual_endpoint.rstrip("/") != spec.endpoint_url
            ):
                _reject("managed_original_endpoint_changed")
            check()
            params = {"Bucket": spec.bucket, "Key": spec.object_key}
            before = _observation(client.head_object(**params), spec)
            check()
            conditional = dict(params)
            if before["version_id"] is not None:
                conditional["VersionId"] = before["version_id"]
            _conditions(client, conditional, before["etag"], check)
            check()
            response = client.get_object(**conditional, IfMatch=before["etag"])
            body = _body(response)
            try:
                check()
                if _observation(response, spec) != before or not callable(
                    getattr(body, "read", None)
                ):
                    _reject()
                digest, size = hashlib.sha256(), 0
                while True:
                    check()
                    requested = min(CHUNK_BYTES, spec.size_bytes - size + 1)
                    chunk = body.read(requested)
                    check()
                    if type(chunk) is not bytes or len(chunk) > requested:
                        _reject()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > spec.size_bytes or size > max_bytes:
                        _reject()
                    if temporary.write(chunk) != len(chunk):
                        _reject("content_source_unavailable", 503)
                    digest.update(chunk)
                if size != spec.size_bytes:
                    _reject()
                if digest.hexdigest() != spec.sha256:
                    _reject("content_mismatch")
            finally:
                close_registered(body)
            check()
            if _observation(client.head_object(**params), spec) != before:
                _reject()
            check()
        finally:
            if client is not None:
                if context is not None:
                    context.__exit__(*sys.exc_info())
                else:
                    close_registered(client)
        check()
        temporary.seek(0)
        stage = BinaryStage(
            temporary,
            spec.sha256,
            spec.size_bytes,
            before,
            _release=release,
            _retain_on_close_error=retain_on_close_error,
        )
        transferred = True
        return stage
    except BaseException as exc:
        unsettled = getattr(exc, "storage_operation_unsettled", False)
        if isinstance(exc, Exception):
            raise _failure(exc) from None
        raise
    finally:
        if not transferred:
            # Closing the local temporary file does not settle an unknown
            # provider constructor or remote handle. Explicit pools retain that
            # responsibility even when this last local close succeeds.
            settled = not (retain_on_close_error and unsettled)
            try:
                if temporary is not None:
                    try:
                        close_registered(temporary)
                    except BaseException as exc:
                        settled = not retain_on_close_error
                        raise _failure(exc) from None
            finally:
                if settled:
                    release()


async def capture(
    spec,
    *,
    max_bytes,
    timeout_seconds,
    admission=None,
    transport_timeout_seconds=None,
    client_context=None,
):
    """Return a full verified file; cancellation drains every real I/O handle."""
    try:
        finite_timeout = math.isfinite(timeout_seconds)
    except (OverflowError, TypeError, ValueError):
        finite_timeout = False
    if transport_timeout_seconds is not None:
        try:
            valid_transport_timeout = (
                not isinstance(transport_timeout_seconds, bool)
                and isinstance(transport_timeout_seconds, (int, float))
                and math.isfinite(transport_timeout_seconds)
                and 0 < transport_timeout_seconds <= timeout_seconds
            )
        except (OverflowError, TypeError, ValueError):
            valid_transport_timeout = False
    else:
        valid_transport_timeout = True
    if (
        not isinstance(spec, ObjectSpec)
        or type(max_bytes) is not int
        or not 0 <= max_bytes <= 2**63 - 1
        or isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not finite_timeout
        or timeout_seconds <= 0
        or not valid_transport_timeout
        or (client_context is not None and not callable(client_context))
        or (admission is not None and not isinstance(admission, threading.Semaphore))
    ):
        _reject("managed_original_read_invalid")
    if spec.size_bytes > max_bytes:
        _reject("content_too_large", 413)
    slots = _slots if admission is None else admission
    if not slots.acquire(blocking=False):
        _reject("snapshot_capacity_exceeded", 429)
    stop = threading.Event()
    deadline = time.monotonic() + timeout_seconds
    try:
        context = contextvars.copy_context()
        options = {}
        if client_context is not None:
            options["client_context"] = client_context
        if admission is not None:
            options["retain_on_close_error"] = True
        if transport_timeout_seconds is not None:
            options["transport_timeout_seconds"] = transport_timeout_seconds
        pending = asyncio.get_running_loop().run_in_executor(
            None,
            context.run,
            partial(
                _capture,
                spec,
                max_bytes,
                timeout_seconds,
                deadline,
                stop,
                slots.release,
                **options,
            ),
        )
    except BaseException:
        slots.release()
        raise
    try:
        return await asyncio.wait_for(asyncio.shield(pending), timeout_seconds)
    except (TimeoutError, asyncio.CancelledError) as exc:
        stop.set()
        cancelled = isinstance(exc, asyncio.CancelledError)
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        unsettled = False
        try:
            stage = pending.result()
            if admission is None:
                stage.close()
            else:
                try:
                    closing = asyncio.get_running_loop().run_in_executor(
                        None, stage.close
                    )
                except BaseException as close_error:
                    raise mark_unsettled(close_error)
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        cancelled = True
                    except BaseException:
                        break
                closing.result()
        except BaseException as late:
            unsettled = getattr(late, "storage_operation_unsettled", False)
        error = (
            asyncio.CancelledError()
            if cancelled
            else ContentError("content_read_timeout", 504)
        )
        if unsettled:
            mark_unsettled(error)
        raise error from None
