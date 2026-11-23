"""Storage client for built-in and explicitly scoped local S3 operations."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
from urllib.parse import urlsplit

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


def directory_prefix(base, relative=""):
    for value in (base, relative):
        if not isinstance(value, str) or "\x00" in value:
            raise StorageError("invalid_s3_prefix")
    return (base + ("/" if base and not base.endswith("/") else "")) + (
        relative + ("/" if relative and not relative.endswith("/") else "")
    )


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


def configured_endpoint(config):
    endpoint = config["endpoint"]
    if "://" not in endpoint:
        endpoint = ("https://" if config.get("use_ssl") else "http://") + endpoint
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


class _S3ClientAccess:
    config: dict
    allowlist: tuple[str, ...]

    @property
    def endpoint(self):
        return configured_endpoint(self.config)

    def _check_destination(self, endpoint):
        allowed_endpoint(endpoint, self.allowlist)

    def _checked_endpoint(self):
        endpoint = self.endpoint
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.config["bucket"]):
            raise StorageError("s3_bucket_not_supported")
        self._check_destination(endpoint)
        return endpoint

    @contextmanager
    def _client(self, endpoint, secret, *, read_timeout=None):
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=secret["access_key"],
                aws_secret_access_key=secret["secret_key"],
                region_name=self.config.get("region"),
                config=Config(
                    connect_timeout=2,
                    read_timeout=2 if read_timeout is None else read_timeout,
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
            self._check_destination(endpoint)

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


@dataclass(frozen=True, repr=False)
class S3Access(_S3ClientAccess):
    config: dict
    secret_enc: bytes
    allowlist: tuple[str, ...]

    @contextmanager
    def client(self, *, read_timeout=None):
        endpoint = self._checked_endpoint()
        try:
            secret = decrypt_secret(self.secret_enc)
        except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
            raise StorageError("connection_unavailable") from None
        with self._client(endpoint, secret, read_timeout=read_timeout) as client:
            yield client


@dataclass(frozen=True, repr=False)
class ScopedS3Access(_S3ClientAccess):
    """Use credentials already admitted by the physical owner, without a platform key."""

    config: dict
    secret: dict
    allowlist: tuple[str, ...]

    def _check_destination(self, endpoint):
        if endpoint != self.secret.get("endpoint"):
            raise StorageError("connector_host_denied")
        try:
            address = ipaddress.ip_address(urlsplit(endpoint).hostname)
        except ValueError:
            allowed_endpoint(endpoint, self.allowlist)
        else:
            if not address.is_loopback:
                allowed_endpoint(endpoint, (str(address),))

    def client(self, *, read_timeout=None):
        return self._client(
            self._checked_endpoint(), self.secret, read_timeout=read_timeout
        )
