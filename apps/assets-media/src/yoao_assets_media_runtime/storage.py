"""Fixed-bucket S3 operations for source reads and media-cache artifacts."""

from __future__ import annotations

import asyncio
import builtins
from hashlib import sha256
from pathlib import Path
import re
from typing import Protocol

from yoao_assets_media.contract import (
    Artifact,
    MediaFailure,
    MediaSource,
    ObjectIdentity,
    OriginalRead,
)

from .original_read import blocks_physical_source

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CACHE_KEY = re.compile(
    rf"^media-assets/{_UUID}/g[1-9][0-9]*/a{_UUID}/(?:poster\.webp|playback\.mp4|frame\.(?:webp|jpeg)|chunk\.mp4)$"
)
_CACHE_PREFIX = re.compile(rf"^media-assets/{_UUID}/g[1-9][0-9]*/a{_UUID}/$")
_LEGACY_FRAME_KEY = re.compile(
    rf"^videos/{_UUID}/frames/(?:0|[1-9][0-9]*)_[1-9][0-9]*\.(?:webp|jpeg)$"
)
_LEGACY_CHUNK_KEY = re.compile(rf"^videos/{_UUID}/chunks/(?:0|[1-9][0-9]*)\.mp4$")
_LEGACY_PLAYBACK_KEY = re.compile(rf"^playback/{_UUID}\.mp4$")
_CHUNK_BYTES = 1024 * 1024


class MediaOriginalStorage(Protocol):
    original_sha256: str | None

    async def head_source(self, key: str) -> ObjectIdentity: ...

    async def download_source(self, source: MediaSource, destination: Path) -> None: ...


class MediaStorage(Protocol):
    def bind_original(self, permit: OriginalRead) -> BoundMediaOriginal: ...

    async def head_cache(self, key: str) -> ObjectIdentity | None: ...

    async def upload_cache(self, path: Path, key: str) -> Artifact: ...

    async def delete_cache_keys(self, keys: list[str]) -> int: ...

    async def delete_cache_prefix(self, prefix: str) -> int: ...

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str: ...


class BoundMediaOriginal:
    """Read one owner-authorized key, pinning a version for fixture originals."""

    def __init__(self, storage: S3MediaStorage, permit: OriginalRead):
        from .additional_original import physical_identity

        actual_endpoint = getattr(
            getattr(storage._client, "meta", None), "endpoint_url", None
        )
        self._client = storage._client
        self._bucket = storage.datasets_bucket
        if permit.ownership == "fixture":
            primary = (
                isinstance(actual_endpoint, str)
                and actual_endpoint.rstrip("/") == storage._endpoint
                and permit.endpoint_url.rstrip("/") == storage._endpoint
                and permit.bucket == storage.datasets_bucket
                and permit.storage_identity
                == physical_identity(storage._endpoint, storage.datasets_bucket)
            )
            if (
                storage._additional_original is not None
                and storage._additional_original.matches(permit)
            ):
                self._client = storage._additional_original.client
                self._bucket = storage._additional_original.bucket
            elif not primary:
                raise MediaFailure("source_changed")
        if (
            permit.ownership == "ordinary"
            and (permit.blocked_storage_identities or permit.source_storage_identity)
            and (
                not isinstance(actual_endpoint, str)
                or actual_endpoint.rstrip("/") != storage._endpoint
                or blocks_physical_source(
                    permit, storage._endpoint, storage.datasets_bucket
                )
            )
        ):
            raise MediaFailure("source_changed")
        self._storage = storage
        self._permit = permit
        self.original_sha256 = permit.sha256

    async def sign_source(self, key: str, expires_in: int) -> str | None:
        _validate_key(key)
        if key != self._permit.object_key:
            raise MediaFailure("source_changed")
        if self._permit.ownership == "fixture":
            return None
        return await asyncio.to_thread(
            self._storage._signer.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._storage.datasets_bucket, "Key": key},
            ExpiresIn=_valid_expiry(expires_in, maximum=604800),
        )

    async def head_source(self, key: str) -> ObjectIdentity:
        _validate_key(key)
        if key != self._permit.object_key:
            raise MediaFailure("source_changed")
        try:
            params = {"Bucket": self._bucket, "Key": key}
            if self._permit.ownership == "fixture":
                params["VersionId"] = self._permit.version_id
            response = await _run_blocking(self._client.head_object, **params)
            identity = _identity(response)
            if self._permit.ownership == "fixture" and (
                identity.version_id != self._permit.version_id
                or identity.etag != self._permit.etag.strip().strip('"')
                or identity.byte_size != self._permit.size_bytes
            ):
                raise MediaFailure("source_changed")
            return identity
        except MediaFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc, missing_is_object=True) from None

    async def download_source(self, source: MediaSource, destination: Path) -> None:
        _validate_key(source.file_path)
        identity = source.object_identity
        if source.file_path != self._permit.object_key or identity is None:
            raise MediaFailure("source_changed")
        if self._permit.ownership == "fixture" and (
            identity.version_id != self._permit.version_id
            or identity.etag != self._permit.etag.strip().strip('"')
            or identity.byte_size != self._permit.size_bytes
        ):
            raise MediaFailure("source_changed")
        await _run_blocking(
            self._storage._download_source,
            source,
            destination,
            self._client,
            self._bucket,
        )


class S3MediaStorage:
    """Use explicit credentials and fixed source/cache buckets only."""

    def __init__(
        self,
        *,
        endpoint: str,
        public_endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        datasets_bucket: str,
        cache_bucket: str,
        connect_timeout: float = 10,
        read_timeout: float = 120,
        max_attempts: int = 2,
        additional_original=None,
        additional_original_client=None,
        client=None,
        signer=None,
    ) -> None:
        self.datasets_bucket = datasets_bucket
        self._endpoint = endpoint.rstrip("/")
        self.cache_bucket = cache_bucket
        if client is None or signer is None:
            import boto3
            from botocore.config import Config

            config = Config(
                signature_version="s3v4",
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries={"mode": "standard", "total_max_attempts": max_attempts},
                s3={"addressing_style": "path"},
            )
            credentials = {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
                "region_name": region,
                "config": config,
            }
            if client is None:
                client = boto3.client("s3", endpoint_url=endpoint, **credentials)
            if signer is None:
                signer = boto3.client("s3", endpoint_url=public_endpoint, **credentials)
        self._client = client
        self._signer = signer
        from .additional_original import AdditionalOriginalTarget

        if additional_original_client is not None and additional_original is None:
            raise ValueError(
                "Additional original client requires explicit target configuration"
            )
        self._additional_original = (
            AdditionalOriginalTarget(
                additional_original,
                region=region,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                max_attempts=max_attempts,
                client=additional_original_client,
            )
            if additional_original is not None
            else None
        )

    def bind_original(self, permit: OriginalRead) -> BoundMediaOriginal:
        return BoundMediaOriginal(self, permit)

    def close_additional_original(self) -> None:
        if self._additional_original is not None:
            self._additional_original.close()

    def _download_source(
        self, source: MediaSource, destination: Path, client, bucket: str
    ) -> None:
        identity = source.object_identity
        assert identity is not None
        params: dict[str, object] = {
            "Bucket": bucket,
            "Key": source.file_path,
            "IfMatch": _if_match(identity.etag),
        }
        if identity.version_id is not None:
            params["VersionId"] = identity.version_id
        created = False
        try:
            response = client.get_object(**params)
            body = response.get("Body")
            if not callable(getattr(body, "read", None)) or not callable(
                getattr(body, "close", None)
            ):
                error = MediaFailure("storage_unavailable")
                error.storage_operation_unsettled = True
                raise error
            written = 0
            failure = None
            try:
                observed = _identity(response)
                if observed != identity:
                    raise MediaFailure("source_changed")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as output:
                    created = True
                    while True:
                        chunk = body.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > identity.byte_size:
                            raise MediaFailure("source_changed")
                        if output.write(chunk) != len(chunk):
                            raise OSError("short local media write")
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
                        "Media source read and physical close both failed",
                        [failure, close_error],
                    )
                    combined.storage_operation_unsettled = True
                    raise combined from None
            if written != identity.byte_size:
                raise MediaFailure("source_changed")
            if source.file_size is not None and written != source.file_size:
                raise MediaFailure("source_changed")
        except BaseException as exc:
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except BaseException as cleanup_error:
                    combined = builtins.BaseExceptionGroup(
                        "Media source read and local cleanup both failed",
                        [exc, cleanup_error],
                    )
                    if getattr(exc, "storage_operation_unsettled", False):
                        combined.storage_operation_unsettled = True
                    raise combined from None
            if (
                isinstance(exc, MediaFailure)
                or isinstance(exc, builtins.BaseExceptionGroup)
                or getattr(exc, "storage_operation_unsettled", False)
                or not isinstance(exc, Exception)
            ):
                raise
            raise _storage_error(exc) from None

    async def head_cache(self, key: str) -> ObjectIdentity | None:
        _validate_cache_read_key(key)
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self.cache_bucket,
                Key=key,
            )
            return _identity(response)
        except MediaFailure as exc:
            if exc.code == "object_missing":
                return None
            raise
        except Exception as exc:
            mapped = _storage_error(exc, missing_is_object=True)
            if mapped.code == "object_missing":
                return None
            raise mapped from None

    async def upload_cache(self, path: Path, key: str) -> Artifact:
        _validate_cache_key(key)
        try:
            return await _run_blocking(self._upload_cache, path, key)
        except MediaFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None

    def _upload_cache(self, path: Path, key: str) -> Artifact:
        content_type = {
            ".webp": "image/webp",
            ".jpeg": "image/jpeg",
            ".mp4": "video/mp4",
        }.get(path.suffix.lower())
        if content_type is None or not path.is_file():
            raise MediaFailure("invalid_response")
        digest = _sha256_file(path)
        byte_size = path.stat().st_size
        self._client.upload_file(
            str(path),
            self.cache_bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": digest}},
        )
        head = self._client.head_object(Bucket=self.cache_bucket, Key=key)
        identity = _identity(head)
        metadata = head.get("Metadata")
        if (
            identity.byte_size != byte_size
            or not isinstance(metadata, dict)
            or metadata.get("sha256") != digest
        ):
            raise MediaFailure("storage_unavailable")
        return Artifact(key=key, byte_size=byte_size, sha256=digest)

    async def delete_cache_keys(self, keys: list[str]) -> int:
        if len(keys) > 500 or len(set(keys)) != len(keys):
            raise MediaFailure("resource_budget_exceeded")
        for key in keys:
            _validate_cache_delete_key(key)
        if not keys:
            return 0
        return await asyncio.to_thread(self._delete_cache_keys, keys)

    def _delete_cache_keys(self, keys: list[str]) -> int:
        try:
            response = self._client.delete_objects(
                Bucket=self.cache_bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
            if response.get("Errors"):
                raise MediaFailure("storage_unavailable")
            return len(keys)
        except MediaFailure:
            raise
        except Exception as exc:
            raise _storage_error(exc) from None

    async def delete_cache_prefix(self, prefix: str) -> int:
        _validate_cache_prefix(prefix)
        return await asyncio.to_thread(self._delete_cache_prefix, prefix)

    def _delete_cache_prefix(self, prefix: str) -> int:
        deleted = 0
        continuation_token: str | None = None
        while True:
            request: dict[str, object] = {
                "Bucket": self.cache_bucket,
                "Prefix": prefix,
                "MaxKeys": 1000,
            }
            if continuation_token is not None:
                request["ContinuationToken"] = continuation_token
            try:
                page = self._client.list_objects_v2(**request)
                contents = page.get("Contents", [])
                keys = [
                    item["Key"] for item in contents if isinstance(item.get("Key"), str)
                ]
                if len(keys) != len(contents) or any(
                    not key.startswith(prefix) for key in keys
                ):
                    raise MediaFailure("invalid_response")
                if keys:
                    result = self._client.delete_objects(
                        Bucket=self.cache_bucket,
                        Delete={
                            "Objects": [{"Key": key} for key in keys],
                            "Quiet": True,
                        },
                    )
                    if result.get("Errors"):
                        raise MediaFailure("storage_unavailable")
                    deleted += len(keys)
            except MediaFailure:
                raise
            except Exception as exc:
                raise _storage_error(exc) from None
            if not page.get("IsTruncated"):
                return deleted
            continuation_token = page.get("NextContinuationToken")
            if not isinstance(continuation_token, str) or not continuation_token:
                raise MediaFailure("storage_unavailable")

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str:
        _validate_cache_read_key(key)
        return await asyncio.to_thread(
            self._signer.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.cache_bucket, "Key": key},
            ExpiresIn=_valid_expiry(expires_in, maximum=3600),
        )


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or not key or "\x00" in key:
        raise MediaFailure("invalid_request")


def _validate_cache_key(key: str) -> None:
    if not isinstance(key, str) or not _CACHE_KEY.fullmatch(key):
        raise MediaFailure("invalid_request")


def _validate_cache_read_key(key: str) -> None:
    if not isinstance(key, str) or not (
        _CACHE_KEY.fullmatch(key)
        or _LEGACY_FRAME_KEY.fullmatch(key)
        or _LEGACY_CHUNK_KEY.fullmatch(key)
        or _LEGACY_PLAYBACK_KEY.fullmatch(key)
    ):
        raise MediaFailure("invalid_request")


def _validate_cache_delete_key(key: str) -> None:
    if not isinstance(key, str) or not (
        _CACHE_KEY.fullmatch(key)
        or _LEGACY_FRAME_KEY.fullmatch(key)
        or _LEGACY_CHUNK_KEY.fullmatch(key)
    ):
        raise MediaFailure("invalid_request")


def _validate_cache_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not _CACHE_PREFIX.fullmatch(prefix):
        raise MediaFailure("invalid_request")


def _identity(response: object) -> ObjectIdentity:
    if not isinstance(response, dict):
        raise MediaFailure("storage_unavailable")
    etag = response.get("ETag")
    size = response.get("ContentLength")
    version_id = response.get("VersionId")
    if not isinstance(etag, str) or not etag.strip().strip('"'):
        raise MediaFailure("storage_unavailable")
    if type(size) is not int or size < 0:
        raise MediaFailure("storage_unavailable")
    if version_id is not None and not isinstance(version_id, str):
        raise MediaFailure("storage_unavailable")
    return ObjectIdentity(
        etag=etag.strip().strip('"'), byte_size=size, version_id=version_id
    )


def _if_match(etag: str) -> str:
    value = etag.strip().strip('"')
    if not value:
        raise MediaFailure("source_changed")
    return f'"{value}"'


def _valid_expiry(expires_in: int, *, maximum: int) -> int:
    if type(expires_in) is not int or not 1 <= expires_in <= maximum:
        raise MediaFailure("invalid_request")
    return expires_in


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_error(exc: Exception, *, missing_is_object: bool = False) -> MediaFailure:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = error.get("Code") if isinstance(error, dict) else None
    status = (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(response, dict)
        else None
    )
    if code in {"PreconditionFailed", "412"} or status == 412:
        return MediaFailure("source_changed")
    if missing_is_object and code in {"NoSuchKey", "NotFound", "404"}:
        return MediaFailure("object_missing")
    if (
        missing_is_object
        and status == 404
        and code
        not in {
            "NoSuchBucket",
            "NoSuchAccessPoint",
        }
    ):
        return MediaFailure("object_missing")
    return MediaFailure("storage_unavailable")


async def _run_blocking(function, /, *args, **kwargs):
    """Keep blocking work alive until it exits, even when its caller is canceled."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
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
                "Blocking media work was cancelled and then failed",
                [cancelled, late],
            )
            if getattr(late, "storage_operation_unsettled", False):
                combined.storage_operation_unsettled = True
            raise combined from None
        raise
