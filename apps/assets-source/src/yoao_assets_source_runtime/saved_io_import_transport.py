"""Keep an accepted import's source identity pinned through EOF and provider close."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
import hashlib
import json
import time

from botocore.exceptions import ClientError
from pydantic import ValidationError

from yoao_assets_source.connectors import (
    ConnectorHostDenied,
    SourceChangedError,
    SourceNotFoundError,
    SourcePathError,
)
from yoao_assets_source.connectors.base import SourceObject, matches_include_globs
from yoao_assets_source.connectors.s3 import (
    S3CompatibleSource,
    _is_not_found,
    _is_precondition_failed,
    _join_under_base,
    _under_prefix,
)
from yoao_assets_source.connectors.sftp import SftpSource
from yoao_assets_source.import_contract import ImportSourceObject, ImportState
from yoao_assets_source.saved_io import CHUNK_BYTES, SavedIOError

MAX_MANIFEST_BYTES = 16 * 1024**2


def source_error(exc: Exception, *, listing: bool = False) -> SavedIOError:
    if isinstance(exc, SavedIOError):
        return exc
    if getattr(exc, "storage_operation_unsettled", False):
        return SavedIOError("source_close_unsettled", status_code=503, unsettled=True)
    if isinstance(exc, SourceChangedError):
        return SavedIOError("source_changed")
    if isinstance(exc, SourceNotFoundError):
        return SavedIOError("source_not_found", status_code=404)
    if isinstance(exc, ConnectorHostDenied):
        return SavedIOError("connection_changed")
    if isinstance(exc, SourcePathError):
        return SavedIOError("invalid_request" if listing else "invalid_source_object")
    return SavedIOError(
        "storage_unavailable" if listing else "source_read_failed", status_code=503
    )


class TransferGuard:
    """Recheck the durable fence during native streaming without holding SQL locks."""

    def __init__(self, proof, timeout: float) -> None:
        self._loop = asyncio.get_running_loop()
        self._proof = proof
        self._deadline = time.monotonic() + timeout
        self._next_check = time.monotonic() + 1
        self.error: SavedIOError | None = None

    def __call__(self) -> None:
        now = time.monotonic()
        self._check_deadline(now)
        if now < self._next_check:
            return
        task = asyncio.run_coroutine_threadsafe(self._proof(), self._loop)
        try:
            task.result(timeout=self._deadline - now)
        except FutureTimeout:
            task.cancel()
            self._check_deadline(self._deadline)
        except SavedIOError as exc:
            self.error = exc
            raise
        self._next_check = time.monotonic() + 1
        self._check_deadline(time.monotonic())

    def _check_deadline(self, now: float) -> None:
        if now >= self._deadline:
            self.error = SavedIOError("storage_unavailable", status_code=503)
            raise self.error

    def deadline(self):
        self._check_deadline(time.monotonic())


def collect_manifest(adapter, state: ImportState, check: Callable[[], None]):
    limits = state.settings_snapshot
    objects = []
    seen: set[str] = set()
    payload = bytearray(b"[")
    total = 0
    listing = (
        _list_s3(adapter, state, check)
        if isinstance(adapter, S3CompatibleSource)
        else adapter.list(state.source_path, state.recursive, state.include_globs)
    )
    for source in listing:
        check()
        if len(objects) >= limits.dataset_import_max_files:
            raise SavedIOError("invalid_request", status_code=413)
        try:
            value = ImportSourceObject(
                relpath=source.relpath,
                size=source.size,
                mtime=source.mtime,
                etag=source.etag,
            ).model_dump(mode="json")
        except ValidationError:
            raise SavedIOError("invalid_response", status_code=502) from None
        if source.relpath in seen:
            raise SavedIOError("invalid_response", status_code=502)
        seen.add(source.relpath)
        total += source.size
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if total > limits.dataset_import_max_total_bytes or (
            len(payload) + bool(objects) + len(encoded) + 1 > MAX_MANIFEST_BYTES
        ):
            raise SavedIOError("invalid_request", status_code=413)
        if objects:
            payload.extend(b",")
        payload.extend(encoded)
        objects.append(value)
    check()
    payload.extend(b"]")
    return objects, bytes(payload)


def _list_s3(adapter, state, check):
    prefix = _join_under_base(adapter.base_prefix, state.source_path)
    params = {"Bucket": adapter.bucket, "Prefix": prefix}
    while True:
        check()
        reply = adapter.client.list_objects_v2(**params)
        check()
        for obj in reply.get("Contents", ()):
            key = obj.get("Key")
            if not isinstance(key, str) or not key or key.endswith("/"):
                continue
            if not _under_prefix(prefix, key):
                continue
            below = key[len(prefix) :].lstrip("/") if prefix else key
            if not state.recursive and "/" in below:
                continue
            path = key[len(adapter.base_prefix) + 1 :] if adapter.base_prefix else key
            if not path or not matches_include_globs(path, state.include_globs):
                continue
            tag = obj.get("ETag")
            yield SourceObject(
                path,
                obj.get("Size"),
                obj.get("LastModified"),
                (tag.strip('"') or None) if isinstance(tag, str) else None,
            )
        if not reply.get("IsTruncated"):
            return
        token = reply.get("NextContinuationToken")
        if (
            not isinstance(token, str)
            or not token
            or token == params.get("ContinuationToken")
        ):
            raise SavedIOError("invalid_response", status_code=502)
        params["ContinuationToken"] = token


def open_observed(
    adapter,
    observed: ImportSourceObject,
    path: str,
    check: TransferGuard,
    *,
    digest: bool,
):
    check()
    if not observed.etag:
        raise SourceChangedError("Missing source observation")
    if isinstance(adapter, S3CompatibleSource):
        stream, verify = _open_s3(adapter, observed, path)
    else:
        if isinstance(adapter, SftpSource):
            root = adapter._candidate_for_source_path(path)
            exact = adapter._safe_file_path(observed.relpath)
            if not exact.startswith(root.rstrip("/") + "/"):
                raise SourcePathError("Saved object outside the accepted directory")
        stream = adapter.open(observed.relpath, etag=observed.etag)
        verify = None
    return _BoundSource(stream, observed.size, check, verify, digest=digest)


def _s3_identity(reply):
    tag = reply.get("ETag")
    modified = reply.get("LastModified")
    version = reply.get("VersionId")
    if (
        reply.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or reply.get("DeleteMarker")
        or not isinstance(tag, str)
        or not tag.strip('"')
        or type(reply.get("ContentLength")) is not int
        or not isinstance(modified, datetime)
        or modified.utcoffset() is None
        or version is not None
        and (not isinstance(version, str) or not version)
    ):
        raise SourceChangedError("Invalid source observation")
    return (
        tag.strip('"'),
        reply["ContentLength"],
        modified.replace(microsecond=0),
        None if version == "null" else version,
    )


def _open_s3(adapter, observed, path):
    key = (
        f"{adapter.base_prefix}/{observed.relpath}"
        if adapter.base_prefix
        else observed.relpath
    )
    if not _under_prefix(_join_under_base(adapter.base_prefix, path), key):
        raise SourcePathError("Saved object outside the accepted directory")
    params = {"Bucket": adapter.bucket, "Key": key}

    def request(method, **condition):
        try:
            return method(**params, **condition)
        except ClientError as exc:
            if (
                _is_not_found(exc)
                or exc.response.get("Error", {}).get("Code") == "NoSuchVersion"
            ):
                raise SourceNotFoundError("Saved object is missing") from None
            if _is_precondition_failed(exc):
                raise SourceChangedError("Saved object changed") from None
            raise

    before = _s3_identity(request(adapter.client.head_object))
    if (
        before[:2] != (observed.etag.strip('"'), observed.size)
        or observed.mtime is not None
        and before[2] != observed.mtime.replace(microsecond=0)
    ):
        raise SourceChangedError("Saved object changed")
    condition = {"IfMatch": f'"{before[0]}"'}
    if before[3] is not None:
        condition["VersionId"] = before[3]
    reply = request(adapter.client.get_object, **condition)
    stream = reply.get("Body")
    try:
        if stream is None or _s3_identity(reply) != before:
            raise SourceChangedError("Saved object changed while opening")
    except BaseException:
        if stream is not None:
            _close(stream)
        raise

    def verify():
        if _s3_identity(request(adapter.client.head_object)) != before:
            raise SourceChangedError("Saved object changed while reading")

    return stream, verify


def _close(stream):
    try:
        stream.close()
    except Exception:
        raise SavedIOError(
            "source_close_unsettled", status_code=503, unsettled=True
        ) from None


class _BoundSource:
    def __init__(self, stream, length, check, verify, *, digest):
        self._stream = stream
        self._remaining = length
        self._check = check
        self._verify = verify
        self._sha256 = hashlib.sha256() if digest else None
        self._md5 = hashlib.md5(usedforsecurity=False) if digest else None
        self.error = None

    def read(self, size):
        try:
            self._check()
            bound = min(size, self._remaining + 1, CHUNK_BYTES)
            chunk = self._stream.read(bound)
            if not isinstance(chunk, bytes) or len(chunk) > min(bound, self._remaining):
                raise SourceChangedError("Source exceeded its listed length")
            self._remaining -= len(chunk)
            if chunk and self._sha256 is not None:
                self._sha256.update(chunk)
                self._md5.update(chunk)
            if not chunk:
                if self._remaining:
                    raise SourceChangedError("Source ended before its listed length")
                if self._verify is not None:
                    self._verify()
                    self._verify = None
            self._check()
            return chunk
        except Exception as exc:
            self.error = source_error(exc)
            raise self.error from None

    def evidence(self):
        return {"sha256": self._sha256.hexdigest(), "md5": self._md5.hexdigest()}

    def close(self):
        _close(self._stream)
