"""Conditional S3 reads and attempt-scoped image artifact operations."""

from __future__ import annotations

import asyncio
from builtins import BaseExceptionGroup
from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
from pathlib import Path
import re
import sys
from typing import AsyncIterator, Protocol
from urllib.parse import parse_qsl

import httpx

from yoao_assets_images.contract import (
    Artifact,
    ImageFailure,
    ImageOrphanCandidate,
    ImageSource,
    ObjectIdentity,
    PyramidMemberReceipt,
    PyramidMemberReservation,
    PyramidOutputAllocation,
)

from . import native_media
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
_NATIVE_SAVED_KEY = re.compile(
    rf"^[^\\]{{0,436}}image-pyramids/objects/{_UUID}/"
    r"(?:manifest\.json|overview\.webp|tiles/"
    r"(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp)$"
)
_HASH_CHUNK_BYTES = 1024 * 1024
_S3_LIST_PAGE_SIZE = 1000
_HTTPX_REQUEST_LOG_FORMAT = 'HTTP Request: %s %s "%s %d %s"'


def _mark_fixture_upload_uncertain(failure):
    from .original_read import fixture_original_active

    if fixture_original_active():
        failure.storage_operation_unsettled = True
    return failure


def _has_sigv4_query(url: httpx.URL) -> bool:
    query = {
        key.lower(): value
        for key, value in parse_qsl(url.query.decode("ascii", errors="ignore"))
    }
    return (
        query.get("x-amz-algorithm") == "AWS4-HMAC-SHA256"
        and "x-amz-credential" in query
        and "x-amz-signature" in query
    )


class _RedactSigV4HttpxLogFilter(logging.Filter):
    """Hide presigned query credentials from HTTPX's request INFO record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _HTTPX_REQUEST_LOG_FORMAT:
            return True
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        url = record.args[1]
        if not isinstance(url, httpx.URL) or not _has_sigv4_query(url):
            return True
        redacted_url = url.copy_with(query=b"redacted")
        record.args = (*record.args[:1], redacted_url, *record.args[2:])
        return True


_HTTPX_LOGGER = logging.getLogger("httpx")
if not any(
    isinstance(logger_filter, _RedactSigV4HttpxLogFilter)
    for logger_filter in _HTTPX_LOGGER.filters
):
    _HTTPX_LOGGER.addFilter(_RedactSigV4HttpxLogFilter())


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
    def bind_original(self, permit): ...

    async def head_cache(self, key: str) -> ObjectIdentity | None: ...

    async def read_cache(self, key: str, limit: int) -> bytes: ...

    async def upload_cache(self, path: Path, key: str) -> Artifact: ...

    async def upload_pyramid_member(
        self,
        path: Path,
        allocation: PyramidOutputAllocation,
        member: PyramidMemberReservation,
    ) -> PyramidMemberReceipt: ...

    async def verify_pyramid_member(
        self,
        allocation: PyramidOutputAllocation,
        member: PyramidMemberReservation,
        receipt: PyramidMemberReceipt,
    ) -> None: ...

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str: ...

    async def head_fixture_cache(self, key: str) -> ObjectIdentity: ...

    async def read_fixture_cache(self, key: str, limit: int) -> bytes: ...

    async def sign_fixture_cache(self, key: str, expires_in: int = 3600) -> str: ...

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
        additional_original_client=None,
        fixture_cache_resolver=None,
        native_writer=None,
    ) -> None:
        self.config = config
        self._fixture_cache_resolver = fixture_cache_resolver
        self._native_writer = native_writer
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
        from yoao_assets_media_runtime.additional_original import (
            AdditionalOriginalTarget,
        )

        if (
            additional_original_client is not None
            and config.additional_original is None
        ):
            raise ValueError(
                "Additional original client requires explicit target configuration"
            )
        self._additional_original = (
            AdditionalOriginalTarget(
                config.additional_original,
                region=config.region,
                connect_timeout=config.connect_timeout,
                read_timeout=config.read_timeout,
                max_attempts=config.max_attempts,
                client=additional_original_client,
            )
            if config.additional_original is not None
            else None
        )

    def bind_original(self, permit):
        if permit.ownership == "ordinary":
            from .ordinary_original import OrdinaryImageOriginal

            return OrdinaryImageOriginal(self, permit)
        from .original_storage import BoundImageOriginal

        return BoundImageOriginal(self, permit)

    def _get_http_client(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.config.read_timeout,
                    connect=self.config.connect_timeout,
                    pool=self.config.connect_timeout,
                ),
                follow_redirects=False,
                trust_env=False,
            )
        return self._http_client

    async def close(self) -> None:
        failure = None
        try:
            if self._owns_http_client and self._http_client is not None:
                client, self._http_client = self._http_client, None
                await client.aclose()
        except BaseException as exc:
            failure = exc
        try:
            writer, self._native_writer = self._native_writer, None
            if writer is not None:
                await writer.aclose()
        except BaseException as exc:
            if failure is not None:
                raise BaseExceptionGroup(
                    "Image storage close failed", [failure, exc]
                ) from None
            raise
        try:
            if self._additional_original is not None:
                self._additional_original.close()
        except BaseException as exc:
            if failure is not None:
                raise BaseExceptionGroup(
                    "Image storage close failed", [failure, exc]
                ) from None
            raise
        if failure is not None:
            raise failure

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

    async def head_cache(self, key: str) -> ObjectIdentity | None:
        _validate_cache_key(key)
        try:
            fixture_url = (
                await self._fixture_cache_resolver.url(key, 300)
                if self._fixture_cache_resolver is not None
                else None
            )
            if fixture_url is not None:
                response = await self._fixture_cache_resolver.http.head(fixture_url)
                if response.status_code != 200:
                    raise ImageFailure("storage_unavailable")
                return _http_identity(response.headers)
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
        fixture_url = (
            await self._fixture_cache_resolver.url(key, 300)
            if self._fixture_cache_resolver is not None
            else None
        )
        url = fixture_url or self._presigned_http_url(
            "get_object", {"Bucket": self.config.cache_bucket, "Key": key}
        )
        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    if fixture_url is not None:
                        raise ImageFailure("storage_unavailable")
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
        from yoao_assets_media_runtime.fixture_native_outputs import current

        writer = current()
        if writer is not None:
            return await self._upload_fixture_cache(
                path, key, digest, info.st_size, content_type, writer
            )
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(info.st_size),
            "x-amz-meta-sha256": digest,
        }
        url = self._presigned_http_url("put_object", params)
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
        except (ImageFailure, asyncio.CancelledError) as exc:
            _mark_fixture_upload_uncertain(exc)
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None
        finally:
            failure = sys.exception()
            try:
                await body.aclose()
            except BaseException as close_error:
                from .original_read import fixture_original_active

                if not fixture_original_active():
                    raise
                _mark_fixture_upload_uncertain(close_error)
                if isinstance(failure, asyncio.CancelledError):
                    _mark_fixture_upload_uncertain(failure)
                    raise failure from close_error
                if failure is not None:
                    combined = BaseExceptionGroup(
                        "Image upload and stream close both failed",
                        [failure, close_error],
                    )
                    _mark_fixture_upload_uncertain(combined)
                    raise combined from None
                raise

    async def upload_pyramid_member(
        self,
        path: Path,
        allocation: PyramidOutputAllocation,
        member: PyramidMemberReservation,
    ) -> PyramidMemberReceipt:
        """Write one reserved saved-output member and return its actual receipt.

        The caller has submitted this member's frozen reservation under the
        allocation's saved prefix.  An ordinary write executes only the
        Source-issued reserved-key PUT; its physical namespace is never
        reconstructed from the logical image-pyramid family or a configured
        cache bucket.  The returned ``PyramidMemberReceipt`` carries the raw
        quoted HTTP ETag and the raw nullable version, so a literal ``"null"``
        version stays a real recorded receipt.
        """
        key = allocation.storage_ref.prefix + member.relative_key
        digest, size = member.sha256, member.size_bytes
        path = Path(path)
        try:
            info = path.stat(follow_symlinks=False)
        except OSError:
            raise ImageFailure("invalid_response") from None
        if path.is_symlink() or not path.is_file() or info.st_size != size:
            raise ImageFailure("invalid_response")
        if _sha256_file(path) != digest:
            raise ImageFailure("invalid_response")
        content_type = native_media.content_type_for(member.relative_key)
        from yoao_assets_media_runtime.fixture_native_outputs import current

        writer = current()
        if writer is not None:
            return await self._upload_fixture_pyramid_member(
                writer,
                path,
                key,
                digest,
                size,
                content_type,
                member.operation_id,
            )
        if self._native_writer is None:
            raise ImageFailure("storage_unavailable")
        request = self._native_write_request(allocation, member)
        signed = native_media.write_member(
            await self._native_context(self._native_writer.write_reference, request),
            request=request,
            output_id=allocation.output_id,
            storage_ref=allocation.storage_ref,
            reservation=member,
        )
        body = _FileBody(path, size, digest)
        failure: BaseException | None = None
        receipt: PyramidMemberReceipt | None = None
        try:
            observed = await native_media.put_member(
                self._get_http_client(),
                signed.put,
                content=body,
                relative_key=member.relative_key,
                size_bytes=size,
                sha256=digest,
            )
            receipt = PyramidMemberReceipt(
                operation_id=member.operation_id,
                etag=observed.etag,
                version_id=observed.version_id,
            )
        except (ImageFailure, asyncio.CancelledError) as exc:
            failure = exc
            if not isinstance(exc, native_media.NativeWriteRefused):
                # The request may have reached the saved location; only the
                # recorded receipt can prove otherwise.
                exc.storage_operation_unsettled = True
            raise
        except Exception:
            failure = ImageFailure("storage_unavailable")
            failure.storage_operation_unsettled = True
            raise failure from None
        finally:
            try:
                await body.aclose()
            except BaseException as close_error:
                if not isinstance(failure, native_media.NativeWriteRefused):
                    # Observed response headers are not closure proof: the
                    # client/body operation may still be open.  Never register
                    # this receipt and never report it as not_written.
                    failure = failure or ImageFailure("storage_unavailable")
                    failure.storage_operation_unsettled = True
                    raise failure from close_error
        assert receipt is not None
        return receipt

    async def verify_pyramid_member(
        self,
        allocation: PyramidOutputAllocation,
        member: PyramidMemberReservation,
        receipt: PyramidMemberReceipt,
    ) -> None:
        """Fully read back the recorded saved member before verification.

        A fixture-native write already proved its exact bytes through its own
        writer receipt, so it is not read a second time.  The ordinary lane
        proves the durable recorded receipt with one conditional full GET.
        """
        from yoao_assets_media_runtime.fixture_native_outputs import current

        if current() is not None:
            return
        if self._native_writer is None:
            raise ImageFailure("storage_unavailable")
        request = self._native_write_request(allocation, member)
        signed = native_media.verify_member(
            await self._native_context(self._native_writer.verify_reference, request),
            request=request,
            output_id=allocation.output_id,
            storage_ref=allocation.storage_ref,
            reservation=member,
            etag=receipt.etag,
            version_id=receipt.version_id,
        )
        await native_media.get_member(
            self._get_http_client(),
            signed.get,
            member=signed.member,
            limit=member.size_bytes,
        )

    @staticmethod
    def _native_write_request(allocation, member):
        from yoao_assets_images.native_storage_contract import (
            NativePyramidWriteIoRequest,
        )

        return NativePyramidWriteIoRequest(
            operation_id=member.operation_id,
            job_id=allocation.job_id,
            job_generation=allocation.job_generation,
            lease_token=allocation.lease_token,
        )

    async def _native_context(self, call, request):
        try:
            return await call(request)
        except asyncio.CancelledError:
            raise
        except ImageFailure:
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def _upload_fixture_pyramid_member(
        self, writer, path, key, digest, size, content_type, operation_id
    ) -> PyramidMemberReceipt:
        from yoao_assets_media_runtime.storage import _run_blocking

        await writer.prepare()

        try:
            return await _run_blocking(
                self._fixture_member_put,
                writer,
                path,
                key,
                digest,
                size,
                content_type,
                operation_id,
            )
        except BaseException as exc:
            exc.storage_operation_unsettled = True
            raise

    @staticmethod
    def _fixture_member_put(
        writer, path, key, digest, size, content_type, operation_id
    ) -> PyramidMemberReceipt:
        from yoao_assets_media_runtime.fixture_native_outputs import verify_object

        with path.open("rb") as source:
            try:
                result = writer.client.put_object(
                    Bucket=writer.bucket,
                    Key=key,
                    Body=source,
                    ContentLength=size,
                    ContentType=content_type,
                    Metadata={"sha256": digest},
                    IfNoneMatch="*",
                )
            except Exception as exc:
                code = str(
                    getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                )
                if code in {"PreconditionFailed", "412"}:
                    raise ImageFailure("upload_verification_failed") from None
                raise
        etag, version = result.get("ETag"), result.get("VersionId")
        if (
            result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or not isinstance(etag, str)
            or not etag
            or not isinstance(version, str)
            or not version
            or version == "null"
        ):
            raise ImageFailure("upload_verification_failed")
        verify_object(writer.client, writer.bucket, key, digest, size, etag, version)
        return PyramidMemberReceipt(
            operation_id=operation_id,
            etag=etag,
            version_id=version,
        )

    @staticmethod
    def _fixture_put(writer, path, key, digest, size, content_type):
        with path.open("rb") as source:
            result = writer.client.put_object(
                Bucket=writer.bucket,
                Key=key,
                Body=source,
                ContentLength=size,
                ContentType=content_type,
                Metadata={"sha256": digest},
                IfNoneMatch="*",
            )
        etag, version = result.get("ETag"), result.get("VersionId")
        if (
            result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or not isinstance(etag, str)
            or not etag
            or not isinstance(version, str)
            or not version
            or version == "null"
        ):
            raise RuntimeError("fixture native cache PUT receipt is invalid")
        return etag, version

    async def _upload_fixture_cache(
        self, path, key, digest, size, content_type, writer
    ):
        from yoao_assets_media_runtime.fixture_native_outputs import verify_object
        from yoao_assets_media_runtime.storage import _run_blocking
        from .original_read import fixture_upload_started

        intent = await writer.begin(key, digest, size)
        fixture_upload_started()
        try:
            etag, version = await _run_blocking(
                self._fixture_put, writer, path, key, digest, size, content_type
            )
            await writer.advance(intent, "written", etag, version)
            await _run_blocking(
                verify_object,
                writer.client,
                writer.bucket,
                key,
                digest,
                size,
                etag,
                version,
            )
            await writer.advance(intent, "verified", etag, version)
            return Artifact(key=key, byte_size=size, sha256=digest)
        except BaseException as exc:
            exc.storage_operation_unsettled = True
            raise

    async def sign_cache(self, key: str, expires_in: int = 3600) -> str:
        _validate_cache_key(key)
        try:
            fixture_url = (
                await self._fixture_cache_resolver.url(key, expires_in)
                if self._fixture_cache_resolver is not None
                else None
            )
            if fixture_url is not None:
                return fixture_url
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

    async def _fixture_url(self, key: str, expires_in: int) -> str:
        if self._fixture_cache_resolver is None:
            raise ImageFailure("storage_unavailable")
        url = await self._fixture_cache_resolver.url(key, expires_in)
        if not isinstance(url, str) or not url:
            raise ImageFailure("object_missing")
        return url

    async def head_fixture_cache(self, key: str) -> ObjectIdentity:
        """Head one fixture-owned saved member; never the configured cache bucket."""
        _validate_native_saved_key(key)
        try:
            url = await self._fixture_url(key, 300)
            response = await self._get_http_client().head(url)
            if response.status_code == 404:
                raise ImageFailure("object_missing")
            if response.status_code != 200:
                raise ImageFailure("storage_unavailable")
            return _http_identity(response.headers)
        except (ImageFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def read_fixture_cache(self, key: str, limit: int) -> bytes:
        """Read one fixture-owned saved member without any configured-bucket fallback."""
        _validate_native_saved_key(key)
        if type(limit) is not int or not 1 <= limit <= 16 * 1024 * 1024:
            raise ImageFailure("resource_budget_exceeded")
        try:
            url = await self._fixture_url(key, 300)
            async with self._get_http_client().stream("GET", url) as response:
                if response.status_code == 404:
                    raise ImageFailure("object_missing")
                if response.status_code != 200:
                    raise ImageFailure("storage_unavailable")
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
        except (ImageFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

    async def sign_fixture_cache(self, key: str, expires_in: int = 3600) -> str:
        """Sign one fixture-owned saved member; never the configured cache bucket."""
        _validate_native_saved_key(key)
        _expiry(expires_in)
        try:
            return await self._fixture_url(key, expires_in)
        except (ImageFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise ImageFailure("storage_unavailable") from None

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


def _validate_native_saved_key(key: str) -> None:
    """Only exact canonical members of one saved native output are operable."""
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 2048
        or "\x00" in key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or _NATIVE_SAVED_KEY.fullmatch(key) is None
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
