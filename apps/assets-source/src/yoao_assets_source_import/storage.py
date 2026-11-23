"""Builtin S3 original writes with independent Source verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import mimetypes
from typing import Any, Awaitable, BinaryIO, Callable

from yoao_assets_images.contract import ObjectIdentity
from yoao_assets_source import managed_original
from yoao_assets_source.connectors import SourceChangedError
from yoao_assets_source.import_contract import ImportEntry
from yoao_assets_source.import_verification_contract import ImportVerificationResult
from yoao_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)
from yoao_assets_source.saved_io import SavedIOError

from .config import ImportConfig
from .verification import ExpectedOriginal


_MAX_MULTIPART_BYTES = 64 * 1024 * 1024 * managed_original.MAX_PARTS


class DestinationWriteError(OSError):
    """A destination write failed; safe to record as one item-level error."""


class SourceReadError(OSError):
    """Reading the already opened source object failed."""


@dataclass(frozen=True)
class TransferResult:
    file_size: int
    content_hash: str
    dimensions: tuple[int, int] | None
    object_identity: ObjectIdentity | None
    proof: ManagedOriginalProof | None = None


class DestinationStorage:
    """Write exactly one owner-allocated key with conditional creation."""

    def __init__(self, config: ImportConfig, *, client: Any | None = None) -> None:
        self.bucket = config.datasets_bucket
        self._io_timeout = config.s3_read_timeout_seconds
        self._client = client if client is not None else _create_client(config)

    async def transfer(
        self,
        stream: BinaryIO,
        entry: ImportEntry,
        *,
        target: ManagedOriginalMaterial,
        byte_budget: int,
        before_io: Callable[[], None],
        on_bytes: Callable[[int], None],
        on_prepared: Callable[[Any], Awaitable[None]] | None = None,
        on_writing: Callable[[], Awaitable[None]] | None = None,
        on_written: Callable[[Any], Awaitable[None]] | None = None,
        on_verify: Callable[[ExpectedOriginal], Awaitable[ImportVerificationResult]],
    ) -> TransferResult:
        """Prepare, conditionally complete and fully read back one builtin original."""
        if target.connection_id is not None or target.bucket != self.bucket:
            raise DestinationWriteError("destination target is not builtin")
        if entry.file_path != target.file_path:
            raise DestinationWriteError("destination target scope changed")
        content_type = (
            mimetypes.guess_type(entry.file_name)[0] or "application/octet-stream"
        )
        max_bytes = min(target.max_bytes, byte_budget)
        if entry.source.size > max_bytes:
            raise SourceChangedError(
                "source object exceeds the remaining import budget"
            )
        if entry.source.size > _MAX_MULTIPART_BYTES:
            raise DestinationWriteError("object exceeds supported multipart size")
        counted = _CountingStream(stream, on_bytes=on_bytes)
        try:
            prepared = await _thread(
                managed_original.prepare_stream,
                self._client,
                target.bucket,
                target.object_key,
                counted,
                content_type=content_type,
                max_bytes=entry.source.size,
                timeout_seconds=self._io_timeout,
                _check=_deadline(before_io, self._io_timeout),
            )
            try:
                if prepared.size_bytes != entry.source.size:
                    raise SourceChangedError("source size changed after enumeration")
                if on_prepared is not None:
                    await on_prepared(prepared)
                before_io()
                if on_writing is not None:
                    await on_writing()
            except BaseException:
                await _thread(
                    managed_original.abort_prepared,
                    self._client,
                    target.bucket,
                    target.object_key,
                    prepared,
                )
                raise
            receipt = await _thread(
                managed_original.complete,
                self._client,
                target.bucket,
                target.object_key,
                prepared,
                content_type=content_type,
            )
            if on_written is not None:
                await on_written(receipt)
            verified = await on_verify(
                ExpectedOriginal(
                    sha256=prepared.sha256,
                    md5=prepared.md5,
                    size_bytes=prepared.size_bytes,
                    etag=receipt.etag,
                    version_id=receipt.version_id,
                )
            )
        except managed_original.WriterError as exc:
            if counted.failure is not None:
                counted.failure.storage_operation_unsettled |= exc.outcome == "unknown"
                raise counted.failure from None
            raise _writer_failure(exc) from None
        dimensions = (
            (verified.width, verified.height) if verified.width is not None else None
        )
        object_identity = None
        if entry.file_type == "image":
            object_identity = _object_identity_from_verified(
                verified, byte_size=prepared.size_bytes
            )
        proof = ManagedOriginalProof.verified(
            mode=target.mode,
            sha256=prepared.sha256,
            md5=prepared.md5,
            size_bytes=prepared.size_bytes,
            etag=receipt.etag,
            version_id=receipt.version_id,
            request_id=receipt.request_id,
            prepared=prepared.evidence(),
            verification=verified.model_dump(mode="json"),
            observed={"observation": verified.observation},
        )
        return TransferResult(
            proof=proof,
            file_size=prepared.size_bytes,
            content_hash=prepared.md5,
            dimensions=dimensions,
            object_identity=object_identity,
        )


def _writer_failure(exc: managed_original.WriterError) -> DestinationWriteError:
    error = DestinationWriteError(exc.code)
    error.outcome = exc.outcome
    if exc.outcome == "unknown":
        error.storage_operation_unsettled = True
    return error


class _CountingStream:
    """Preserve the caller's read semantics while observing consumed bytes."""

    def __init__(self, stream: BinaryIO, *, on_bytes: Callable[[int], None]) -> None:
        self._stream = stream
        self._on_bytes = on_bytes
        self.failure: SavedIOError | None = None

    def read(self, size: int = -1) -> bytes:
        try:
            chunk = self._stream.read(size)
        except SavedIOError as exc:
            self.failure = exc
            raise
        if isinstance(chunk, bytes) and chunk:
            self._on_bytes(len(chunk))
        return chunk


def _deadline(before_io: Callable[[], None], timeout: float):
    import time

    deadline = time.monotonic() + max(0.1, float(timeout))

    def check() -> None:
        before_io()
        if time.monotonic() >= deadline:
            raise DestinationWriteError("destination transfer timed out")

    return check


async def _thread(function, /, *args, **kwargs):
    """Keep one attempt alive past caller cancellation while it owns bytes."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def _object_identity_from_verified(
    verified: ImportVerificationResult, *, byte_size: int
) -> ObjectIdentity:
    etag = verified.observation.get("etag")
    version_id = verified.observation.get("version_id")
    if not isinstance(etag, str):
        raise DestinationWriteError("destination returned an invalid write receipt")
    etag = etag.strip().strip('"')
    if (
        not etag
        or len(etag) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in etag)
    ):
        raise DestinationWriteError("destination returned an invalid write receipt")
    if version_id is not None and (
        not isinstance(version_id, str)
        or not version_id
        or len(version_id) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in version_id)
    ):
        raise DestinationWriteError("destination returned an invalid write receipt")
    try:
        return ObjectIdentity(etag=etag, byte_size=byte_size, version_id=version_id)
    except (TypeError, ValueError):
        raise DestinationWriteError(
            "destination returned an invalid write receipt"
        ) from None


def _create_client(config: ImportConfig) -> Any:
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=config.s3_access_key,
        aws_secret_access_key=config.s3_secret_key,
        region_name=config.s3_region,
        config=BotoConfig(
            connect_timeout=config.s3_connect_timeout_seconds,
            read_timeout=config.s3_read_timeout_seconds,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
        ),
    )
