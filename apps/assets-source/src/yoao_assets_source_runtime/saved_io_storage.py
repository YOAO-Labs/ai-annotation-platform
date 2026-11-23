"""Physical I/O primitives available only to trusted, registered Source handlers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
import re
from urllib.parse import quote

import boto3
from anyio import CancelScope
from botocore.config import Config
from botocore.exceptions import ClientError

from yoao_assets_source import managed_original
from yoao_assets_source.connectors.host_guard import ConnectorHostDenied
from yoao_assets_source.connectors.s3_socket import open_pinned_s3
from yoao_assets_source.saved_io import STORAGE_CODES, SavedIOError

PURPOSES = frozenset(
    {
        "annotations",
        "audit-archive",
        "avatars",
        "bug-reports",
        "datasets",
        "export",
        "import",
        "media-cache",
        "training",
    }
)
_CONTENT_FIELDS = frozenset(
    {
        "CacheControl",
        "ContentDisposition",
        "ContentEncoding",
        "ContentLanguage",
        "ContentType",
        "Expires",
        "WebsiteRedirectLocation",
    }
)
_OBJECT_FIELDS = _CONTENT_FIELDS | frozenset(
    {
        "ContentLength",
        "ContentRange",
        "DeleteMarker",
        "ETag",
        "LastModified",
        "Metadata",
        "VersionId",
        "ChecksumCRC32",
        "ChecksumCRC32C",
        "ChecksumCRC64NVME",
        "ChecksumSHA1",
        "ChecksumSHA256",
        "ChecksumType",
    }
)
_NOT_FOUND = frozenset(
    {"404", "NoSuchBucket", "NoSuchKey", "NoSuchVersion", "NotFound"}
)
_CONFLICT = frozenset(
    {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}
)
_FORBIDDEN = frozenset(
    {
        "403",
        "AccessDenied",
        "InvalidAccessKeyId",
        "InvalidToken",
        "SignatureDoesNotMatch",
    }
)
_RANGE = re.compile(r"bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})\Z")


def safe_text(value, limit, *, empty=False):
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
        and len(value.encode("utf-8")) <= limit
    )


def managed_roots(config, purpose):
    base = config.get("base_prefix", "")
    if not safe_text(base, 1024, empty=True) or "\\" in base:
        raise SavedIOError("target_unproven")
    base = base.removesuffix("/")
    if base and any(part in {"", ".", ".."} for part in base.split("/")):
        raise SavedIOError("target_unproven")
    parent = base + "/" if base else ""
    if purpose == "export":
        return (parent + "_yoao_managed/export/", parent + "_aap_managed/export/")
    if purpose == "migration":
        return (parent + "_aap_migration/",)
    if purpose not in PURPOSES:
        raise SavedIOError("target_unproven")
    return (parent + f"_aap_managed/{purpose}/",)


async def physical_thread(call, /, *args, _discard=None, **kwargs):
    """Drain a started native call before releasing its enclosing permit or body."""
    job = asyncio.get_running_loop().run_in_executor(
        None, partial(call, *args, **kwargs)
    )
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        with CancelScope(shield=True):
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            with suppress(BaseException):
                result = job.result()
                if _discard is not None:
                    await physical_thread(_discard, result)
        raise


@dataclass
class Download:
    body: object
    length: int
    metadata: dict
    close_action: object = None
    after_body: object = None

    def __init__(self, body, *, length, metadata, close=None, after_body=None):
        self.body = body
        self.length = length
        self.metadata = metadata
        self.close_action = close
        self.after_body = after_body
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self.body.close()
            finally:
                if self.close_action is not None:
                    self.close_action()
        except Exception:
            raise SavedIOError(
                "source_close_unsettled", status_code=503, unsettled=True
            ) from None


@dataclass(frozen=True, repr=False)
class PhysicalTarget:
    bucket: str
    settings: object
    config: dict | None = None
    secret: dict | None = None
    allowlist: tuple[str, ...] = ()

    def open(self):
        config = self.settings
        if self.config is not None:
            return open_pinned_s3(
                self.config,
                self.secret,
                self.allowlist,
                connect_timeout=config.s3_connect_timeout_seconds,
                read_timeout=config.s3_read_timeout_seconds,
            )
        return boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint,
            aws_access_key_id=config.s3_access_key,
            aws_secret_access_key=config.s3_secret_key,
            region_name=config.s3_region,
            config=Config(
                connect_timeout=config.s3_connect_timeout_seconds,
                read_timeout=config.s3_read_timeout_seconds,
                retries={"total_max_attempts": 1, "mode": "standard"},
                proxies={},
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @asynccontextmanager
    async def client(self):
        try:
            client = await physical_thread(self.open, _discard=lambda c: c.close())
        except Exception as exc:
            raise physical_error(exc) from None
        try:
            yield client
        finally:
            await physical_thread(_close_client, client)


def _close_client(client):
    try:
        client.close()
    except Exception:
        raise SavedIOError(
            "source_close_unsettled", status_code=503, unsettled=True
        ) from None


def physical_error(exc, *, mutation=False):
    if isinstance(exc, SavedIOError):
        return exc
    if isinstance(exc, ConnectorHostDenied):
        return SavedIOError("connector_host_denied", status_code=403)
    if isinstance(exc, ClientError):
        provider_code = exc.response.get("Error", {}).get("Code")
        code = str(provider_code) if provider_code is not None else None
        if code in _NOT_FOUND:
            return SavedIOError("object_missing", status_code=404, storage_code=code)
        if code in _CONFLICT:
            return SavedIOError(
                "storage_precondition_failed", status_code=412, storage_code=code
            )
        if code in _FORBIDDEN:
            return SavedIOError(
                "storage_access_denied", status_code=403, storage_code=code
            )
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in {400, 405, 411, 413, 416, 422, 501} and code in STORAGE_CODES:
            return SavedIOError(
                "storage_request_rejected", status_code=409, storage_code=code
            )
    return SavedIOError(
        "storage_unavailable",
        status_code=503,
        unsettled=mutation
        or bool(getattr(exc, "storage_operation_unsettled", False))
        or getattr(exc, "outcome", None) == "unknown",
    )


def _values(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_values(item) for item in value]
    return value


def _observed(response, fields):
    result = {key: _values(value) for key, value in response.items() if key in fields}
    metadata = response.get("ResponseMetadata")
    if isinstance(metadata, dict):
        result["ResponseMetadata"] = {"HTTPStatusCode": metadata.get("HTTPStatusCode")}
    return result


def _receipt(response, fields, statuses, *, mutation=False):
    result = _observed(response, fields)
    if result.get("ResponseMetadata", {}).get("HTTPStatusCode") not in statuses:
        raise SavedIOError(
            "storage_invalid_receipt", status_code=502, unsettled=mutation
        )
    return result


def _content_fields(content_type, content_fields):
    fields = {} if content_fields is None else dict(content_fields)
    if fields.keys() - _CONTENT_FIELDS:
        raise SavedIOError("invalid_request", status_code=400)
    if content_type is not None:
        fields["ContentType"] = content_type
    for name, value in fields.items():
        if name == "Expires":
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    raise SavedIOError("invalid_request", status_code=400) from None
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise SavedIOError("invalid_request", status_code=400)
            fields[name] = value
        elif not safe_text(value, 2048, empty=True):
            raise SavedIOError("invalid_request", status_code=400)
    return fields


def _metadata(metadata):
    if metadata is None:
        return {}
    if (
        not isinstance(metadata, dict)
        or any(
            not safe_text(k, 256)
            or not safe_text(v, 2048, empty=True)
            or not k.isascii()
            or not v.isascii()
            for k, v in metadata.items()
        )
        or sum(len(k) + len(v) for k, v in metadata.items()) > 2048
    ):
        raise SavedIOError("invalid_request", status_code=400)
    return {"Metadata": metadata}


def _condition(etag):
    if etag is None:
        return {}
    if not safe_text(etag, 1024):
        raise SavedIOError("invalid_request", status_code=400)
    return {"IfMatch": etag}


def _conditional_create(value):
    if value is None:
        return {}
    if value != "*":
        raise SavedIOError("invalid_request", status_code=400)
    return {"IfNoneMatch": "*"}


@dataclass(frozen=True, repr=False)
class PinnedS3:
    _target: PhysicalTarget
    key: str
    version: str | None = None

    def __post_init__(self):
        if not safe_text(self.key, 1024) or (
            self.version is not None and not safe_text(self.version, 1024)
        ):
            raise SavedIOError("target_unproven")

    @property
    def bucket(self):
        return self._target.bucket

    def with_version(self, version):
        return replace(self, version=version)

    def client(self):
        return self._target.client()

    def _params(self):
        return {
            "Bucket": self.bucket,
            "Key": self.key,
            **({"VersionId": self.version} if self.version is not None else {}),
        }

    def _check_read(self, result, if_match):
        version = result.get("VersionId")
        if self.version is not None and not (
            version == self.version or self.version == "null" and version is None
        ):
            raise SavedIOError("source_changed")
        if result.get("DeleteMarker"):
            raise SavedIOError("object_missing", status_code=404)
        if if_match not in {None, "*"}:
            etag = result.get("ETag")
            if not isinstance(etag, str) or etag.strip('"') != if_match.strip('"'):
                raise SavedIOError(
                    "source_changed", status_code=412, storage_code="PreconditionFailed"
                )
        return result

    async def head(self, if_match=None):
        async with self.client() as client:
            try:
                response = await physical_thread(
                    client.head_object, **self._params(), **_condition(if_match)
                )
                return self._check_read(
                    _receipt(response, _OBJECT_FIELDS, {200}), if_match
                )
            except Exception as exc:
                raise physical_error(exc) from None

    async def get(self, range=None, if_match=None):
        if range is not None and (
            not isinstance(range, str) or not _RANGE.fullmatch(range)
        ):
            raise SavedIOError("invalid_request", status_code=400)
        params = {**self._params(), **_condition(if_match)}
        if range is not None:
            params["Range"] = range

        def open_body():
            client = self._target.open()
            body = None
            try:
                response = client.get_object(**params)
                body = response["Body"]
                result = self._check_read(
                    _receipt(
                        response, _OBJECT_FIELDS, {200} if range is None else {206}
                    ),
                    if_match,
                )
                length = result.get("ContentLength")
                if (
                    type(length) is not int
                    or length < 0
                    or (
                        range is None
                        and "ContentRange" in result
                        or range is not None
                        and not result.get("ContentRange")
                    )
                ):
                    raise SavedIOError("storage_invalid_receipt", status_code=502)
                return Download(
                    body,
                    length=length,
                    metadata=result,
                    close=lambda: _close_client(client),
                )
            except BaseException:
                try:
                    if body is not None:
                        body.close()
                finally:
                    _close_client(client)
                raise

        try:
            return await physical_thread(open_body, _discard=lambda d: d.close())
        except Exception as exc:
            raise physical_error(exc) from None

    async def put(
        self,
        body,
        *,
        length,
        content_type=None,
        metadata=None,
        if_none_match=None,
        multipart=False,
        content_fields=None,
    ):
        if self.version is not None or type(length) is not int or length < 0:
            raise SavedIOError("invalid_request", status_code=400)
        fields = {
            **_content_fields(content_type, content_fields),
            **_metadata(metadata),
        }
        condition = _conditional_create(if_none_match)
        async with self.client() as client:
            try:
                if multipart and length:
                    result = await physical_thread(
                        _multipart,
                        client,
                        self._params(),
                        body,
                        length,
                        fields,
                        condition,
                    )
                else:
                    result = await physical_thread(
                        client.put_object,
                        **self._params(),
                        **fields,
                        **condition,
                        Body=body,
                        ContentLength=length,
                    )
                return _receipt(result, _OBJECT_FIELDS, {200}, mutation=True)
            except Exception as exc:
                raise physical_error(exc, mutation=True) from None

    async def delete(self, if_match=None):
        async with self.client() as client:
            try:
                result = await physical_thread(
                    client.delete_object, **self._params(), **_condition(if_match)
                )
                return _receipt(result, _OBJECT_FIELDS, {200, 204}, mutation=True)
            except Exception as exc:
                raise physical_error(exc, mutation=True) from None

    async def get_bucket_versioning(self):
        async with self.client() as client:
            try:
                result = await physical_thread(
                    client.get_bucket_versioning, Bucket=self.bucket
                )
                return _receipt(result, {"Status", "MFADelete"}, {200})
            except Exception as exc:
                raise physical_error(exc) from None

    async def list_object_versions(self, max_keys=2):
        async with self.client() as client:
            return await physical_thread(
                ManagedPrefix(self._target, self.key, client).list_object_versions,
                MaxKeys=max_keys,
            )

    async def sign(
        self,
        method,
        *,
        expires_in,
        content_type=None,
        metadata=None,
        if_none_match=None,
        length=None,
        download_name=None,
        response_content_disposition=None,
        response_content_type=None,
        if_match=None,
    ):
        operations = {"GET": "get_object", "HEAD": "head_object", "PUT": "put_object"}
        if (
            method not in operations
            or type(expires_in) is not int
            or not 0 < expires_in <= 604800
        ):
            raise SavedIOError("invalid_request", status_code=400)
        params = self._params()
        if method == "PUT":
            if self.version is not None or any(
                v is not None
                for v in (
                    download_name,
                    response_content_disposition,
                    response_content_type,
                    if_match,
                )
            ):
                raise SavedIOError("invalid_request", status_code=400)
            params.update(_content_fields(content_type, None))
            params.update(_metadata(metadata))
            params.update(_conditional_create(if_none_match))
            if length is not None:
                if type(length) is not int or length < 0:
                    raise SavedIOError("invalid_request", status_code=400)
                params["ContentLength"] = length
        else:
            if any(
                value is not None
                for value in (content_type, metadata, if_none_match, length)
            ):
                raise SavedIOError("invalid_request", status_code=400)
            params.update(_condition(if_match))
            if download_name is not None:
                if (
                    not safe_text(download_name, 255)
                    or response_content_disposition is not None
                ):
                    raise SavedIOError("invalid_request", status_code=400)
                response_content_disposition = "attachment; filename*=UTF-8''" + quote(
                    download_name, safe=""
                )
            for name, value in (
                ("ResponseContentDisposition", response_content_disposition),
                ("ResponseContentType", response_content_type),
            ):
                if value is not None:
                    if method != "GET" or not safe_text(value, 2048):
                        raise SavedIOError("invalid_request", status_code=400)
                    params[name] = value
        async with self.client() as client:
            try:
                return await physical_thread(
                    client.generate_presigned_url,
                    operations[method],
                    Params=params,
                    ExpiresIn=expires_in,
                    HttpMethod=method,
                )
            except Exception as exc:
                raise physical_error(exc) from None


def _multipart(client, params, body, length, fields, condition):
    if isinstance(body, bytes):
        import io

        body = io.BytesIO(body)
    if length > managed_original.PART_BYTES * managed_original.MAX_PARTS:
        raise SavedIOError("request_too_large", status_code=413)
    started = client.create_multipart_upload(**params, **fields)
    upload_id = started.get("UploadId")
    if not safe_text(upload_id, 4096):
        raise SavedIOError("storage_invalid_receipt", status_code=502, unsettled=True)
    args = {**params, "UploadId": upload_id}
    completing = False
    parts = []
    try:
        remaining = length
        while remaining:
            size = min(remaining, managed_original.PART_BYTES)
            chunk = body.read(size)
            if not isinstance(chunk, bytes) or len(chunk) != size:
                raise SavedIOError("upload_length_mismatch", status_code=400)
            number = len(parts) + 1
            part = client.upload_part(
                **args, PartNumber=number, Body=chunk, ContentLength=size
            )
            if not part.get("ETag"):
                raise SavedIOError("storage_invalid_receipt", status_code=502)
            parts.append({"ETag": part["ETag"], "PartNumber": number})
            remaining -= size
        if body.read(1):
            raise SavedIOError("upload_length_mismatch", status_code=400)
        completing = True
        return client.complete_multipart_upload(
            **args, **condition, MultipartUpload={"Parts": parts}
        )
    except Exception as exc:
        error = physical_error(exc, mutation=completing)
        if not completing or not error.unsettled:
            try:
                result = client.abort_multipart_upload(**args)
                _receipt(result, (), {200, 204}, mutation=True)
            except Exception:
                raise SavedIOError(
                    "multipart_abort_unsettled", status_code=503, unsettled=True
                ) from None
        raise error from None


class ManagedPrefix:
    def __init__(self, target, prefix, client):
        self._target = target
        self._client = client
        self.bucket = target.bucket
        self.prefix = prefix

    def object(self, key, *, version=None):
        if not isinstance(key, str) or not key.startswith(self.prefix):
            raise SavedIOError("target_unproven")
        return PinnedS3(self._target, key, version)

    def _list(self, operation, *, Prefix, MaxKeys, **kwargs):
        if (
            not isinstance(Prefix, str)
            or not Prefix.startswith(self.prefix)
            or not safe_text(Prefix, 1024)
        ):
            raise SavedIOError("target_unproven")
        if type(MaxKeys) is not int or not 1 <= MaxKeys <= 1000:
            raise SavedIOError("invalid_request", status_code=400)
        for name, value in kwargs.items():
            if (
                not safe_text(value, 4096)
                or name in {"StartAfter", "KeyMarker"}
                and not value.startswith(Prefix)
            ):
                raise SavedIOError("invalid_request", status_code=400)
        try:
            raw = operation(
                Bucket=self.bucket, Prefix=Prefix, MaxKeys=MaxKeys, **kwargs
            )
            result = _receipt(
                raw,
                {
                    "IsTruncated",
                    "NextContinuationToken",
                    "NextKeyMarker",
                    "NextVersionIdMarker",
                    "KeyCount",
                    "Contents",
                    "Versions",
                    "DeleteMarkers",
                    "CommonPrefixes",
                },
                {200},
            )
            total = 0
            for collection, identifier in (
                ("Contents", "Key"),
                ("Versions", "Key"),
                ("DeleteMarkers", "Key"),
                ("CommonPrefixes", "Prefix"),
            ):
                if collection not in result:
                    continue
                values = result[collection]
                total += len(values)
                if not isinstance(values, list) or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get(identifier), str)
                    or not item[identifier].startswith(Prefix)
                    for item in values
                ):
                    raise SavedIOError("storage_invalid_receipt", status_code=502)
                fields = {
                    identifier,
                    "ETag",
                    "Size",
                    "LastModified",
                    "VersionId",
                    "IsLatest",
                    "StorageClass",
                }
                result[collection] = [
                    {name: item[name] for name in fields if name in item}
                    for item in values
                ]
            if (
                total > MaxKeys
                or result.get("NextKeyMarker")
                and not result["NextKeyMarker"].startswith(Prefix)
            ):
                raise SavedIOError("storage_invalid_receipt", status_code=502)
            return result
        except Exception as exc:
            raise physical_error(exc) from None

    def list_objects_v2(
        self,
        *,
        Prefix=None,
        MaxKeys=200,
        ContinuationToken=None,
        StartAfter=None,
        Delimiter=None,
    ):
        if Delimiter not in {None, "/"}:
            raise SavedIOError("invalid_request", status_code=400)
        optional = {
            "ContinuationToken": ContinuationToken,
            "StartAfter": StartAfter,
            "Delimiter": Delimiter,
        }
        return self._list(
            self._client.list_objects_v2,
            Prefix=self.prefix if Prefix is None else Prefix,
            MaxKeys=MaxKeys,
            **{k: v for k, v in optional.items() if v is not None},
        )

    def list_object_versions(
        self, *, Prefix=None, MaxKeys=200, KeyMarker=None, VersionIdMarker=None
    ):
        optional = {"KeyMarker": KeyMarker, "VersionIdMarker": VersionIdMarker}
        return self._list(
            self._client.list_object_versions,
            Prefix=self.prefix if Prefix is None else Prefix,
            MaxKeys=MaxKeys,
            **{k: v for k, v in optional.items() if v is not None},
        )

    def get_bucket_versioning(self):
        try:
            result = self._client.get_bucket_versioning(Bucket=self.bucket)
            return _receipt(result, {"Status", "MFADelete"}, {200})
        except Exception as exc:
            raise physical_error(exc) from None
