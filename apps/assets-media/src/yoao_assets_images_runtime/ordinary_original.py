"""Existing ordinary-source S3 contract scoped to an owner-authorized key."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
from pathlib import Path
import re

from yoao_assets_images.contract import ImageFailure, ObjectIdentity
from yoao_assets_media_runtime.original_read import blocks_physical_source
from .processing import IMAGE_HEADER_BYTES
from .storage import (
    DownloadedSource,
    SourceHeader,
    _declares_md5,
    _expiry,
    _http_identity,
    _if_match,
    _matches_declared_content_hash,
    _raise_http_failure,
    _source_bucket,
    _storage_error,
    _validate_source_key,
)


def _range_identity_headers(headers, expected):
    etag = headers.get("etag")
    version_id = headers.get("x-amz-version-id")
    content_range = headers.get("content-range")
    match = (
        re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
        if isinstance(content_range, str)
        else None
    )
    if match is None:
        raise ImageFailure("source_changed")
    end, total = int(match.group(1)), int(match.group(2))
    try:
        range_length = int(headers.get("content-length"))
    except (TypeError, ValueError):
        raise ImageFailure("source_changed") from None
    if (
        total != expected.byte_size
        or end != min(expected.byte_size, IMAGE_HEADER_BYTES) - 1
        or range_length != end + 1
        or not isinstance(etag, str)
        or etag.strip().strip('"') != expected.etag
        or version_id != expected.version_id
    ):
        raise ImageFailure("source_changed")
    return ObjectIdentity(
        etag=expected.etag, byte_size=total, version_id=expected.version_id
    )


class OrdinaryImageOriginal:
    def __init__(self, storage, permit):
        self._storage = storage
        self._key = permit.object_key
        self._permit = permit

    def _check_source(self, source):
        _validate_source_key(source.file_path)
        if source.file_path != self._key:
            raise ImageFailure("source_changed")
        if (
            self._permit.blocked_storage_identities
            or self._permit.source_storage_identity
        ):
            endpoint = self._storage.config.endpoint.rstrip("/")
            actual_endpoint = getattr(
                getattr(self._storage._client, "meta", None), "endpoint_url", None
            )
            if (
                not isinstance(actual_endpoint, str)
                or actual_endpoint.rstrip("/") != endpoint
                or blocks_physical_source(
                    self._permit, endpoint, _source_bucket(source, self._storage.config)
                )
            ):
                raise ImageFailure("source_changed")

    async def head_source(self, source):
        self._check_source(source)
        bucket = _source_bucket(source, self._storage.config)
        try:
            identity, _headers = await self._storage._head_object(
                self._storage._get_http_client(), bucket, source.file_path
            )
            return identity
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc, missing_is_object=True) from None

    async def read_source_header(self, source):
        self._check_source(source)
        expected = source.object_identity
        if expected is None:
            raise ImageFailure("source_changed")
        if expected.byte_size <= 0:
            raise ImageFailure("source_missing")
        if source.file_size is not None and expected.byte_size != source.file_size:
            raise ImageFailure("source_changed")
        bucket = _source_bucket(source, self._storage.config)
        params = {
            "Bucket": bucket,
            "Key": source.file_path,
            "Range": f"bytes=0-{IMAGE_HEADER_BYTES - 1}",
            "IfMatch": _if_match(expected.etag),
        }
        if expected.version_id is not None:
            params["VersionId"] = expected.version_id
        client = self._storage._get_http_client()
        try:
            if (await self._storage._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            url = self._storage._presigned_http_url("get_object", params)
            headers = {"Range": params["Range"], "If-Match": params["IfMatch"]}
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 206:
                    if 200 <= response.status_code < 300:
                        raise ImageFailure("source_changed")
                    _raise_http_failure(response, missing_is_object=True)
                identity = _range_identity_headers(response.headers, expected)
                expected_length = min(expected.byte_size, IMAGE_HEADER_BYTES)
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > expected_length:
                        raise ImageFailure("source_changed")
                    data.extend(chunk)
                if len(data) != expected_length:
                    raise ImageFailure("source_changed")
                header = SourceHeader(identity=identity, data=bytes(data))
            if (await self._storage._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            return header
        except ImageFailure:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def download_source(self, source, destination: Path):
        self._check_source(source)
        expected = source.object_identity
        if expected is None:
            raise ImageFailure("source_changed")
        bucket = _source_bucket(source, self._storage.config)
        params = {
            "Bucket": bucket,
            "Key": source.file_path,
            "IfMatch": _if_match(expected.etag),
        }
        if expected.version_id is not None:
            params["VersionId"] = expected.version_id
        client = self._storage._get_http_client()
        written = 0
        created_destination = False
        try:
            if (await self._storage._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            url = self._storage._presigned_http_url("get_object", params)
            async with client.stream(
                "GET", url, headers={"If-Match": params["IfMatch"]}
            ) as response:
                if response.status_code != 200:
                    _raise_http_failure(response, missing_is_object=True)
                if _http_identity(response.headers) != expected:
                    raise ImageFailure("source_changed")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                md5 = (
                    hashlib.md5(usedforsecurity=False)
                    if _declares_md5(source.content_hash)
                    else None
                )
                with destination.open("xb") as output:
                    created_destination = True
                    async for chunk in response.aiter_raw():
                        written += len(chunk)
                        if written > expected.byte_size:
                            raise ImageFailure("source_changed")
                        digest.update(chunk)
                        if md5 is not None:
                            md5.update(chunk)
                        output.write(chunk)
                if written != expected.byte_size or (
                    source.file_size is not None and written != source.file_size
                ):
                    raise ImageFailure("source_changed")
                sha256_hex = digest.hexdigest()
                if not _matches_declared_content_hash(
                    source.content_hash,
                    sha256_hex,
                    md5.hexdigest() if md5 is not None else None,
                ):
                    raise ImageFailure("source_changed")
                destination.chmod(0o400)
            if (await self._storage._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            return DownloadedSource(
                identity=expected, byte_size=written, sha256=sha256_hex
            )
        except asyncio.CancelledError:
            if created_destination:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
            raise
        except ImageFailure:
            if created_destination:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
            raise
        except Exception:
            if created_destination:
                with suppress(OSError):
                    destination.unlink(missing_ok=True)
            raise ImageFailure("storage_unavailable") from None

    async def sign_source(self, source, expires_in=3600):
        self._check_source(source)
        params = {
            "Bucket": _source_bucket(source, self._storage.config),
            "Key": source.file_path,
        }
        if (
            source.object_identity is not None
            and source.object_identity.version_id is not None
        ):
            params["VersionId"] = source.object_identity.version_id
        try:
            return await asyncio.to_thread(
                self._storage._signer.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=_expiry(expires_in),
            )
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None
