"""Bounded S3 reads and immutable publication for synchronous ingestion."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
import re
from typing import Any, AsyncIterator, BinaryIO

from botocore.exceptions import ClientError
from aap_assets_images.contract import ImageFailure, ObjectIdentity

from aap_assets_source.ingest_contract import IngestEntryErrorCode, IngestFailure
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
    ManagedOriginalProof,
)
from .image_admission import object_identity_from_head
from .ingest_storage_errors import ManagedStorageError
from .managed_storage import ManagedStorage

logger = logging.getLogger("aap_assets_source_runtime.ingest_storage")

_PART_BYTES = 8 * 1024 * 1024
_IMAGE_HEADER_BYTES = 256 * 1024
_MD5_ETAG = re.compile(r"^[0-9a-fA-F]{32}$")
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
_CHANGED_CODES = {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}


class IngestEntryFailure(Exception):
    """A safe per-file outcome; details from S3 or ZIP readers never escape."""

    def __init__(self, code: IngestEntryErrorCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IngestStorageObject:
    key: str
    size: int | None
    etag: str | None


@dataclass(frozen=True, slots=True)
class IngestStorageFacts:
    file_size: int
    content_hash: str | None
    width: int | None
    height: int | None
    object_identity: ObjectIdentity | None = None


async def run_in_thread(function, /, *args, **kwargs):
    """Wait for blocking work to stop using request-owned resources on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(task)
        except Exception:
            pass
        raise


class IngestStorage:
    """Use one explicit S3 client and bucket for legacy reads and new writes."""

    def __init__(
        self,
        client: Any,
        datasets_bucket: str,
        *,
        managed: ManagedStorage | None = None,
    ) -> None:
        if not datasets_bucket:
            raise ValueError("datasets_bucket must not be empty")
        self._client = client
        self.datasets_bucket = datasets_bucket
        self.managed = managed or ManagedStorage(client)

    async def iter_objects(self, prefix: str) -> AsyncIterator[IngestStorageObject]:
        """Yield one listed object at a time while retaining at most one S3 page."""
        if not prefix or not prefix.endswith("/"):
            raise ValueError("scan prefix must end with a slash")
        iterator = self._listed_objects(prefix)
        while True:
            try:
                item = await run_in_thread(_next_or_none, iterator)
            except Exception as exc:  # noqa: BLE001 - listing failure aborts the whole scan.
                logger.warning("Source object listing failed (%s)", type(exc).__name__)
                raise IngestFailure("storage_unavailable") from None
            if item is None:
                return
            yield item

    def _listed_objects(self, prefix: str):
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.datasets_bucket, Prefix=prefix):
            for item in page.get("Contents", ()):
                key = item.get("Key")
                if not isinstance(key, str):
                    continue
                size = item.get("Size")
                yield IngestStorageObject(
                    key=key,
                    size=size if type(size) is int and size >= 0 else None,
                    etag=item.get("ETag")
                    if isinstance(item.get("ETag"), str)
                    else None,
                )

    async def observe_legacy(
        self,
        key: str,
        *,
        prefix: str,
        etag: str | None,
        size: int | None,
        image: bool,
    ) -> IngestStorageFacts:
        if not key.startswith(prefix) or key.endswith("/"):
            raise IngestEntryFailure("invalid_source_object")
        return await run_in_thread(self._observe_legacy, key, prefix, etag, size, image)

    def _observe_legacy(
        self,
        key: str,
        prefix: str,
        etag: str | None,
        size: int | None,
        image: bool,
    ) -> IngestStorageFacts:
        try:
            parameters: dict[str, Any] = {"Bucket": self.datasets_bucket, "Key": key}
            if etag:
                parameters["IfMatch"] = etag
            head = self._client.head_object(**parameters)
        except Exception as exc:  # noqa: BLE001 - source observations are per-file outcomes.
            raise IngestEntryFailure(_read_failure_code(exc)) from None

        observed_size = head.get("ContentLength")
        if type(observed_size) is not int or observed_size < 0:
            raise IngestEntryFailure("source_read_failed")
        if size is not None and observed_size != size:
            raise IngestEntryFailure("source_changed")

        head_etag = head.get("ETag")
        if etag and head_etag and _unquote_etag(etag) != _unquote_etag(head_etag):
            raise IngestEntryFailure("source_changed")

        content_hash = _md5_etag(etag)
        object_identity = None
        if image:
            try:
                object_identity = object_identity_from_head(
                    head, expected_size=observed_size
                )
            except ImageFailure as exc:
                code = (
                    "source_changed"
                    if exc.code == "source_changed"
                    else "source_read_failed"
                )
                raise IngestEntryFailure(code) from None
        width = height = None
        if image and observed_size:
            width, height = self._image_dimensions(key, head_etag or etag)
        return IngestStorageFacts(
            observed_size, content_hash, width, height, object_identity
        )

    def _image_dimensions(
        self, key: str, etag: str | None
    ) -> tuple[int | None, int | None]:
        parameters: dict[str, Any] = {
            "Bucket": self.datasets_bucket,
            "Key": key,
            "Range": f"bytes=0-{_IMAGE_HEADER_BYTES - 1}",
        }
        if etag:
            parameters["IfMatch"] = etag
        try:
            response = self._client.get_object(**parameters)
            body = response["Body"]
            try:
                header = body.read(_IMAGE_HEADER_BYTES)
            finally:
                body.close()
        except Exception as exc:  # noqa: BLE001 - source observations are per-file outcomes.
            raise IngestEntryFailure(_read_failure_code(exc)) from None
        return _dimensions(header)

    async def publish(
        self,
        source: BinaryIO,
        target: ManagedOriginalMaterial,
        *,
        content_type: str,
        expected_size: int,
        expected_hash: str | None = None,
        image: bool = False,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ) -> tuple[IngestStorageFacts, ManagedOriginalProof]:
        """Conditionally create the allocated key and fully read it back."""
        try:
            written = await self.managed.publish_stream(
                source,
                target,
                content_type=content_type,
                expected_size=expected_size,
                expected_hash=expected_hash,
                image=image,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )
        except ManagedStorageError as exc:
            raise IngestEntryFailure(exc.code) from None
        facts = IngestStorageFacts(
            written.file_size,
            written.content_hash,
            written.dimensions[0] if written.dimensions else None,
            written.dimensions[1] if written.dimensions else None,
            written.object_identity,
        )
        return facts, written.proof

    async def publish_from_existing(
        self,
        source_key: str,
        target: ManagedOriginalMaterial,
        *,
        content_type: str,
        expected_size: int,
        expected_hash: str | None,
        image: bool,
        on_prepared=None,
        on_writing=None,
        on_written=None,
    ) -> tuple[IngestStorageFacts, ManagedOriginalProof]:
        """Copy an existing platform object into a connection-backed target."""
        body = None
        try:
            response = await run_in_thread(
                self._client.get_object, Bucket=self.datasets_bucket, Key=source_key
            )
            body = response.get("Body")
            if not callable(getattr(body, "read", None)):
                raise IngestEntryFailure("source_read_failed")
            written = await self.managed.publish_stream(
                body,
                target,
                content_type=content_type,
                expected_size=expected_size,
                expected_hash=expected_hash,
                image=image,
                on_prepared=on_prepared,
                on_writing=on_writing,
                on_written=on_written,
            )
        except ManagedStorageError as exc:
            raise IngestEntryFailure(exc.code) from None
        finally:
            if body is not None:
                try:
                    await run_in_thread(body.close)
                except Exception:
                    raise ManagedStorageError(
                        "storage_write_failed", uncertain=True
                    ) from None
        facts = IngestStorageFacts(
            written.file_size,
            written.content_hash,
            written.dimensions[0] if written.dimensions else None,
            written.dimensions[1] if written.dimensions else None,
            written.object_identity,
        )
        return facts, written.proof

    async def verify_managed(
        self,
        target: ManagedOriginalMaterial,
        *,
        sha256: str | None,
        size_bytes: int | None,
        image: bool,
    ) -> IngestStorageFacts:
        """Read back a verified creation without acquiring new bytes."""
        if sha256 is None or size_bytes is None:
            raise IngestEntryFailure("source_read_failed")
        try:
            read = await self.managed.verify(
                target, sha256=sha256, size_bytes=size_bytes, image=image
            )
        except ManagedStorageError as exc:
            raise IngestEntryFailure(exc.code) from None
        object_identity = None
        if image:
            try:
                object_identity = ObjectIdentity(
                    etag=read.etag.strip().strip('"'),
                    byte_size=read.size_bytes,
                    version_id=read.version_id,
                )
            except (TypeError, ValueError):
                raise IngestEntryFailure("source_read_failed") from None
        return IngestStorageFacts(
            read.size_bytes,
            read.md5,
            read.dimensions[0] if read.dimensions else None,
            read.dimensions[1] if read.dimensions else None,
            object_identity,
        )

    async def adopt(
        self,
        target: ManagedOriginalMaterial,
        *,
        expected_size: int,
        image: bool,
    ) -> tuple[IngestStorageFacts, ManagedOriginalProof]:
        """Pin an existing source object's exact version after a full read."""
        try:
            written = await self.managed.adopt(
                target, max_bytes=max(expected_size, 0), image=image
            )
        except ManagedStorageError as exc:
            raise IngestEntryFailure(exc.code) from None
        if written.file_size != expected_size:
            raise IngestEntryFailure("source_changed")
        facts = IngestStorageFacts(
            written.file_size,
            written.content_hash,
            written.dimensions[0] if written.dimensions else None,
            written.dimensions[1] if written.dimensions else None,
            written.object_identity,
        )
        return facts, written.proof


def _next_or_none(iterator):
    try:
        return next(iterator)
    except StopIteration:
        return None


def _read_failure_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in _MISSING_CODES or status == 404:
            return "source_missing"
        if code in _CHANGED_CODES or status in {409, 412}:
            return "source_changed"
    return "source_read_failed"


def _md5_etag(etag: str | None) -> str | None:
    value = _unquote_etag(etag or "")
    return value.lower() if _MD5_ETAG.fullmatch(value) else None


def _unquote_etag(value: str) -> str:
    return value.strip().strip('"')


def _dimensions(header: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(BytesIO(header)) as image:
            width, height = int(image.width), int(image.height)
            try:
                orientation = int(image.getexif().get(274, 1))
            except Exception:  # noqa: BLE001 - malformed EXIF uses the default orientation.
                orientation = 1
            return (height, width) if orientation in {5, 6, 7, 8} else (width, height)
    except Exception as exc:  # noqa: BLE001 - corrupt or unsupported images stay dimensionless.
        logger.debug("Image header could not be decoded (%s)", type(exc).__name__)
        return None, None
