"""Streaming destination writes with a fixed bucket and write-only key scope."""

from __future__ import annotations

import asyncio
from io import BytesIO
import mimetypes
from typing import Any, BinaryIO, Callable

from aap_assets_source.import_contract import ImportEntry
from aap_assets_source_runtime.image_admission import object_identity_from_head
from aap_assets_images.contract import ImageFailure, ObjectIdentity
from aap_assets_source.connectors import (
    SourceChangedError,
    SourceNotFoundError,
    SourcePathError,
)

from .config import ImportConfig


class DestinationWriteError(OSError):
    """A destination write failed; safe to record as one item-level error."""


class SourceReadError(OSError):
    """Reading the already opened source object failed."""


class DestinationStorage:
    """Write to exactly one configured bucket; this class has no read/delete API."""

    def __init__(self, config: ImportConfig, *, client: Any | None = None) -> None:
        self.bucket = config.datasets_bucket
        self._part_bytes = config.part_bytes
        self._head_bytes = config.image_header_bytes
        self._io_timeout = config.s3_read_timeout_seconds
        self._client = client if client is not None else _create_client(config)

    async def transfer(
        self,
        stream: BinaryIO,
        entry: ImportEntry,
        *,
        byte_budget: int,
        before_io: Callable[[], None],
        on_bytes: Callable[[int], None],
    ) -> tuple[int, str, tuple[int, int] | None]:
        """Stream one source through bounded multipart parts and return MD5 metadata."""
        content_type = (
            mimetypes.guess_type(entry.file_name)[0] or "application/octet-stream"
        )
        if entry.source.size == 0:
            before_io()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.put_object,
                        Bucket=self.bucket,
                        Key=entry.file_path,
                        Body=b"",
                        ContentType=content_type,
                    ),
                    timeout=self._io_timeout,
                )
            except Exception as exc:
                raise DestinationWriteError("empty object write failed") from exc
            return 0, _empty_md5(), None

        if byte_budget < entry.source.size:
            raise SourceChangedError(
                "source object exceeds the remaining import budget"
            )
        part_bytes = _multipart_part_bytes(
            min(byte_budget, entry.source.size), self._part_bytes
        )

        before_io()
        try:
            created = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.create_multipart_upload,
                    Bucket=self.bucket,
                    Key=entry.file_path,
                    ContentType=content_type,
                ),
                timeout=self._io_timeout,
            )
        except Exception as exc:
            raise DestinationWriteError("multipart upload could not start") from exc
        upload_id = created.get("UploadId") if isinstance(created, dict) else None
        if not isinstance(upload_id, str) or not upload_id:
            raise DestinationWriteError("destination returned no upload id")

        parts: list[dict[str, Any]] = []
        digest = _md5()
        head = bytearray()
        total = 0
        completed = False
        try:
            part_number = 1
            while True:
                before_io()
                try:
                    chunk = await _read_part(
                        stream,
                        part_bytes,
                        timeout=self._io_timeout,
                        before_io=before_io,
                        on_bytes=on_bytes,
                    )
                except (SourceChangedError, SourceNotFoundError, SourcePathError):
                    raise
                except Exception as exc:
                    raise SourceReadError("source read failed") from exc
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise SourceReadError("source returned a non-byte chunk")

                total += len(chunk)
                if total > entry.source.size or total > byte_budget:
                    raise SourceChangedError("source size exceeded its accepted bound")
                digest.update(chunk)
                if len(head) < self._head_bytes:
                    head.extend(chunk[: self._head_bytes - len(head)])

                before_io()
                try:
                    part = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._client.upload_part,
                            Bucket=self.bucket,
                            Key=entry.file_path,
                            PartNumber=part_number,
                            UploadId=upload_id,
                            Body=chunk,
                        ),
                        timeout=self._io_timeout,
                    )
                except Exception as exc:
                    raise DestinationWriteError("multipart part write failed") from exc
                etag = part.get("ETag") if isinstance(part, dict) else None
                if not isinstance(etag, str) or not etag:
                    raise DestinationWriteError("destination returned no part receipt")
                parts.append({"ETag": etag, "PartNumber": part_number})
                part_number += 1

            if total != entry.source.size:
                raise SourceChangedError("source size changed after enumeration")
            before_io()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self._client.complete_multipart_upload,
                        Bucket=self.bucket,
                        Key=entry.file_path,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    ),
                    timeout=self._io_timeout,
                )
            except Exception as exc:
                raise DestinationWriteError(
                    "multipart upload could not complete"
                ) from exc
            completed = True
            dimensions = (
                await asyncio.to_thread(_image_dimensions, bytes(head))
                if entry.file_type == "image"
                else None
            )
            return total, digest.hexdigest(), dimensions
        finally:
            if not completed:
                try:
                    await asyncio.shield(
                        asyncio.wait_for(
                            asyncio.to_thread(
                                self._client.abort_multipart_upload,
                                Bucket=self.bucket,
                                Key=entry.file_path,
                                UploadId=upload_id,
                            ),
                            timeout=self._io_timeout,
                        )
                    )
                except Exception:
                    pass

    async def head_image_identity(
        self, key: str, *, expected_size: int
    ) -> ObjectIdentity:
        """Capture identity only after the final immutable destination object exists."""
        try:
            head = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.head_object,
                    Bucket=self.bucket,
                    Key=key,
                ),
                timeout=self._io_timeout,
            )
        except Exception as exc:
            raise DestinationWriteError(
                "published image could not be verified"
            ) from exc
        try:
            return object_identity_from_head(head, expected_size=expected_size)
        except ImageFailure as exc:
            if exc.code == "source_changed":
                raise SourceChangedError("published image size changed") from None
            raise DestinationWriteError("published image identity is invalid") from None


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
            retries={"max_attempts": 2, "mode": "standard"},
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
