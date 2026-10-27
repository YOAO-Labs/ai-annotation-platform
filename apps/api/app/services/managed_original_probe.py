"""Observe exact retired-original bytes without granting physical permission."""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import re
import socket
import sys
import time
from typing import Literal
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from app.config import settings
from app.services.dataset_assets import MAX_DIRECT_UPLOAD_BYTES
from app.services.filesystem_storage import FilesystemStorageError as StorageError
from app.services.managed_original_retirement import managed_storage_identity
from app.services.reference_keys import is_reference
from app.services.sources import s3_reference
from app.services.storage_access import close_registered, mark_unsettled

MAX_BYTES = MAX_DIRECT_UPLOAD_BYTES
CHUNK_BYTES = 256 * 1024
TIMEOUT_SECONDS = 60

Outcome = Literal["matched", "missing", "changed", "unverifiable", "failed"]


@dataclass(frozen=True, repr=False)
class ProbeInput:
    bucket: str
    object_key: str
    storage_identity: str
    sha256: str
    size_bytes: int
    endpoint_url: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    outcome: Outcome
    error_code: str | None = None
    observed_sha256: str | None = None
    observed_size: int | None = None
    observation: dict = field(default_factory=dict)


class _ProbeFailure(Exception):
    def __init__(self, outcome: Outcome, code: str):
        self.outcome, self.code = outcome, code
        super().__init__(code)


def _reject(outcome: Outcome, code: str):
    raise _ProbeFailure(outcome, code)


def _validate(candidate):
    if (
        not isinstance(candidate, ProbeInput)
        or not isinstance(candidate.bucket, str)
        or not candidate.bucket
        or len(candidate.bucket) > 255
        or any(c in candidate.bucket for c in ("\x00", "\r", "\n"))
        or not isinstance(candidate.object_key, str)
        or not candidate.object_key
        or "\x00" in candidate.object_key
        or len(candidate.object_key.encode("utf-8")) > 1024
        or is_reference(candidate.object_key)
        or not isinstance(candidate.storage_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate.storage_identity) is None
        or not isinstance(candidate.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate.sha256) is None
        or type(candidate.size_bytes) is not int
        or candidate.size_bytes < 0
    ):
        _reject("unverifiable", "managed_original_invalid_input")
    if candidate.size_bytes > MAX_BYTES:
        _reject("unverifiable", "managed_original_byte_budget_exceeded")


def _endpoint(value=None):
    if value is None:
        value = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        _reject("unverifiable", "managed_original_storage_configuration_changed")
    return value.rstrip("/")


def _client(endpoint):
    try:
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            config=Config(
                connect_timeout=2,
                read_timeout=2,
                retries={"total_max_attempts": 1, "mode": "standard"},
                s3={"addressing_style": "path"},
                proxies={},
                signature_version="s3v4",
            ),
        )
    except BaseException as exc:
        # A failed constructor is not evidence that no physical handle escaped.
        raise mark_unsettled(exc)


def _guard(endpoint):
    target = urlsplit(endpoint)

    def before_send(request, **kwargs):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (target.scheme, target.netloc):
            raise StorageError("s3_redirect_denied")

    return before_send


def _observation(response, candidate):
    try:
        observed = s3_reference.read_observation(
            response, candidate.bucket, candidate.object_key
        )
        modified = response.get("LastModified")
        version = observed["version_id"]
        if (
            type(observed["size"]) is not int
            or not isinstance(modified, datetime)
            or modified.utcoffset() is None
            or not observed["etag"].strip()
            or len(observed["etag"]) > 1024
            or any(c in observed["etag"] for c in ("\r", "\n"))
            or (version is not None and (not isinstance(version, str) or not version))
        ):
            raise StorageError("s3_invalid_observation")
        return observed
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        raise StorageError("s3_invalid_response") from None


def _classify(exc):
    if isinstance(exc, _ProbeFailure):
        return exc.outcome, exc.code
    if isinstance(exc, ClientError):
        response = exc.response if isinstance(exc.response, dict) else {}
        error, metadata = response.get("Error"), response.get("ResponseMetadata")
        code = error.get("Code") if isinstance(error, dict) else None
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        if code in {"AccessDenied", "403"} or status in {401, 403}:
            return "failed", "managed_original_access_denied"
        if code in {"NoSuchKey", "NoSuchVersion", "404", "NotFound"} and status in {
            None,
            404,
        }:
            return "missing", "managed_original_object_missing"
        if code in {"PreconditionFailed", "412"}:
            return "changed", "managed_original_object_changed"
        return "failed", "managed_original_source_unavailable"
    if isinstance(
        exc, (TimeoutError, socket.timeout, ConnectTimeoutError, ReadTimeoutError)
    ):
        return "failed", "managed_original_probe_timeout"
    if isinstance(exc, StorageError):
        if exc.code == "path_unavailable":
            return "missing", "managed_original_object_missing"
        if exc.code == "source_changed":
            return "changed", "managed_original_object_changed"
        if exc.code == "permission_denied":
            return "failed", "managed_original_access_denied"
        if exc.code == "s3_conditions_not_supported":
            return "unverifiable", "managed_original_conditions_unsupported"
        if exc.code == "s3_redirect_denied":
            return "unverifiable", "managed_original_redirect_denied"
        return "unverifiable", "managed_original_invalid_response"
    if isinstance(exc, (BotoCoreError, OSError)):
        return "failed", "managed_original_source_unavailable"
    if isinstance(exc, (AttributeError, KeyError, TypeError, ValueError)):
        return "unverifiable", "managed_original_invalid_response"
    return "failed", "managed_original_probe_failed"


def run_probe(candidate: ProbeInput, *, client_context=None) -> ProbeResult:
    """Read one bounded observation; even a match is only historical evidence."""
    observation, observed_sha256, observed_size = {}, None, None
    try:
        _validate(candidate)
        endpoint = _endpoint(candidate.endpoint_url)
        if (
            managed_storage_identity(endpoint, candidate.bucket)
            != candidate.storage_identity
        ):
            _reject("unverifiable", "managed_original_storage_configuration_changed")
        deadline = time.monotonic() + TIMEOUT_SECONDS

        def check_deadline():
            if time.monotonic() >= deadline:
                _reject("failed", "managed_original_probe_timeout")

        context = client_context() if client_context is not None else None
        client = context.__enter__() if context is not None else _client(endpoint)
        try:
            if client is None:
                raise mark_unsettled(StorageError("connection_unavailable"))
            if client.meta.endpoint_url.rstrip("/") != endpoint:
                _reject(
                    "unverifiable", "managed_original_storage_configuration_changed"
                )
            client.meta.events.register("before-send.s3", _guard(endpoint))
            params = {"Bucket": candidate.bucket, "Key": candidate.object_key}
            check_deadline()
            before = _observation(client.head_object(**params), candidate)
            check_deadline()
            observation["initial"] = before
            if before["size"] != candidate.size_bytes:
                _reject("changed", "managed_original_size_mismatch")
            conditions = {**params, "IfMatch": before["etag"]}
            if before["version_id"] not in {None, "null"}:
                conditions["VersionId"] = before["version_id"]
            s3_reference.require_conditions(client, conditions)
            check_deadline()
            response = client.get_object(**conditions)
            body = response.get("Body")
            try:
                check_deadline()
                current = _observation(response, candidate)
                observation["read"] = current
                if current != before:
                    _reject("changed", "managed_original_object_changed")
                if body is None or not callable(getattr(body, "read", None)):
                    raise StorageError("s3_invalid_response")
                digest, size = hashlib.sha256(), 0
                while True:
                    check_deadline()
                    chunk = body.read(CHUNK_BYTES)
                    check_deadline()
                    if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
                        raise StorageError("s3_invalid_response")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES:
                        _reject("failed", "managed_original_byte_budget_exceeded")
                    if size > candidate.size_bytes:
                        _reject("changed", "managed_original_size_mismatch")
                    digest.update(chunk)
                observed_sha256, observed_size = digest.hexdigest(), size
                if size != candidate.size_bytes:
                    _reject("changed", "managed_original_size_mismatch")
                # This HEAD intentionally has no VersionId or IfMatch. Re-reading
                # an old pinned version cannot prove that it is still current.
                after = _observation(client.head_object(**params), candidate)
                check_deadline()
                observation["final"] = after
                if after != before:
                    _reject("changed", "managed_original_object_changed")
                if observed_sha256 != candidate.sha256:
                    _reject("changed", "managed_original_sha256_mismatch")
            finally:
                if body is not None:
                    close_registered(body)
        finally:
            if context is not None:
                context.__exit__(*sys.exc_info())
            else:
                close_registered(client)
        check_deadline()
        return ProbeResult("matched", None, observed_sha256, observed_size, observation)
    except BaseException as exc:
        if getattr(exc, "storage_operation_unsettled", False) or not isinstance(
            exc, Exception
        ):
            raise
        outcome, code = _classify(exc)
        return ProbeResult(outcome, code, observed_sha256, observed_size, observation)
