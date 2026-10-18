"""Conditional S3 reads and attempt-scoped image artifact operations."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import AsyncIterator, Protocol

import httpx

from aap_assets_images.contract import (
    Artifact,
    ImageFailure,
    ImageOrphanCandidate,
    ImageSource,
    ObjectIdentity,
)
from aap_assets_images_runtime.processing import IMAGE_HEADER_BYTES

from .config import ImageStorageConfig

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ATTEMPT = rf"a{_UUID}"
_IMAGE_THUMBNAIL_KEY = re.compile(
    rf"^image-assets/{_UUID}/g[1-9][0-9]*/{_ATTEMPT}/thumbnail\.webp$"
)
_IMAGE_PYRAMID_KEY = re.compile(
    rf"^image-pyramids/{_UUID}/g[1-9][0-9]*/(?:{_ATTEMPT}/)?(?:"
    r"manifest\.json|overview\.webp|tiles/(?:0|[1-9][0-9]*)/"
    r"(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp)$"
)
_IMAGE_PYRAMID_ATTEMPT_KEY = re.compile(
    rf"^image-pyramids/{_UUID}/g[1-9][0-9]*/{_ATTEMPT}/(?:"
    r"manifest\.json|overview\.webp|tiles/(?:0|[1-9][0-9]*)/"
    r"(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp)$"
)
_IMAGE_GENERATION_KEY = re.compile(
    rf"^image-pyramids/(?P<asset>{_UUID})/g(?P<generation>[1-9][0-9]*)/"
    rf"(?:{_ATTEMPT}/)?(?:manifest\.json|overview\.webp|tiles/"
    r"(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp)$"
)
_HASH_CHUNK_BYTES = 1024 * 1024
_S3_LIST_PAGE_SIZE = 1000


@dataclass(frozen=True, slots=True)
class SourceHeader:
    identity: ObjectIdentity
    data: bytes


@dataclass(frozen=True, slots=True)
class DownloadedSource:
    identity: ObjectIdentity
    byte_size: int
    sha256: str


class _FileBody(httpx.AsyncByteStream):
    def __init__(self, path: Path, byte_size: int, expected_sha256: str) -> None:
        self.path = path
        self.byte_size = byte_size
        self.expected_sha256 = expected_sha256
        self._stream = None

    async def __aiter__(self):
        digest = hashlib.sha256()
        written = 0
        try:
            self._stream = self.path.open("rb")
            while chunk := self._stream.read(_HASH_CHUNK_BYTES):
                written += len(chunk)
                if written > self.byte_size:
                    raise ImageFailure("upload_verification_failed")
                digest.update(chunk)
                yield chunk
            if written != self.byte_size or digest.hexdigest() != self.expected_sha256:
                raise ImageFailure("upload_verification_failed")
        except ImageFailure:
            raise
        except OSError:
            raise ImageFailure("storage_unavailable") from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class ImageStorage(Protocol):
    async def head_source(self, source: ImageSource) -> ObjectIdentity: ...

    async def read_source_header(self, source: ImageSource) -> SourceHeader: ...

    async def download_source(
        self, source: ImageSource, destination: Path
    ) -> DownloadedSource: ...

    async def head_cache(self, key: str) -> ObjectIdentity | None: ...

    async def read_cache(self, key: str, limit: int) -> bytes: ...

    async def upload_cache(self, path: Path, key: str) -> Artifact: ...

    async def sign_source(self, source: ImageSource, expires_in: int = 3600) -> str: ...

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str: ...

    def iter_orphan_generations(self) -> AsyncIterator[ImageOrphanCandidate]: ...


class S3ImageStorage:
    """Fixed-bucket S3 access; retry attempts remain under caller authority."""

    def __init__(
        self,
        config: ImageStorageConfig,
        *,
        client=None,
        signer=None,
        http_client=None,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self._owns_http_client = http_client is None
        if client is None or signer is None:
            import boto3
            from botocore.config import Config

            client_config = Config(
                signature_version="s3v4",
                connect_timeout=config.connect_timeout,
                read_timeout=config.read_timeout,
                retries={"mode": "standard", "total_max_attempts": config.max_attempts},
                s3={"addressing_style": "path"},
            )
            credentials = {
                "aws_access_key_id": config.access_key,
                "aws_secret_access_key": config.secret_key,
                "region_name": config.region,
                "config": client_config,
            }
            if client is None:
                client = boto3.client("s3", endpoint_url=config.endpoint, **credentials)
            if signer is None:
                signer = boto3.client(
                    "s3", endpoint_url=config.public_endpoint, **credentials
                )
        self._client = client
        self._signer = signer

    def _get_http_client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.config.read_timeout,
                    connect=self.config.connect_timeout,
                    pool=self.config.connect_timeout,
                ),
                follow_redirects=False,
            )
        return self._http_client

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            client = self._http_client
            self._http_client = None
            await client.aclose()

    def _presigned_http_url(self, operation: str, params: dict[str, object]) -> str:
        try:
            return self._client.generate_presigned_url(
                operation,
                Params=params,
                ExpiresIn=300,
            )
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None

    async def _head_object(self, client, bucket: str, key: str):
        url = self._presigned_http_url("head_object", {"Bucket": bucket, "Key": key})
        try:
            response = await client.head(url)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None
        if not 200 <= response.status_code < 300:
            _raise_http_failure(response, missing_is_object=True)
        return _http_identity(response.headers), response.headers

    async def head_source(self, source: ImageSource) -> ObjectIdentity:
        bucket = _source_bucket(source, self.config)
        _validate_source_key(source.file_path)
        try:
            identity, _headers = await self._head_object(
                self._get_http_client(), bucket, source.file_path
            )
            return identity
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc, missing_is_object=True) from None

    async def read_source_header(self, source: ImageSource) -> SourceHeader:
        expected = source.object_identity
        if expected is None:
            raise ImageFailure("source_changed")
        if expected.byte_size <= 0:
            raise ImageFailure("source_missing")
        if source.file_size is not None and expected.byte_size != source.file_size:
            raise ImageFailure("source_changed")
        bucket = _source_bucket(source, self.config)
        params: dict[str, object] = {
            "Bucket": bucket,
            "Key": source.file_path,
            "Range": f"bytes=0-{IMAGE_HEADER_BYTES - 1}",
            "IfMatch": _if_match(expected.etag),
        }
        if expected.version_id is not None:
            params["VersionId"] = expected.version_id
        client = self._get_http_client()
        try:
            if (await self._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            url = self._presigned_http_url("get_object", params)
            request_headers = {
                "Range": params["Range"],
                "If-Match": params["IfMatch"],
            }
            async with client.stream("GET", url, headers=request_headers) as response:
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
            if (await self._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            return header
        except ImageFailure:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def download_source(
        self, source: ImageSource, destination: Path
    ) -> DownloadedSource:
        expected = source.object_identity
        if expected is None:
            raise ImageFailure("source_changed")
        _validate_source_key(source.file_path)
        bucket = _source_bucket(source, self.config)
        params: dict[str, object] = {
            "Bucket": bucket,
            "Key": source.file_path,
            "IfMatch": _if_match(expected.etag),
        }
        if expected.version_id is not None:
            params["VersionId"] = expected.version_id
        client = self._get_http_client()
        written = 0
        created_destination = False
        try:
            if (await self._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            url = self._presigned_http_url("get_object", params)
            request_headers = {"If-Match": params["IfMatch"]}
            async with client.stream("GET", url, headers=request_headers) as response:
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
            if (await self._head_object(client, bucket, source.file_path))[
                0
            ] != expected:
                raise ImageFailure("source_changed")
            return DownloadedSource(
                identity=expected,
                byte_size=written,
                sha256=sha256_hex,
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

    async def head_cache(self, key: str) -> ObjectIdentity | None:
        _validate_cache_key(key)
        try:
            identity, _headers = await self._head_object(
                self._get_http_client(), self.config.cache_bucket, key
            )
            return identity
        except ImageFailure as exc:
            if exc.code == "object_missing":
                return None
            raise
        except Exception as exc:
            mapped = _storage_error(exc, missing_is_object=True)
            if mapped.code == "object_missing":
                return None
            raise mapped from None

    async def read_cache(self, key: str, limit: int) -> bytes:
        _validate_cache_key(key)
        if type(limit) is not int or not 1 <= limit <= 16 * 1024 * 1024:
            raise ImageFailure("resource_budget_exceeded")
        client = self._get_http_client()
        url = self._presigned_http_url(
            "get_object", {"Bucket": self.config.cache_bucket, "Key": key}
        )
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    _raise_http_failure(response, missing_is_object=True)
                identity = _http_identity(response.headers)
                if identity.byte_size > limit:
                    raise ImageFailure("resource_budget_exceeded")
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > limit:
                        raise ImageFailure("resource_budget_exceeded")
                    data.extend(chunk)
                if len(data) != identity.byte_size:
                    raise ImageFailure("storage_unavailable")
                return bytes(data)
        except ImageFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def upload_cache(self, path: Path, key: str) -> Artifact:
        _validate_cache_write_key(key)
        path = Path(path)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            raise ImageFailure("invalid_response") from None
        if path.is_symlink() or not path.is_file() or info.st_size <= 0:
            raise ImageFailure("invalid_response")
        is_manifest = key.endswith("/manifest.json")
        expected_suffix = ".json" if is_manifest else ".webp"
        if path.suffix.lower() != expected_suffix:
            raise ImageFailure("invalid_response")
        digest = _sha256_file(path)
        content_type = "application/json" if is_manifest else "image/webp"
        params = {
            "Bucket": self.config.cache_bucket,
            "Key": key,
            "ContentType": content_type,
            "ContentLength": info.st_size,
            "Metadata": {"sha256": digest},
        }
        url = self._presigned_http_url("put_object", params)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(info.st_size),
            "x-amz-meta-sha256": digest,
        }
        body = _FileBody(path, info.st_size, digest)
        try:
            response = await self._get_http_client().put(
                url,
                headers=headers,
                content=body,
            )
            if not 200 <= response.status_code < 300:
                _raise_http_failure(response, missing_is_object=False)
            identity, observed_headers = await self._head_object(
                self._get_http_client(), self.config.cache_bucket, key
            )
            if (
                identity.byte_size != info.st_size
                or observed_headers.get("x-amz-meta-sha256") != digest
            ):
                raise ImageFailure("upload_verification_failed")
            return Artifact(key=key, byte_size=info.st_size, sha256=digest)
        except ImageFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None
        finally:
            await body.aclose()

    async def sign_source(self, source: ImageSource, expires_in: int = 3600) -> str:
        bucket = _source_bucket(source, self.config)
        _validate_source_key(source.file_path)
        params: dict[str, object] = {"Bucket": bucket, "Key": source.file_path}
        if (
            source.object_identity is not None
            and source.object_identity.version_id is not None
        ):
            params["VersionId"] = source.object_identity.version_id
        try:
            return await asyncio.to_thread(
                self._signer.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=_expiry(expires_in),
            )
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str:
        _validate_cache_key(key)
        try:
            return await asyncio.to_thread(
                self._signer.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.config.cache_bucket, "Key": key},
                ExpiresIn=_expiry(expires_in),
            )
        except ImageFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None

    async def iter_orphan_generations(self) -> AsyncIterator[ImageOrphanCandidate]:
        """Stream every canonical generation after its complete key prefix is seen."""
        token: str | None = None
        seen_tokens: set[str] = set()
        last_key: str | None = None
        current: tuple[str, int] | None = None
        current_modified: datetime | None = None
        while True:
            request: dict[str, object] = {
                "Bucket": self.config.cache_bucket,
                "Prefix": "image-pyramids/",
                "MaxKeys": _S3_LIST_PAGE_SIZE,
            }
            if token is not None:
                request["ContinuationToken"] = token
            try:
                page = await asyncio.to_thread(self._client.list_objects_v2, **request)
            except Exception as exc:
                raise _storage_error(exc) from None
            if not isinstance(page, dict) or type(page.get("IsTruncated")) is not bool:
                raise ImageFailure("invalid_response")
            contents = page.get("Contents", [])
            if not isinstance(contents, list):
                raise ImageFailure("invalid_response")
            for entry in contents:
                if not isinstance(entry, dict):
                    raise ImageFailure("invalid_response")
                key = entry.get("Key")
                modified = entry.get("LastModified")
                if not isinstance(key, str) or not key.startswith("image-pyramids/"):
                    raise ImageFailure("invalid_response")
                if last_key is not None and key <= last_key:
                    raise ImageFailure("invalid_response")
                last_key = key
                if key == "image-pyramids/":
                    continue
                match = _IMAGE_GENERATION_KEY.fullmatch(key)
                if match is None or not isinstance(modified, datetime):
                    raise ImageFailure("invalid_response")
                if modified.tzinfo is None or modified.utcoffset() is None:
                    raise ImageFailure("invalid_response")
                pair = (match.group("asset"), int(match.group("generation")))
                if pair != current:
                    if current is not None and current_modified is not None:
                        yield ImageOrphanCandidate(
                            asset_id=current[0],
                            generation=current[1],
                            last_modified=current_modified,
                        )
                    current = pair
                    current_modified = modified
                elif current_modified is None or modified > current_modified:
                    current_modified = modified
            truncated = page["IsTruncated"]
            next_token = page.get("NextContinuationToken")
            if not truncated:
                if current is not None and current_modified is not None:
                    yield ImageOrphanCandidate(
                        asset_id=current[0],
                        generation=current[1],
                        last_modified=current_modified,
                    )
                return
            if (
                not isinstance(next_token, str)
                or not next_token
                or next_token == token
                or next_token in seen_tokens
            ):
                raise ImageFailure("invalid_response")
            seen_tokens.add(next_token)
            token = next_token


def _source_bucket(source: ImageSource, config: ImageStorageConfig) -> str:
    if source.bucket_kind == "datasets":
        return config.datasets_bucket
    if source.bucket_kind == "legacy":
        return config.legacy_bucket
    raise ImageFailure("invalid_request")


def _validate_source_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key
        or len(key) > 1024
        or "\x00" in key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ImageFailure("invalid_request")


def _validate_cache_key(key: str) -> None:
    if not isinstance(key, str) or not (
        _IMAGE_THUMBNAIL_KEY.fullmatch(key) or _IMAGE_PYRAMID_KEY.fullmatch(key)
    ):
        raise ImageFailure("invalid_request")


def _validate_cache_write_key(key: str) -> None:
    if not isinstance(key, str) or not (
        _IMAGE_THUMBNAIL_KEY.fullmatch(key) or _IMAGE_PYRAMID_ATTEMPT_KEY.fullmatch(key)
    ):
        raise ImageFailure("invalid_request")


def _http_identity(headers) -> ObjectIdentity:
    etag = headers.get("etag")
    size_value = headers.get("content-length")
    version_id = headers.get("x-amz-version-id")
    try:
        size = int(size_value)
    except (TypeError, ValueError):
        raise ImageFailure("storage_unavailable") from None
    if (
        not isinstance(etag, str)
        or not etag.strip().strip('"')
        or size < 0
        or version_id == ""
    ):
        raise ImageFailure("storage_unavailable")
    return ObjectIdentity(
        etag=etag.strip().strip('"'), byte_size=size, version_id=version_id
    )


def _range_identity_headers(headers, expected: ObjectIdentity) -> ObjectIdentity:
    etag = headers.get("etag")
    version_id = headers.get("x-amz-version-id")
    content_range = headers.get("content-range")
    content_length = headers.get("content-length")
    match = (
        re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
        if isinstance(content_range, str)
        else None
    )
    if match is None:
        raise ImageFailure("source_changed")
    end = int(match.group(1))
    total = int(match.group(2))
    wanted_end = min(expected.byte_size, IMAGE_HEADER_BYTES) - 1
    try:
        range_length = int(content_length)
    except (TypeError, ValueError):
        raise ImageFailure("source_changed") from None
    if (
        total != expected.byte_size
        or end != wanted_end
        or range_length != end + 1
        or not isinstance(etag, str)
        or etag.strip().strip('"') != expected.etag
        or version_id != expected.version_id
    ):
        raise ImageFailure("source_changed")
    return ObjectIdentity(
        etag=expected.etag,
        byte_size=total,
        version_id=expected.version_id,
    )


def _raise_http_failure(response, *, missing_is_object: bool) -> None:
    status = response.status_code
    code = response.headers.get("x-amz-error-code")
    if code in {"PreconditionFailed", "412"} or status == 412:
        raise ImageFailure("source_changed")
    if missing_is_object and code in {"NoSuchKey", "NotFound", "404"}:
        raise ImageFailure("object_missing")
    if (
        missing_is_object
        and status == 404
        and code not in {"NoSuchBucket", "NoSuchAccessPoint"}
    ):
        raise ImageFailure("object_missing")
    raise ImageFailure("storage_unavailable")


def _if_match(etag: str) -> str:
    value = etag.strip().strip('"')
    if not value:
        raise ImageFailure("source_changed")
    return f'"{value}"'


def _declares_md5(expected: str | None) -> bool:
    if expected is None:
        return False
    value = expected.strip().lower()
    if value.startswith("md5:"):
        value = value[4:]
    return len(value) == 32 and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _matches_declared_content_hash(
    expected: str | None, sha256_hex: str, md5_hex: str | None
) -> bool:
    if expected is None:
        return True
    value = expected.strip()
    if value.lower().startswith("sha256:"):
        value = value[7:]
        return len(value) == 64 and value.lower() == sha256_hex
    if value.lower().startswith("md5:"):
        value = value[4:]
        return md5_hex is not None and len(value) == 32 and value.lower() == md5_hex
    if len(value) == 32:
        return md5_hex is not None and value.lower() == md5_hex
    if len(value) == 64:
        return value.lower() == sha256_hex
    return True


def _expiry(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 604800:
        raise ImageFailure("invalid_request")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_error(exc: Exception, *, missing_is_object: bool = False) -> ImageFailure:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = error.get("Code") if isinstance(error, dict) else None
    status = (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(response, dict)
        else None
    )
    if code in {"PreconditionFailed", "412"} or status == 412:
        return ImageFailure("source_changed")
    if missing_is_object and code in {"NoSuchKey", "NotFound", "404"}:
        return ImageFailure("object_missing")
    if (
        missing_is_object
        and status == 404
        and code not in {"NoSuchBucket", "NoSuchAccessPoint"}
    ):
        return ImageFailure("object_missing")
    return ImageFailure("storage_unavailable")
