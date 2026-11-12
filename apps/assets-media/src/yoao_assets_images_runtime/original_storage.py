"""Physical reads scoped by the native owner to one image original."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
from pathlib import Path

from yoao_assets_images.contract import (
    ImageFailure,
    ImageSource,
    ObjectIdentity,
    OriginalRead,
)

from .processing import IMAGE_HEADER_BYTES
from .storage import (
    DownloadedSource,
    SourceHeader,
    _declares_md5,
    _if_match,
    _matches_declared_content_hash,
    _source_bucket,
    _storage_error,
    _validate_source_key,
)

_CHUNK_BYTES = 1024 * 1024


async def _run(function, /, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException as late:
            combined = builtins.BaseExceptionGroup(
                "Image source I/O was cancelled and then failed", [cancelled, late]
            )
            if getattr(late, "storage_operation_unsettled", False):
                combined.storage_operation_unsettled = True
            raise combined from None
        raise


def _read_body(response, read):
    body = response.get("Body") if isinstance(response, dict) else None
    if not callable(getattr(body, "read", None)) or not callable(
        getattr(body, "close", None)
    ):
        error = ImageFailure("storage_unavailable")
        error.storage_operation_unsettled = True
        raise error
    failure = None
    try:
        return read(body)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            body.close()
        except BaseException as close_error:
            if failure is None:
                close_error.storage_operation_unsettled = True
                raise
            combined = builtins.BaseExceptionGroup(
                "Image source read and physical close both failed",
                [failure, close_error],
            )
            combined.storage_operation_unsettled = True
            raise combined from None


class BoundImageOriginal:
    def __init__(self, storage, permit: OriginalRead):
        from yoao_assets_media_runtime.additional_original import physical_identity

        if permit.ownership != "fixture":
            raise ImageFailure("source_changed")

        actual_endpoint = getattr(
            getattr(storage._client, "meta", None), "endpoint_url", None
        )
        primary = (
            isinstance(actual_endpoint, str)
            and actual_endpoint.rstrip("/") == storage.config.endpoint.rstrip("/")
            and permit.endpoint_url.rstrip("/") == storage.config.endpoint.rstrip("/")
            and permit.bucket
            in {storage.config.datasets_bucket, storage.config.legacy_bucket}
            and permit.storage_identity
            == physical_identity(storage.config.endpoint, permit.bucket)
        )
        additional = storage._additional_original
        self._additional = additional is not None and additional.matches(permit)
        if not (self._additional or primary):
            raise ImageFailure("source_changed")
        self._client = additional.client if self._additional else storage._client
        self._storage = storage
        self._permit = permit
        self._identity = ObjectIdentity(
            etag=permit.etag.strip().strip('"'),
            byte_size=permit.size_bytes,
            version_id=permit.version_id,
        )

    def _check_source(self, source: ImageSource, *, observed: bool):
        _validate_source_key(source.file_path)
        if (
            source.file_path != self._permit.object_key
            or (
                source.bucket_kind != "datasets"
                if self._additional
                else _source_bucket(source, self._storage.config) != self._permit.bucket
            )
            or source.file_size is not None
            and source.file_size != self._permit.size_bytes
            or observed
            and source.object_identity != self._identity
        ):
            raise ImageFailure("source_changed")

    async def sign_source(self, source: ImageSource, expires_in: int) -> None:
        self._check_source(source, observed=False)
        return None

    def _head(self, source: ImageSource) -> ObjectIdentity:
        self._check_source(source, observed=False)
        response = self._client.head_object(
            Bucket=self._permit.bucket,
            Key=self._permit.object_key,
            VersionId=self._permit.version_id,
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("ETag"), str)
            or not response["ETag"].strip().strip('"')
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] < 0
        ):
            raise ImageFailure("storage_unavailable")
        identity = ObjectIdentity(
            etag=response["ETag"].strip().strip('"'),
            byte_size=response["ContentLength"],
            version_id=response.get("VersionId"),
        )
        if identity != self._identity:
            raise ImageFailure("source_changed")
        return identity

    async def _call(self, function, /, *args):
        try:
            return await _run(function, *args)
        except ImageFailure:
            raise
        except Exception as exc:
            if isinstance(exc, builtins.BaseExceptionGroup) or getattr(
                exc, "storage_operation_unsettled", False
            ):
                raise
            raise _storage_error(exc, missing_is_object=True) from None

    async def head_source(self, source: ImageSource) -> ObjectIdentity:
        return await self._call(self._head, source)

    async def read_source_header(self, source: ImageSource) -> SourceHeader:
        return await self._call(self._read_header, source)

    def _read_header(self, source: ImageSource) -> SourceHeader:
        self._check_source(source, observed=True)
        identity = source.object_identity
        if identity is None:
            raise ImageFailure("source_changed")
        if identity.byte_size <= 0:
            raise ImageFailure("source_missing")
        if source.file_size is not None and source.file_size != identity.byte_size:
            raise ImageFailure("source_changed")
        if self._head(source) != identity:
            raise ImageFailure("source_changed")
        expected_length = min(identity.byte_size, IMAGE_HEADER_BYTES)
        params = {
            "Bucket": self._permit.bucket,
            "Key": self._permit.object_key,
            "IfMatch": _if_match(identity.etag),
            "Range": f"bytes=0-{IMAGE_HEADER_BYTES - 1}",
        }
        if identity.version_id is not None:
            params["VersionId"] = identity.version_id
        response = self._client.get_object(**params)

        def capture(body):
            if (
                response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 206
                or response.get("ContentLength") != expected_length
                or response.get("ContentRange")
                != f"bytes 0-{expected_length - 1}/{identity.byte_size}"
                or not isinstance(response.get("ETag"), str)
                or response["ETag"].strip().strip('"') != identity.etag
                or response.get("VersionId") != identity.version_id
            ):
                raise ImageFailure("source_changed")
            data = bytearray()
            while True:
                chunk = body.read(min(_CHUNK_BYTES, expected_length - len(data) + 1))
                if not isinstance(chunk, bytes):
                    raise ImageFailure("storage_unavailable")
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > expected_length:
                    raise ImageFailure("source_changed")
            if len(data) != expected_length:
                raise ImageFailure("source_changed")
            return bytes(data)

        data = _read_body(response, capture)
        if self._head(source) != identity:
            raise ImageFailure("source_changed")
        return SourceHeader(identity=identity, data=data)

    async def download_source(
        self, source: ImageSource, destination: Path
    ) -> DownloadedSource:
        return await self._call(self._download, source, destination)

    def _download(self, source: ImageSource, destination: Path) -> DownloadedSource:
        self._check_source(source, observed=True)
        identity = source.object_identity
        if identity is None:
            raise ImageFailure("source_changed")
        if source.file_size is not None and source.file_size != identity.byte_size:
            raise ImageFailure("source_changed")
        created = False
        try:
            if self._head(source) != identity:
                raise ImageFailure("source_changed")
            params = {
                "Bucket": self._permit.bucket,
                "Key": self._permit.object_key,
                "IfMatch": _if_match(identity.etag),
            }
            if identity.version_id is not None:
                params["VersionId"] = identity.version_id
            response = self._client.get_object(**params)

            def capture(body):
                nonlocal created
                if (
                    response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    or response.get("ContentLength") != identity.byte_size
                    or not isinstance(response.get("ETag"), str)
                    or response["ETag"].strip().strip('"') != identity.etag
                    or response.get("VersionId") != identity.version_id
                ):
                    raise ImageFailure("source_changed")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                md5 = (
                    hashlib.md5(usedforsecurity=False)
                    if _declares_md5(source.content_hash)
                    else None
                )
                written = 0
                with destination.open("xb") as output:
                    created = True
                    while True:
                        chunk = body.read(_CHUNK_BYTES)
                        if not isinstance(chunk, bytes):
                            raise ImageFailure("storage_unavailable")
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > identity.byte_size:
                            raise ImageFailure("source_changed")
                        digest.update(chunk)
                        if md5 is not None:
                            md5.update(chunk)
                        if output.write(chunk) != len(chunk):
                            raise OSError("short local image write")
                digest_hex = digest.hexdigest()
                if (
                    written != identity.byte_size
                    or self._permit.sha256 is not None
                    and digest_hex != self._permit.sha256
                    or not _matches_declared_content_hash(
                        source.content_hash,
                        digest_hex,
                        md5.hexdigest() if md5 is not None else None,
                    )
                ):
                    raise ImageFailure("source_changed")
                return DownloadedSource(
                    identity=identity, byte_size=written, sha256=digest_hex
                )

            result = _read_body(response, capture)
            if self._head(source) != identity:
                raise ImageFailure("source_changed")
            destination.chmod(0o400)
            return result
        except BaseException as exc:
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except BaseException as cleanup_error:
                    combined = builtins.BaseExceptionGroup(
                        "Image source read and local cleanup both failed",
                        [exc, cleanup_error],
                    )
                    if getattr(exc, "storage_operation_unsettled", False):
                        combined.storage_operation_unsettled = True
                    raise combined from None
            raise
