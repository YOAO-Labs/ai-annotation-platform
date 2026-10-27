"""Conditional managed-original writes with version-pinned readback.

The owner reserves the immutable locator before this class touches bytes. Every
write uses one nonretrying client, ``If-None-Match: *`` completion and a full
re-read of the returned version; a failed or unverified object is never
published as a dataset item.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import mimetypes
from typing import Any, Awaitable, BinaryIO, Callable

from aap_assets_images.contract import ObjectIdentity
from aap_assets_source import managed_original
from aap_assets_source.connectors import (
    SourceChangedError,
    SourceNotFoundError,
    SourcePathError,
)
from aap_assets_source.connectors.crypto import (
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
    decrypt_secret,
)
from aap_assets_source.import_contract import ImportEntry
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)

from .config import ImportConfig


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
        self._config = config
        self._head_bytes = config.image_header_bytes
        self._io_timeout = config.s3_read_timeout_seconds
        self._client = client if client is not None else _create_client(config)

    @contextmanager
    def _client_scope(self, target: ManagedOriginalMaterial):
        """Own connection-backed clients for exactly one physical operation."""
        if target.destination is None:
            if target.bucket != self.bucket:
                raise DestinationWriteError("destination target is not configured")
            # The caller owns the platform client for its whole process.
            yield self._client
            return
        client = self._build_client(target)
        try:
            yield client
        except BaseException as exc:
            try:
                _close_client(client)
            except DestinationWriteError:
                try:
                    exc.storage_operation_unsettled = True
                except Exception:
                    pass
            raise
        else:
            _close_client(client)

    def _build_client(self, target: ManagedOriginalMaterial) -> Any:
        """Build a single-attempt client for exactly one connection-backed write."""
        config = target.destination.get("config")
        secret_enc = target.destination.get("secret_enc")
        if not isinstance(config, dict) or not isinstance(secret_enc, str):
            raise DestinationWriteError("destination credentials are malformed")
        try:
            secret = decrypt_secret(secret_enc, self._config.connector_encryption_key)
            access_key = secret.get("access_key")
            secret_key = secret.get("secret_key")
            if (
                not isinstance(access_key, str)
                or not access_key
                or not isinstance(secret_key, str)
                or not secret_key
            ):
                raise DestinationWriteError("destination credentials are malformed")
        except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
            raise DestinationWriteError(
                "destination credentials are unavailable"
            ) from None
        return _create_client(
            self._config,
            endpoint_url=target.endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=str(config.get("region") or self._config.s3_region),
        )

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
    ) -> TransferResult:
        with self._client_scope(target) as client:
            return await self._transfer_with(
                client,
                stream,
                entry,
                target=target,
                byte_budget=byte_budget,
                before_io=before_io,
                on_bytes=on_bytes,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )

    async def _transfer_with(
        self,
        client: Any,
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
    ) -> TransferResult:
        """Prepare, conditionally complete and fully read back one original."""
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
        counted = _CountingStream(stream, on_bytes=on_bytes)
        try:
            prepared = await _thread(
                managed_original.prepare_stream,
                client,
                target.bucket,
                target.object_key,
                counted,
                content_type=content_type,
                max_bytes=min(max_bytes, max(entry.source.size, 0)),
                timeout_seconds=self._io_timeout,
                _check=_deadline(before_io, self._io_timeout),
            )
            if on_prepared is not None:
                await on_prepared(prepared)
            before_io()
            if on_writing is not None:
                await on_writing()
            receipt = await _thread(
                managed_original.complete,
                client,
                target.bucket,
                target.object_key,
                prepared,
                content_type=content_type,
            )
            if on_written is not None:
                await on_written(receipt)
            verified = await _thread(
                managed_original.verify,
                client,
                target.bucket,
                target.object_key,
                receipt,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                max_bytes=max_bytes,
                timeout_seconds=self._io_timeout,
            )
        except managed_original.WriterError as exc:
            raise _writer_failure(exc) from None
        if prepared.size_bytes != entry.source.size:
            raise SourceChangedError("source size changed after enumeration")
        dimensions = (
            await asyncio.to_thread(_image_dimensions, verified.head_bytes)
            if entry.file_type == "image"
            else None
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
            verification={
                **verified.evidence(),
                "etag": receipt.etag,
                "version_id": receipt.version_id,
            },
            observed={"observation": verified.observation},
        )
        return TransferResult(
            proof=proof,
            file_size=prepared.size_bytes,
            content_hash=prepared.md5,
            dimensions=dimensions,
            object_identity=object_identity,
        )

    async def verify(
        self,
        target: ManagedOriginalMaterial,
        *,
        sha256: str,
        size_bytes: int,
    ) -> Any:
        """Read back the exact saved version of an already verified creation."""
        if target.etag is None:
            raise DestinationWriteError("destination returned an invalid write receipt")
        with self._client_scope(target) as client:
            try:
                verified = await _thread(
                    managed_original.verify,
                    client,
                    target.bucket,
                    target.object_key,
                    managed_original.WriteReceipt(
                        etag=target.etag,
                        version_id=target.version_id,
                        request_id=None,
                        response={},
                    ),
                    sha256=sha256,
                    size_bytes=size_bytes,
                    max_bytes=max(size_bytes, 1),
                    timeout_seconds=self._io_timeout,
                )
            except managed_original.WriterError as exc:
                raise _writer_failure(exc) from None
        return verified


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

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
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
    return await asyncio.shield(task)


def _close_client(client: Any) -> None:
    """A failed close is not proof that the client stopped owning bytes."""
    try:
        client.close()
    except BaseException:
        error = DestinationWriteError("destination_close_failed")
        error.storage_operation_unsettled = True
        raise error from None


def _object_identity_from_verified(
    verified: managed_original.Verified, *, byte_size: int
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


def _object_identity_from_write_response(
    response: Any, *, byte_size: int
) -> ObjectIdentity:
    """Validate the exact object receipt returned by the successful S3 write."""
    if not isinstance(response, dict):
        raise DestinationWriteError("destination returned an invalid write receipt")
    etag = response.get("ETag")
    version_id = response.get("VersionId")
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
        return ObjectIdentity(
            etag=etag,
            byte_size=byte_size,
            version_id=version_id,
        )
    except (TypeError, ValueError):
        raise DestinationWriteError(
            "destination returned an invalid write receipt"
        ) from None


def _create_client(
    config: ImportConfig,
    *,
    endpoint_url: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    region: str | None = None,
) -> Any:
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or config.s3_endpoint,
        aws_access_key_id=access_key or config.s3_access_key,
        aws_secret_access_key=secret_key or config.s3_secret_key,
        region_name=region or config.s3_region,
        config=BotoConfig(
            connect_timeout=config.s3_connect_timeout_seconds,
            read_timeout=config.s3_read_timeout_seconds,
            # One durable intent represents one submission, including when the
            # provider loses its response. Never let the SDK repeat completion.
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
        ),
    )


def _md5():
    import hashlib

    return hashlib.md5(usedforsecurity=False)


def _empty_md5() -> str:
    return _md5().hexdigest()


def _multipart_part_bytes(size_limit: int, configured_part_bytes: int) -> int:
    """Use enough bytes per part to stay below S3's 10,000-part ceiling."""
    mib = 1024 * 1024
    minimum_part = 5 * mib
    maximum_part = 64 * mib
    required = max(minimum_part, configured_part_bytes, (size_limit + 9_999) // 10_000)
    rounded = ((required + mib - 1) // mib) * mib
    if rounded > maximum_part:
        raise DestinationWriteError("object exceeds supported multipart size")
    return rounded


async def _read_part(
    stream: BinaryIO,
    part_bytes: int,
    *,
    timeout: float,
    before_io: Callable[[], None],
    on_bytes: Callable[[int], None],
) -> bytes:
    """Aggregate legal short reads into a service-sized part or the final tail."""
    data = bytearray()
    while len(data) < part_bytes:
        before_io()
        requested = part_bytes - len(data)
        try:
            chunk = await asyncio.wait_for(
                asyncio.to_thread(stream.read, requested), timeout=timeout
            )
        except (SourceChangedError, SourceNotFoundError, SourcePathError):
            raise
        except Exception as exc:
            raise SourceReadError("source read failed") from exc
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise SourceReadError("source returned a non-byte chunk")
        if len(chunk) > requested:
            raise SourceReadError("source returned more bytes than requested")
        data.extend(chunk)
        on_bytes(len(chunk))
    return bytes(data)


def _image_dimensions(head: bytes) -> tuple[int, int] | None:
    if not head:
        return None
    try:
        from PIL import Image

        with Image.open(BytesIO(head)) as image:
            width, height = int(image.width), int(image.height)
            try:
                orientation = int(image.getexif().get(274, 1))
            except Exception:
                orientation = 1
            if width < 1 or height < 1:
                return None
            return (height, width) if orientation in {5, 6, 7, 8} else (width, height)
    except Exception:
        return None
