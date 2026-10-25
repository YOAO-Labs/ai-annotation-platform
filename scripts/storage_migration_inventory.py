"""Bounded, read-only S3 source observations; never a migration/cutover permit."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit


PURPOSES = frozenset(
    {
        "annotations",
        "datasets",
        "bug-reports",
        "media-cache",
        "audit-archive",
        "import",
        "export",
        "avatars",
        "training",
    }
)
SCHEMA = "aap.storage-migration-inventory/v1"
_MAX_RECORD = 256 * 1024
_SECRET = re.compile(
    r"^(password|secret|token|authorization|credentials|access[_-]?key(?:[_-]?id)?|"
    r"(?:secret|private|api)[_-]?key|secret[_-]?access[_-]?key|session[_-]?token|"
    r"(?:aws|s3)[_-].*(key|token))$",
    re.I,
)


class InventoryError(ValueError):
    """A credential-free machine code; never includes raw SDK exception text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InventoryLimits:
    max_pages: int = 100000
    max_entries: int = 1000000
    max_object_bytes: int = 1024**3
    max_total_bytes: int = 1024**4
    read_timeout_seconds: float = 300
    page_size: int = 1000
    chunk_bytes: int = 1024**2

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "read_timeout_seconds":
                valid = (
                    type(value) in (int, float) and math.isfinite(value) and value > 0
                )
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise InventoryError("invalid_limits")
        if self.page_size > 1000 or self.chunk_bytes > 8 * 1024**2:
            raise InventoryError("invalid_limits")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    try:
        data = (
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InventoryError("invalid_metadata") from None
    if len(data) > _MAX_RECORD:
        raise InventoryError("metadata_limit")
    return data


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise InventoryError("invalid_endpoint")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InventoryError("invalid_endpoint")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        raise InventoryError("invalid_endpoint") from None
    return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))


def _safe(value, depth=0):
    if depth > 8:
        raise InventoryError("metadata_limit")
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InventoryError("invalid_metadata")
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 8192:
            raise InventoryError("metadata_limit")
        if value.lower().startswith(("http://", "https://")):
            return _endpoint(value)
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise InventoryError("metadata_limit")
        return [_safe(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 1000 or any(not isinstance(key, str) for key in value):
            raise InventoryError("invalid_metadata")
        return {
            key: "[redacted]" if _SECRET.match(key) else _safe(item, depth + 1)
            for key, item in value.items()
            if key != "ResponseMetadata"
        }
    raise InventoryError("invalid_metadata")


def _string(value, code, maximum=1024):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise InventoryError(code)
    return value


def _integer(value, code):
    if type(value) is not int or value < 0:
        raise InventoryError(code)
    return value


def _error_code(exc):
    code = getattr(exc, "code", None)
    if isinstance(exc, InventoryError):
        return exc.code
    response = getattr(exc, "response", {})
    if isinstance(response, dict):
        error = response.get("Error", {})
        if isinstance(error, dict):
            code = error.get("Code", code)
    return (
        code
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
        else "read_failed"
    )


class _Writer:
    def __init__(self, output):
        self.stream = os.fdopen(
            os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        )
        self.digest = hashlib.sha256()
        self.count = 0
        self.bytes = 0

    def emit(self, record):
        data = _canonical(record)
        self.stream.write(data)
        self.digest.update(data)
        self.count += 1
        self.bytes += len(data)

    def finish(self, summary):
        tail = {
            "type": "tail",
            "schema": SCHEMA,
            "prior_records": self.count,
            "prior_bytes": self.bytes,
            "sha256": self.digest.hexdigest(),
            **summary,
        }
        self.stream.write(_canonical(tail))
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return tail


class _Inventory:
    def __init__(self, client, writer, limits):
        self.client, self.writer, self.limits = client, writer, limits
        self.counts = dict(
            buckets=0,
            objects=0,
            delete_markers=0,
            multipart=0,
            verified_objects=0,
            verified_bytes=0,
            bytes_read=0,
            pages=0,
            issues=0,
            entries=0,
        )
        self.stopped = False

    def issue(self, operation, bucket, exc):
        self.counts["issues"] += 1
        self.writer.emit(
            {
                "type": "issue",
                "operation": operation,
                "bucket": bucket,
                "code": _error_code(exc),
            }
        )
        if _error_code(exc) in ("page_budget", "entry_budget", "total_byte_budget"):
            self.stopped = True

    def call(self, operation, **kwargs):
        response = getattr(self.client, operation)(**kwargs)
        if not isinstance(response, dict):
            raise InventoryError("invalid_response")
        if operation != "get_object" and "ResponseMetadata" in response:
            metadata = response["ResponseMetadata"]
            if not isinstance(metadata, dict) or metadata.get("HTTPStatusCode") != 200:
                raise InventoryError("unexpected_status")
        return response

    def config(self, bucket):
        result = {}
        for operation, field in (
            ("get_bucket_versioning", "versioning"),
            ("get_bucket_lifecycle_configuration", "lifecycle"),
        ):
            try:
                response = self.call(operation, Bucket=bucket)
                if field == "versioning":
                    status = response.get("Status", "Disabled")
                    if status not in ("Disabled", "Enabled", "Suspended"):
                        raise InventoryError("invalid_versioning")
                    result[field] = {
                        "state": "known",
                        "status": status,
                        "mfa_delete": _safe(response.get("MFADelete")),
                    }
                else:
                    rules = response.get("Rules")
                    if not isinstance(rules, list):
                        raise InventoryError("invalid_lifecycle")
                    result[field] = {"state": "known", "rules": _safe(rules)}
            except Exception as exc:
                if field == "lifecycle" and _error_code(exc) in (
                    "NoSuchLifecycleConfiguration",
                    "NoSuchLifecycle",
                ):
                    result[field] = {"state": "known", "rules": []}
                else:
                    result[field] = {"state": "unknown", "code": _error_code(exc)}
                    self.issue(operation, bucket, exc)
        return result

    def pages(self, operation, bucket, fields, input_tokens, output_tokens):
        tokens, seen = {}, set()
        while not self.stopped:
            if self.counts["pages"] >= self.limits.max_pages:
                raise InventoryError("page_budget")
            args = {"Bucket": bucket, **tokens}
            args[
                "MaxUploads" if operation == "list_multipart_uploads" else "MaxKeys"
            ] = self.limits.page_size
            response = self.call(operation, **args)
            self.counts["pages"] += 1
            selected = []
            for field in fields:
                values = response.get(field, [])
                if not isinstance(values, list):
                    raise InventoryError("invalid_listing")
                selected.extend((field, item) for item in values)
            if len(selected) > self.limits.page_size:
                raise InventoryError("page_size_exceeded")
            yield selected
            truncated = response.get("IsTruncated")
            if type(truncated) is not bool:
                raise InventoryError("invalid_listing")
            if not truncated:
                return
            values = tuple(response.get(name) for name in output_tokens)
            if not values[0] or any(
                value is not None and not isinstance(value, str) for value in values
            ):
                raise InventoryError("invalid_pagination")
            if values in seen:
                raise InventoryError("pagination_loop")
            seen.add(values)
            tokens = {
                name: value
                for name, value in zip(input_tokens, values)
                if value is not None
            }

    def entry(self):
        if self.counts["entries"] >= self.limits.max_entries:
            raise InventoryError("entry_budget")
        self.counts["entries"] += 1

    def head(self, bucket, key, version, etag, size):
        kwargs = {"Bucket": bucket, "Key": key, "IfMatch": etag}
        if version is not None:
            kwargs["VersionId"] = version
        response = self.call("head_object", **kwargs)
        self.validate_response(response, version, etag, size)
        return self.headers(response)

    @staticmethod
    def headers(response):
        metadata = response.get("Metadata", {})
        if not isinstance(metadata, dict):
            raise InventoryError("invalid_metadata")
        return {
            "etag": response["ETag"],
            "size_bytes": response["ContentLength"],
            "version_id": response.get("VersionId"),
            "content_type": _safe(response.get("ContentType")),
            "metadata": _safe(metadata),
            "metadata_sha256": hashlib.sha256(_canonical(metadata)).hexdigest(),
            "last_modified": _safe(response.get("LastModified")),
        }

    @staticmethod
    def validate_response(response, version, etag, size):
        if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200:
            raise InventoryError("unexpected_status")
        if response.get("DeleteMarker") or "ContentRange" in response:
            raise InventoryError("unexpected_partial_or_deleted")
        if (
            response.get("ContentLength") != size
            or type(response.get("ContentLength")) is not int
        ):
            raise InventoryError("length_changed")
        if response.get("ETag") != etag:
            raise InventoryError("etag_changed")
        actual = response.get("VersionId")
        if (version is not None and actual != version) or (
            version is None and actual not in (None, "null")
        ):
            raise InventoryError("version_changed")

    def verify(self, bucket, key, version, etag, size):
        if size > self.limits.max_object_bytes:
            raise InventoryError("object_byte_budget")
        if size > self.limits.max_total_bytes - self.counts["bytes_read"]:
            raise InventoryError("total_byte_budget")
        started = time.monotonic()

        def deadline():
            remaining = self.limits.read_timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise InventoryError("read_timeout")
            return remaining

        before = self.head(bucket, key, version, etag, size)
        deadline()
        kwargs = {"Bucket": bucket, "Key": key, "IfMatch": etag}
        if version is not None:
            kwargs["VersionId"] = version
        response = self.call("get_object", **kwargs)
        body = response.get("Body")
        digest, observed = hashlib.sha256(), 0
        failure = None
        try:
            if (
                body is None
                or not callable(getattr(body, "read", None))
                or not callable(getattr(body, "close", None))
            ):
                raise InventoryError("invalid_body")
            self.validate_response(response, version, etag, size)
            if self.headers(response) != before:
                raise InventoryError("response_metadata_changed")
            while True:
                remaining = deadline()
                if callable(getattr(body, "set_socket_timeout", None)):
                    body.set_socket_timeout(remaining)
                requested = min(
                    self.limits.chunk_bytes,
                    size - observed + 1,
                    self.limits.max_total_bytes - self.counts["bytes_read"] + 1,
                )
                data = body.read(requested)
                if not isinstance(data, bytes) or len(data) > requested:
                    raise InventoryError("invalid_body")
                self.counts["bytes_read"] += len(data)
                observed += len(data)
                if self.counts["bytes_read"] > self.limits.max_total_bytes:
                    raise InventoryError("total_byte_budget")
                if observed > size:
                    raise InventoryError("body_length_mismatch")
                if not data:
                    break
                digest.update(data)
            deadline()
            if observed != size:
                raise InventoryError("body_length_mismatch")
        except Exception as exc:
            failure = exc
        finally:
            if body is not None and callable(getattr(body, "close", None)):
                try:
                    body.close()
                except Exception:
                    failure = InventoryError("body_close_failed")
        if failure is not None:
            raise failure
        deadline()
        after = self.head(bucket, key, version, etag, size)
        deadline()
        if before != after:
            raise InventoryError("object_changed")
        self.counts["verified_objects"] += 1
        self.counts["verified_bytes"] += observed
        return {
            "status": "verified",
            "sha256": digest.hexdigest(),
            "verified_at": _now(),
            **before,
        }

    def objects(self, bucket, versioning):
        versioned = versioning in ("Enabled", "Suspended")
        operation = "list_object_versions" if versioned else "list_objects_v2"
        fields = ("Versions", "DeleteMarkers") if versioned else ("Contents",)
        inputs = (
            ("KeyMarker", "VersionIdMarker") if versioned else ("ContinuationToken",)
        )
        outputs = (
            ("NextKeyMarker", "NextVersionIdMarker")
            if versioned
            else ("NextContinuationToken",)
        )
        seen = set()
        try:
            for page in self.pages(operation, bucket, fields, inputs, outputs):
                for field, item in page:
                    self.entry()
                    if not isinstance(item, dict):
                        raise InventoryError("invalid_listing")
                    key = _string(item.get("Key"), "invalid_key")
                    version = (
                        _string(item.get("VersionId"), "invalid_version")
                        if versioned
                        else None
                    )
                    identity = (key, version)
                    if identity in seen:
                        raise InventoryError("duplicate_entry")
                    seen.add(identity)
                    record = {
                        "bucket": bucket,
                        "key": key,
                        "version_id": version,
                        "is_latest": item.get("IsLatest") if versioned else True,
                        "last_modified": _safe(item.get("LastModified")),
                        "business_references": "unknown",
                        "target": None,
                        "copy_status": "not_started",
                    }
                    if type(record["is_latest"]) is not bool:
                        raise InventoryError("invalid_listing")
                    if field == "DeleteMarkers":
                        self.counts["delete_markers"] += 1
                        self.writer.emit({"type": "delete_marker", **record})
                        continue
                    self.counts["objects"] += 1
                    record.update(
                        type="object",
                        size_bytes=_integer(item.get("Size"), "invalid_size"),
                        etag=_string(item.get("ETag"), "invalid_etag"),
                    )
                    try:
                        result = self.verify(
                            bucket, key, version, record["etag"], record["size_bytes"]
                        )
                    except Exception as exc:
                        result = {"status": "unverified", "code": _error_code(exc)}
                        self.issue("verify_object", bucket, exc)
                    self.writer.emit({**record, "verification": result})
                    if self.stopped:
                        return
        except Exception as exc:
            self.issue(operation, bucket, exc)

    def multipart(self, bucket):
        seen = set()
        try:
            for page in self.pages(
                "list_multipart_uploads",
                bucket,
                ("Uploads",),
                ("KeyMarker", "UploadIdMarker"),
                ("NextKeyMarker", "NextUploadIdMarker"),
            ):
                for _, item in page:
                    self.entry()
                    if not isinstance(item, dict):
                        raise InventoryError("invalid_listing")
                    key = _string(item.get("Key"), "invalid_key")
                    upload_id = _string(item.get("UploadId"), "invalid_upload_id")
                    if (key, upload_id) in seen:
                        raise InventoryError("duplicate_multipart")
                    seen.add((key, upload_id))
                    self.counts["multipart"] += 1
                    self.writer.emit(
                        {
                            "type": "multipart",
                            "bucket": bucket,
                            "key": key,
                            "upload_id": upload_id,
                            "initiated": _safe(item.get("Initiated")),
                            "state": "unfinished",
                            "copy_status": "not_started",
                        }
                    )
        except Exception as exc:
            self.issue("list_multipart_uploads", bucket, exc)

    def bucket(self, bucket, purposes):
        self.counts["buckets"] += 1
        before = self.config(bucket)
        self.writer.emit(
            {
                "type": "bucket",
                "bucket": bucket,
                "purposes": purposes,
                "observed_at": _now(),
                **before,
            }
        )
        versioning = before["versioning"]
        if versioning["state"] == "known":
            self.objects(bucket, versioning["status"])
        if not self.stopped:
            self.multipart(bucket)
        if not self.stopped:
            after = self.config(bucket)
            if after != before:
                self.issue(
                    "bucket_configuration",
                    bucket,
                    InventoryError("configuration_changed"),
                )


def write_inventory(
    output,
    client,
    purpose_buckets,
    *,
    source=None,
    handling=None,
    observations=None,
    limits=None,
):
    """Exclusively create a private report. SDK/network timeouts remain caller-owned.

    Each stream read has a cooperative deadline (and socket timeout when supported).
    A complete selected-source observation is neither an atomic snapshot nor proof
    that business references, historical locations or all writers are covered.
    """
    limits = limits or InventoryLimits()
    if not isinstance(limits, InventoryLimits):
        raise InventoryError("invalid_limits")
    if not isinstance(purpose_buckets, dict) or set(purpose_buckets) != PURPOSES:
        raise InventoryError("invalid_purpose_map")
    grouped = {}
    for purpose, bucket in sorted(purpose_buckets.items()):
        grouped.setdefault(_string(bucket, "invalid_bucket", 255), []).append(purpose)
    handling = handling or {}
    if (
        not isinstance(handling, dict)
        or not set(handling) <= PURPOSES
        or any(
            value not in ("migrate", "rebuild", "retain") for value in handling.values()
        )
    ):
        raise InventoryError("invalid_handling")
    source = source or {}
    if not isinstance(source, dict) or not set(source) <= {
        "label",
        "endpoint",
        "scope",
    }:
        raise InventoryError("invalid_source")
    source = _safe(source)
    if "endpoint" in source:
        source["endpoint"] = _endpoint(source["endpoint"])
    observations = _safe(observations or {})
    header = {
        "type": "header",
        "schema": SCHEMA,
        "started_at": _now(),
        "source": source,
        "scope": "selected-configured-source",
        "purpose_buckets": purpose_buckets,
        "handling": {
            purpose: handling.get(purpose, "unreviewed") for purpose in sorted(PURPOSES)
        },
        "limits": asdict(limits),
        "observations": observations,
        "migration_ready": False,
        "business_references": "unknown",
        "historical_locations": "unknown",
        "all_writers": "unknown",
        "atomic_snapshot": False,
        "target": None,
        "copy_status": "not_started",
    }
    _canonical(header)
    writer = _Writer(Path(output))
    inventory = _Inventory(client, writer, limits)
    try:
        writer.emit(header)
        for bucket, purposes in sorted(grouped.items()):
            inventory.bucket(bucket, purposes)
            if inventory.stopped:
                break
        return writer.finish(
            {
                "complete": inventory.counts["issues"] == 0,
                "migration_ready": False,
                "atomic_snapshot": False,
                "finished_at": _now(),
                "counts": inventory.counts,
            }
        )
    finally:
        writer.stream.close()


def verify_inventory(path):
    """Verify canonical framing and accidental truncation/tampering, not authenticity."""
    digest, count, size, header, tail = hashlib.sha256(), 0, 0, None, None
    try:
        with Path(path).open("rb") as stream:
            while True:
                line = stream.readline(_MAX_RECORD + 1)
                if not line:
                    break
                if (
                    len(line) > _MAX_RECORD
                    or not line.endswith(b"\n")
                    or tail is not None
                ):
                    raise InventoryError("invalid_report")
                record = json.loads(line)
                if not isinstance(record, dict) or _canonical(record) != line:
                    raise InventoryError("invalid_report")
                if count == 0:
                    if record.get("type") != "header" or record.get("schema") != SCHEMA:
                        raise InventoryError("invalid_report")
                    header = record
                if record.get("type") == "tail":
                    tail = record
                else:
                    digest.update(line)
                    count += 1
                    size += len(line)
        if header is None or tail is None or tail.get("schema") != SCHEMA:
            raise InventoryError("unsealed_report")
        if (
            tail.get("prior_records") != count
            or tail.get("prior_bytes") != size
            or tail.get("sha256") != digest.hexdigest()
        ):
            raise InventoryError("integrity_mismatch")
        if (
            type(tail.get("complete")) is not bool
            or tail.get("migration_ready") is not False
        ):
            raise InventoryError("invalid_report")
        return tail
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, InventoryError):
            raise
        raise InventoryError("invalid_report") from None
