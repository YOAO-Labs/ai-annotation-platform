"""Saved export-media owners and synchronous, bounded physical operations.

The caller owns binding allocation, durable admission, draining and publication.
No client is constructed until ``open_storage`` is entered under that permit.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import stat
import time
from urllib.parse import urlsplit
import uuid

from app.config import settings
from app.services.storage import storage_service
from app.services.storage_access import (
    close_registered,
    closing_registered,
    mark_unsettled,
)

LEGACY_LOCATION_ID = uuid.uuid5(uuid.NAMESPACE_URL, "aap:platform-storage:legacy")
MAX_OBJECT_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 24 * 1024**3
MAX_OBJECTS = 21_000
RETENTION_SECONDS = 7 * 24 * 3600
VERIFY_SECONDS = 3600
CHUNK_BYTES = 1024 * 1024
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_PREFIX = re.compile(r"lidar-export-media/objects/" + _UUID + "/")
_RELATIVE = re.compile(_UUID + r"\.bin")
_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "location_id",
        "config_revision",
        "binding_generation",
        "endpoint_url",
        "bucket",
        "key_prefix",
        "storage_identity",
    }
)


class ExportMediaStorageError(ValueError):
    def __init__(self, code="identity_changed", status_code=None):
        suffix = code.removeprefix("export_media_storage_")
        super().__init__("导出媒体副本的存储归属或完整性核验失败")
        self.code = "export_media_storage_" + suffix
        self.status_code = status_code or {"missing": 404, "unavailable": 503}.get(
            suffix, 409
        )


def _reject(code="identity_changed"):
    raise ExportMediaStorageError(code)


def _plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and all(32 < ord(char) < 127 for char in value)
    )


def _uuid(value):
    if not isinstance(value, (str, uuid.UUID)):
        _reject("invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("invalid_key")
    if result != str(value):
        _reject("invalid_key")
    return result


def prefix_for(identifier):
    return f"lidar-export-media/objects/{_uuid(identifier)}/"


def _endpoint(value):
    if not _plain(value, 2048) or value.endswith("/") or "\\" in value:
        _reject()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            _reject()
    except ValueError:
        _reject()
    return value


def _runtime_endpoint():
    return _endpoint(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


def storage_identity(endpoint, bucket):
    # Exact managed_storage_identity recipe, without its global client import.
    return hashlib.sha256(
        json.dumps([endpoint.rstrip("/"), bucket]).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Owner:
    bucket: str
    prefix: str
    _ref: dict = field(repr=False)

    @property
    def ref(self):
        return deepcopy(self._ref)

    def key(self, relative):
        if not isinstance(relative, str) or _RELATIVE.fullmatch(relative) is None:
            _reject("invalid_key")
        return self.prefix + relative

    def contains(self, key):
        if not isinstance(key, str) or not key.startswith(self.prefix):
            _reject("invalid_key")
        self.key(key[len(self.prefix) :])
        return True


def resolve_ref(ref, *, expected_prefix=None):
    if ref is None:
        _reject("unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "export"
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] != 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] != 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@%:")
        or not isinstance(ref["key_prefix"], str)
        or _PREFIX.fullmatch(ref["key_prefix"]) is None
        or (expected_prefix is not None and ref["key_prefix"] != expected_prefix)
        or not isinstance(ref["storage_identity"], str)
        or _SHA.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref["storage_identity"] != storage_identity(
        endpoint, ref["bucket"]
    ):
        _reject()
    return Owner(ref["bucket"], ref["key_prefix"], deepcopy(ref))


def make_ref(identifier, *, bucket, endpoint_url=None):
    """Build metadata only; the caller has already validated the export binding."""
    endpoint = _endpoint(_runtime_endpoint() if endpoint_url is None else endpoint_url)
    if not _plain(bucket, 255) or any(char in bucket for char in "/\\?#@%:"):
        _reject()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "export",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": bucket,
            "key_prefix": prefix_for(identifier),
            "storage_identity": storage_identity(endpoint, bucket),
        },
        expected_prefix=prefix_for(identifier),
    ).ref


def _field(record, name):
    return record.get(name) if isinstance(record, dict) else getattr(record, name, None)


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _reject("invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record):
    created, expires = (
        _instant(_field(record, name)) for name in ("created_at", "expires_at")
    )
    if not created < expires <= created + timedelta(seconds=RETENTION_SECONDS):
        _reject("invalid_receipt")
    return resolve_ref(
        _field(record, "storage_ref"), expected_prefix=prefix_for(_field(record, "id"))
    )


def _live(record, now=None):
    if _instant(_field(record, "expires_at")) <= _instant(
        now or datetime.now(timezone.utc)
    ):
        _reject("expired")


def _receipt(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"sha256", "size_bytes"}
        or not isinstance(value["sha256"], str)
        or _SHA.fullmatch(value["sha256"]) is None
        or type(value["size_bytes"]) is not int
        or not 0 <= value["size_bytes"] <= MAX_OBJECT_BYTES
    ):
        _reject("invalid_receipt")
    return dict(value)


def _inventory(record, inventory=None):
    value = _field(record, "inventory") if inventory is None else inventory
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "objects"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["objects"], dict)
        or len(value["objects"]) > MAX_OBJECTS
    ):
        _reject("invalid_receipt")
    return value["objects"]


def validate_inventory(record, inventory=None):
    """One linear metadata pass; single-object operations use constant-time lookup."""
    owner = resolve(record)
    result, total = {}, 0
    for key, value in _inventory(record, inventory).items():
        owner.contains(key)
        result[key] = _receipt(value)
        total += result[key]["size_bytes"]
        if total > MAX_TOTAL_BYTES:
            _reject("budget")
    return result


def _expected(record, owner, relative):
    key = owner.key(relative)
    value = _inventory(record).get(key)
    if value is None:
        _reject("invalid_key")
    return key, _receipt(value)


def _translate(exc):
    if isinstance(exc, ExportMediaStorageError) and not getattr(
        exc, "storage_operation_unsettled", False
    ):
        return exc
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    suffix = (
        "missing"
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}
        else "integrity_mismatch"
        if code in {"412", "PreconditionFailed", "409", "ConditionalRequestConflict"}
        else "unavailable"
    )
    result = ExportMediaStorageError(suffix)
    if getattr(exc, "storage_operation_unsettled", False):
        mark_unsettled(result)
    return result


def create_client():
    """Called only inside the caller's durable permit, never at module import."""
    import boto3
    from botocore.config import Config

    endpoint = _runtime_endpoint()
    target = urlsplit(endpoint)

    def same_origin(request, **_kwargs):
        destination = urlsplit(request.url)
        if (destination.scheme, destination.netloc) != (target.scheme, target.netloc):
            _reject("redirect_denied")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 0},
            s3={"addressing_style": "path"},
        ),
    )
    try:
        client.meta.events.register("before-send.s3", same_origin)
    except BaseException:
        close_registered(client)
        raise
    return client


def _client(client, owner):
    endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
    if (
        not isinstance(endpoint, str)
        or endpoint.rstrip("/") != owner.ref["endpoint_url"]
    ):
        _reject()
    return client


@contextmanager
def open_storage(record, factory=None):
    """The caller admits/drains constructor, every operation and actual close."""
    owner = resolve(record)
    try:
        client = (factory or create_client)()
    except BaseException as exc:
        mark_unsettled(exc)
        if isinstance(exc, Exception):
            raise _translate(exc) from None
        raise
    try:
        yield _client(client, owner)
    finally:
        try:
            close_registered(client)
        except Exception as exc:
            raise _translate(exc) from None


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("budget")


def _http_ok(response, *, statuses=(200,)):
    metadata = response.get("ResponseMetadata")
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("HTTPStatusCode")) is not int
        or metadata["HTTPStatusCode"] not in statuses
        or response.get("ContentRange") is not None
    ):
        _reject("integrity_mismatch")


def _version(response):
    value = response.get("VersionId")
    if value is None or value == "null":
        return None
    if not _plain(value, 1024):
        _reject("integrity_mismatch")
    return value


def _read(client, owner, key, expected, deadline, *, written=None):
    _budget(deadline)
    params = {"Bucket": owner.bucket, "Key": key}
    head = client.head_object(**params)
    _http_ok(head)
    etag, version = head.get("ETag"), _version(head)
    if (
        not _plain(etag, 1024)
        or type(head.get("ContentLength")) is not int
        or head["ContentLength"] != expected["size_bytes"]
        or (
            written is not None
            and (written.get("ETag") != etag or _version(written) != version)
        )
    ):
        _reject("integrity_mismatch")
    condition = {"IfMatch": etag}
    if version is not None:
        condition["VersionId"] = version
    response = client.get_object(**params, **condition)
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        raise mark_unsettled(ExportMediaStorageError("unavailable"))
    with closing_registered(body) as body:
        _http_ok(response)
        if (
            response.get("ETag") != etag
            or _version(response) != version
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != expected["size_bytes"]
        ):
            _reject("integrity_mismatch")
        digest, size = hashlib.sha256(), 0
        while True:
            _budget(deadline)
            chunk = body.read(min(CHUNK_BYTES, expected["size_bytes"] - size + 1))
            if type(chunk) is not bytes:
                _reject("integrity_mismatch")
            if not chunk:
                break
            size += len(chunk)
            if size > expected["size_bytes"]:
                _reject("integrity_mismatch")
            digest.update(chunk)
        if size != expected["size_bytes"] or digest.hexdigest() != expected["sha256"]:
            _reject("integrity_mismatch")
    _budget(deadline)
    after = client.head_object(**params)
    _http_ok(after)
    if (
        after.get("ETag") != etag
        or _version(after) != version
        or type(after.get("ContentLength")) is not int
        or after["ContentLength"] != expected["size_bytes"]
    ):
        _reject("integrity_mismatch")
    _budget(deadline)
    return dict(expected)


def verify_object(client, record, relative):
    owner = resolve(record)
    _client(client, owner)
    key, expected = _expected(record, owner, relative)
    _live(record)
    try:
        result = _read(client, owner, key, expected, time.monotonic() + VERIFY_SECONDS)
        _live(record)
        return result
    except Exception as exc:
        raise _translate(exc) from None


@contextmanager
def _borrowed_fd(descriptor, expected):
    if type(descriptor) is not int or descriptor < 0:
        _reject("invalid_receipt")
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected["size_bytes"]:
        _reject("invalid_receipt")
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    duplicate = os.dup(descriptor)
    try:
        try:
            stream = os.fdopen(duplicate, "rb")
        except BaseException:
            try:
                os.close(duplicate)
            except BaseException as closing:
                raise mark_unsettled(closing)
            raise
        with closing_registered(stream):
            stream.seek(0)
            yield stream
    finally:
        try:
            os.lseek(descriptor, position, os.SEEK_SET)
        except BaseException as exc:
            raise mark_unsettled(exc)


def write_object(client, record, relative, source_fd):
    """Borrow an int FD serially, restore its cursor, and close only our duplicate."""
    owner = resolve(record)
    _client(client, owner)
    key, expected = _expected(record, owner, relative)
    _live(record)
    deadline = time.monotonic() + VERIFY_SECONDS
    try:
        with _borrowed_fd(source_fd, expected) as stream:
            digest, size = hashlib.sha256(), 0
            while True:
                _budget(deadline)
                chunk = stream.read(min(CHUNK_BYTES, expected["size_bytes"] - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > expected["size_bytes"]:
                    _reject("integrity_mismatch")
                digest.update(chunk)
            if (
                size != expected["size_bytes"]
                or digest.hexdigest() != expected["sha256"]
            ):
                _reject("integrity_mismatch")
            stream.seek(0)
            _live(record)
            written = client.put_object(
                Bucket=owner.bucket,
                Key=key,
                Body=stream,
                ContentLength=expected["size_bytes"],
                ContentType="application/octet-stream",
                CacheControl="private, max-age=604800, immutable",
                IfNoneMatch="*",
            )
            _http_ok(written)
            result = _read(client, owner, key, expected, deadline, written=written)
        _live(record)
        return result
    except Exception as exc:
        raise _translate(exc) from None


def _objects(client, owner, expected, deadline):
    result, token, seen, total = {}, None, set(), 0
    while True:
        _budget(deadline)
        params = {"Bucket": owner.bucket, "Prefix": owner.prefix, "MaxKeys": 1000}
        if token is not None:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        _http_ok(response)
        rows = response.get("Contents", [])
        if not isinstance(rows, list) or type(response.get("IsTruncated")) is not bool:
            _reject("integrity_mismatch")
        for row in rows:
            if not isinstance(row, dict):
                _reject("integrity_mismatch")
            key, size = row.get("Key"), row.get("Size")
            owner.contains(key)
            if (
                key not in expected
                or key in result
                or type(size) is not int
                or size < 0
            ):
                _reject("integrity_mismatch")
            result[key] = size
            total += size
            if (
                size > MAX_OBJECT_BYTES
                or total > MAX_TOTAL_BYTES
                or len(result) > MAX_OBJECTS
            ):
                _reject("budget")
        if not response["IsTruncated"]:
            return result
        next_token = response.get("NextContinuationToken")
        if not _plain(next_token, 8192) or next_token in seen:
            _reject("integrity_mismatch")
        seen.add(next_token)
        token = next_token


def list_objects(client, record):
    owner = resolve(record)
    _client(client, owner)
    expected = validate_inventory(record)
    try:
        return _objects(client, owner, expected, time.monotonic() + VERIFY_SECONDS)
    except Exception as exc:
        raise _translate(exc) from None


def verify_inventory(client, record):
    owner = resolve(record)
    _client(client, owner)
    objects = validate_inventory(record)
    sizes = {key: entry["size_bytes"] for key, entry in objects.items()}
    deadline = time.monotonic() + VERIFY_SECONDS
    _live(record)
    try:
        if _objects(client, owner, objects, deadline) != sizes:
            _reject("integrity_mismatch")
        for key, expected in objects.items():
            _read(client, owner, key, expected, deadline)
        if _objects(client, owner, objects, deadline) != sizes:
            _reject("integrity_mismatch")
        _live(record)
        return {"schema": 1, "objects": objects}
    except Exception as exc:
        raise _translate(exc) from None


def signed_url(client, record, relative, *, now=None):
    owner = resolve(record)
    _client(client, owner)
    key, _expected_receipt = _expected(record, owner, relative)
    current = _instant(now or datetime.now(timezone.utc))
    seconds = int((_instant(_field(record, "expires_at")) - current).total_seconds())
    if seconds <= 0:
        _reject("expired")
    try:
        raw = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": owner.bucket, "Key": key},
            ExpiresIn=min(RETENTION_SECONDS, seconds),
        )
        return storage_service._public_url(raw)
    except Exception as exc:
        raise _translate(exc) from None


def _versions(client, owner, expected, deadline):
    result, markers, seen = [], {}, set()
    while True:
        _budget(deadline)
        response = client.list_object_versions(
            Bucket=owner.bucket, Prefix=owner.prefix, MaxKeys=1000, **markers
        )
        _http_ok(response)
        if type(response.get("IsTruncated")) is not bool:
            _reject("integrity_mismatch")
        for field_name in ("Versions", "DeleteMarkers"):
            rows = response.get(field_name, [])
            if not isinstance(rows, list):
                _reject("integrity_mismatch")
            for row in rows:
                if not isinstance(row, dict):
                    _reject("integrity_mismatch")
                key, version = row.get("Key"), row.get("VersionId")
                owner.contains(key)
                if (
                    key not in expected
                    or not _plain(version, 1024)
                    or (key, version) in seen
                ):
                    _reject("integrity_mismatch")
                seen.add((key, version))
                result.append({"Key": key, "VersionId": version})
                if len(result) > MAX_OBJECTS * 2:
                    _reject("budget")
        if not response["IsTruncated"]:
            return result
        key_marker, version_marker = (
            response.get("NextKeyMarker"),
            response.get("NextVersionIdMarker"),
        )
        if not _plain(key_marker, 1024) or (
            version_marker not in (None, "") and not _plain(version_marker, 1024)
        ):
            _reject("integrity_mismatch")
        owner.contains(key_marker)
        next_markers = {"KeyMarker": key_marker}
        if version_marker not in (None, ""):
            next_markers["VersionIdMarker"] = version_marker
        if next_markers == markers:
            _reject("integrity_mismatch")
        # A marker may revisit an older page; a separate history catches cycles.
        marker_key = ("page", key_marker, version_marker)
        if marker_key in seen:
            _reject("integrity_mismatch")
        seen.add(marker_key)
        markers = next_markers


def delete_inventory(client, record):
    """Retired-owner only: permit missing known entries, never delete unknown keys."""
    owner = resolve(record)
    _client(client, owner)
    expected = validate_inventory(record)
    deadline = time.monotonic() + VERIFY_SECONDS
    try:
        visible = _objects(client, owner, expected, deadline)
        versions = _versions(client, owner, expected, deadline)
        version_keys = {row["Key"] for row in versions}
        if not set(visible) <= version_keys:
            _reject("integrity_mismatch")
        for row in versions:
            _budget(deadline)
            response = client.delete_object(Bucket=owner.bucket, **row)
            _http_ok(response, statuses=(200, 204))
        if _objects(client, owner, expected, deadline) or _versions(
            client, owner, expected, deadline
        ):
            _reject("integrity_mismatch")
        return len(versions)
    except Exception as exc:
        raise _translate(exc) from None
