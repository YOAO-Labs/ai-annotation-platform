"""Bounded S3 reads and immutable publication for synchronous ingestion."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from io import BytesIO
import logging
import re
from typing import Any, AsyncIterator, BinaryIO

from botocore.exceptions import ClientError
from aap_assets_images.contract import ImageFailure, ObjectIdentity

from aap_assets_source.ingest_contract import IngestEntryErrorCode, IngestFailure
from .image_admission import object_identity_from_head

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

    def __init__(self, client: Any, datasets_bucket: str) -> None:
        if not datasets_bucket:
            raise ValueError("datasets_bucket must not be empty")
        self._client = client
        self.datasets_bucket = datasets_bucket

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
        *,
        key: str,
        content_type: str,
        expected_size: int,
        expected_hash: str | None = None,
        image: bool = False,
    ) -> IngestStorageFacts:
        if (
            not key.startswith("dataset-sources/")
            or expected_size < 0
            or not content_type
        ):
            raise IngestEntryFailure("invalid_source_object")
        return await run_in_thread(
            self._publish,
            source,
            key,
            content_type,
            expected_size,
            expected_hash,
            image,
        )

    def _publish(
        self,
        source: BinaryIO,
        key: str,
        content_type: str,
        expected_size: int,
        expected_hash: str | None,
        image: bool,
    ) -> IngestStorageFacts:
        upload_id: str | None = None
        try:
            multipart = expected_size > _PART_BYTES
            if multipart:
                created = self._client.create_multipart_upload(
                    Bucket=self.datasets_bucket,
                    Key=key,
                    ContentType=content_type,
                )
                upload_id = created.get("UploadId")
                if not isinstance(upload_id, str) or not upload_id:
                    raise IngestEntryFailure("storage_write_failed")

            md5 = hashlib.md5(usedforsecurity=False)
            header = bytearray()
            total = 0
            parts: list[dict[str, Any]] = []
            body = bytearray()
            part = bytearray()
            part_number = 0

            while True:
                try:
                    read_size = _PART_BYTES - len(part) if multipart else _PART_BYTES
                    chunk = source.read(read_size)
                except (
                    Exception
                ) as exc:  # ZIP readers may report CRC/decompression errors here.
                    raise IngestEntryFailure("archive_entry_read_failed") from exc
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise IngestEntryFailure("archive_entry_read_failed")
                total += len(chunk)
                if total > expected_size:
                    raise IngestEntryFailure("archive_entry_read_failed")
                md5.update(chunk)
                if len(header) < _IMAGE_HEADER_BYTES:
                    header.extend(chunk[: _IMAGE_HEADER_BYTES - len(header)])

                if multipart:
                    part.extend(chunk)
                    if len(part) == _PART_BYTES:
                        parts.append(
                            self._upload_part(
                                key, upload_id, part_number + 1, bytes(part)
                            )
                        )
                        part_number += 1
                        part.clear()
                else:
                    body.extend(chunk)

            if total != expected_size:
                raise IngestEntryFailure("archive_entry_read_failed")
            digest = md5.hexdigest()
            if expected_hash is not None and digest != expected_hash:
                raise IngestEntryFailure("archive_entry_read_failed")

            if multipart:
                if part:
                    parts.append(
                        self._upload_part(key, upload_id, part_number + 1, bytes(part))
                    )
                self._client.complete_multipart_upload(
                    Bucket=self.datasets_bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
                upload_id = None
            else:
                self._client.put_object(
                    Bucket=self.datasets_bucket,
                    Key=key,
                    Body=bytes(body),
                    ContentType=content_type,
                    IfNoneMatch="*",
                )
        except IngestEntryFailure:
            self._abort_multipart(key, upload_id)
            raise
        except Exception as exc:  # noqa: BLE001 - hide storage SDK details per file.
            self._abort_multipart(key, upload_id)
            logger.warning(
                "Immutable source publication failed (%s)", type(exc).__name__
            )
            raise IngestEntryFailure("storage_write_failed") from None

        width, height = _dimensions(bytes(header)) if image else (None, None)
        object_identity = None
        if image:
            try:
                head = self._client.head_object(
                    Bucket=self.datasets_bucket,
                    Key=key,
                )
                object_identity = object_identity_from_head(head, expected_size=total)
            except ImageFailure as exc:
                code = (
                    "source_changed"
                    if exc.code == "source_changed"
                    else "storage_write_failed"
                )
                raise IngestEntryFailure(code) from None
            except Exception as exc:  # noqa: BLE001 - do not expose storage SDK details.
                logger.warning(
                    "Published image verification failed (%s)", type(exc).__name__
                )
                raise IngestEntryFailure("storage_write_failed") from None
        return IngestStorageFacts(total, digest, width, height, object_identity)

    def _upload_part(
        self, key: str, upload_id: str | None, part_number: int, body: bytes
    ) -> dict[str, Any]:
        try:
            uploaded = self._client.upload_part(
                Bucket=self.datasets_bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_number,
                Body=body,
            )
            etag = uploaded.get("ETag")
            if not isinstance(etag, str) or not etag:
                raise ValueError("S3 part acknowledgement is missing its ETag")
        except Exception as exc:  # noqa: BLE001 - do not expose SDK details.
            raise IngestEntryFailure("storage_write_failed") from exc
        return {"PartNumber": part_number, "ETag": etag}

    def _abort_multipart(self, key: str, upload_id: str | None) -> None:
        if upload_id is None:
            return
        try:
            self._client.abort_multipart_upload(
                Bucket=self.datasets_bucket, Key=key, UploadId=upload_id
            )
        except Exception as exc:  # noqa: BLE001 - abort failure cannot delete published data.
            logger.info("Multipart source abort failed (%s)", type(exc).__name__)


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
