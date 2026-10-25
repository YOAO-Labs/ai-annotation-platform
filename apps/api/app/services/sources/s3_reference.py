"""Exact, bounded S3 reads for persistent references; never writes the source."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from urllib.parse import unquote_plus, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.crypto import (
    decrypt_secret,
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
)
from app.services import connector_guard
from app.services.storage_access import close_registered, mark_unsettled
from app.services.filesystem_storage import FilesystemStorageError as StorageError

PAGE_SIZE = 200


def directory_prefix(base, relative=""):
    for value in (base, relative):
        if not isinstance(value, str) or "\x00" in value:
            raise StorageError("invalid_s3_prefix")
    return (base + ("/" if base and not base.endswith("/") else "")) + (
        relative + ("/" if relative and not relative.endswith("/") else "")
    )


def decode_key(value):
    if not isinstance(value, str) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise StorageError("s3_invalid_listing")
    try:
        result = unquote_plus(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise StorageError("s3_invalid_listing") from None
    if "\x00" in result or len(result.encode()) > 1024:
        raise StorageError("s3_invalid_key")
    return result


def observation(response, bucket, key):
    etag = response.get("ETag")
    modified = response.get("LastModified")
    size = response.get("ContentLength")
    if (
        not isinstance(etag, str)
        or not etag
        or not isinstance(size, int)
        or size < 0
        or not isinstance(modified, datetime)
    ):
        raise StorageError("s3_invalid_observation")
    return {
        "kind": "s3",
        "bucket": bucket,
        "key": key,
        "size": size,
        "etag": etag,
        "last_modified": modified.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "version_id": response.get("VersionId"),
    }


def matches(actual, expected):
    return all(actual.get(key) == value for key, value in expected.items())


def read_observation(response, bucket, key):
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("ContentRange") is not None
        or response.get("DeleteMarker")
    ):
        raise StorageError("s3_invalid_response")
    return observation(response, bucket, key)


def require_conditions(client, params):
    try:
        response = client.get_object(**{**params, "IfMatch": '"aap-impossible-etag"'})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {
            "PreconditionFailed",
            "412",
        }:
            raise
    else:
        close_registered(response["Body"])
        raise StorageError("s3_conditions_not_supported")


def allowed_endpoint(endpoint, allowlist):
    try:
        connector_guard.assert_host_allowed(endpoint, list(allowlist))
    except connector_guard.ConnectorHostDenied:
        raise StorageError("connector_host_denied") from None


@dataclass(frozen=True, repr=False)
class S3Access:
    config: dict
    secret_enc: bytes
    allowlist: tuple[str, ...]

    @property
    def anchor(self):
        return {
            "kind": "s3",
            "endpoint": self.endpoint,
            "bucket": self.config["bucket"],
            "prefix": directory_prefix(self.config.get("base_prefix", "")),
        }

    @property
    def endpoint(self):
        endpoint = self.config["endpoint"]
        if "://" not in endpoint:
            endpoint = (
                "https://" if self.config.get("use_ssl") else "http://"
            ) + endpoint
        parts = urlsplit(endpoint)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in {"", "/"}
        ):
            raise StorageError("s3_endpoint_not_supported")
        return endpoint.rstrip("/")

    @contextmanager
    def client(self):
        endpoint = self.endpoint
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.config["bucket"]):
            raise StorageError("s3_bucket_not_supported")
        allowed_endpoint(endpoint, self.allowlist)
        try:
            secret = decrypt_secret(self.secret_enc)
        except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
            raise StorageError("connection_unavailable") from None
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=secret["access_key"],
                aws_secret_access_key=secret["secret_key"],
                region_name=self.config.get("region"),
                config=Config(
                    connect_timeout=2,
                    read_timeout=2,
                    retries={"max_attempts": 0},
                    s3={"addressing_style": "path"},
                    proxies={},
                    signature_version="s3v4",
                ),
            )
        except BaseException as exc:
            raise mark_unsettled(exc)

        def guard(request, **kwargs):
            destination = urlsplit(request.url)
            target = urlsplit(endpoint)
            if (destination.scheme, destination.netloc) != (
                target.scheme,
                target.netloc,
            ):
                raise StorageError("s3_redirect_denied")
            allowed_endpoint(endpoint, self.allowlist)

        try:
            client.meta.events.register("before-send.s3", guard)
            yield client
        except ClientError as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            code = exc.response.get("Error", {}).get("Code")
            failure = StorageError(
                "source_changed"
                if code in {"PreconditionFailed", "412"}
                else "permission_denied"
                if code in {"AccessDenied", "403"}
                else "path_unavailable"
                if code in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"}
                else "content_source_unavailable"
            )
            if code in {
                "AccessDenied",
                "403",
                "SignatureDoesNotMatch",
                "InvalidAccessKeyId",
            }:
                failure.storage_fault_category = "access_denied"
            raise failure from None
        except (BotoCoreError, connector_guard.ConnectorHostDenied) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise StorageError("content_source_unavailable") from None
        finally:
            close_registered(client)

    def list_page(self, relative, cursor=None, *, directories=False):
        prefix = directory_prefix(self.config.get("base_prefix", ""), relative)
        kwargs = {
            "Bucket": self.config["bucket"],
            "Prefix": prefix,
            "MaxKeys": PAGE_SIZE,
            "EncodingType": "url",
        }
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                raise StorageError("s3_invalid_cursor")
            kwargs["ContinuationToken"] = cursor
        if directories:
            kwargs["Delimiter"] = "/"
        with self.client() as client:
            response = client.list_objects_v2(**kwargs)
        if response.get("EncodingType") != "url":
            raise StorageError("s3_invalid_listing")
        next_cursor = (
            response.get("NextContinuationToken")
            if response.get("IsTruncated")
            else None
        )
        if response.get("IsTruncated") and (
            not isinstance(next_cursor, str)
            or not next_cursor
            or next_cursor == cursor
            or len(next_cursor) > 4096
        ):
            raise StorageError("s3_invalid_cursor")
        entries = []
        for obj in response.get("Contents", []):
            key = decode_key(obj.get("Key"))
            if not key.startswith(prefix):
                raise StorageError("s3_prefix_mismatch")
            if key.endswith("/"):
                continue
            observed = observation(
                {**obj, "ContentLength": obj.get("Size")}, self.config["bucket"], key
            )
            observed.pop("version_id")  # LIST does not identify a version.
            entries.append({"name": key[len(prefix) :], **observed})
        names = []
        for obj in response.get("CommonPrefixes", []):
            key = decode_key(obj.get("Prefix"))
            if not key.startswith(prefix) or not key.endswith("/"):
                raise StorageError("s3_prefix_mismatch")
            names.append(key[len(prefix) :])
        if len(entries) + len(names) > PAGE_SIZE:
            raise StorageError("s3_invalid_listing")
        return {
            "entries": entries,
            "directories": names,
            "next_cursor": next_cursor,
            "listed_count": len(response.get("Contents", [])) + len(names),
        }

    @contextmanager
    def content_file(self, base, path, *, expected=None, **kwargs):
        if (
            kwargs.get("expected_anchor") is not None
            and kwargs["expected_anchor"] != self.anchor
        ):
            raise StorageError("configuration_changed")
        key = directory_prefix(self.config.get("base_prefix", ""), base) + path
        if "\x00" in key or len(key.encode()) > 1024:
            raise StorageError("s3_invalid_key")
        bucket = self.config["bucket"]
        if not path or (
            expected
            and (expected.get("key") != key or expected.get("bucket") != bucket)
        ):
            raise StorageError("content_binding_changed")
        params = {"Bucket": bucket, "Key": key}
        if expected and expected.get("version_id") not in {None, "null"}:
            params["VersionId"] = expected["version_id"]
        if expected and expected.get("etag"):
            params["IfMatch"] = expected["etag"]
        with self.client() as client:
            before = read_observation(client.head_object(**params), bucket, key)
            if expected and not matches(before, expected):
                raise StorageError("source_changed")
            params["IfMatch"] = before["etag"]
            if before["version_id"] not in {None, "null"}:
                params["VersionId"] = before["version_id"]
            require_conditions(client, params)
            response = client.get_object(**params)
            body = response["Body"]
            try:
                if read_observation(response, bucket, key) != before:
                    raise StorageError("source_changed")
                yield body, before
                if (
                    read_observation(client.head_object(**params), bucket, key)
                    != before
                ):
                    raise StorageError("source_changed")
            finally:
                close_registered(body)

    def probe(self, sample_file=None):
        capabilities = {
            name: "unsupported" for name in ("range", "write", "delete", "presign")
        }
        capabilities.update(list="unknown", read="unknown", conditional_read="unknown")
        try:
            self.list_page("")
            capabilities["list"] = "verified"
            if sample_file is not None:
                with self.content_file("", sample_file) as (body, _):
                    body.read(16)
                capabilities.update(read="verified", conditional_read="verified")
            return {
                "ok": True,
                "code": "ok",
                "capabilities": capabilities,
                "anchor": self.anchor,
            }
        except (StorageError, connector_guard.ConnectorHostDenied) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            return {
                "ok": False,
                "code": getattr(exc, "code", "connector_host_denied"),
                "capabilities": capabilities,
                "anchor": self.anchor,
            }
