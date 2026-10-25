"""Bounded, provider-independent assertions for the isolated S3 comparison.

This module never reads application settings or contacts a configured service.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

SCHEMA = "aap.storage-s3-comparison/v1"
OWNER_LABEL = "aap.e07.owner"
CHECKOUT_LABEL = "aap.e07.checkout"
STATUSES = {"passed", "failed", "not_supported", "not_run", "inconclusive"}
CATEGORIES = (
    "basic_objects",
    "browser_upload",
    "large_objects",
    "media_reads",
    "checksums",
    "lifecycle",
    "frozen_versions",
    "operations",
)
# Explicit original-matrix coverage remains visible even after a successful PoC.
CASES = {
    "basic": ("basic_objects", "PUT/GET/HEAD, metadata and raw application keys"),
    "prefix_pagination": ("basic_objects", "Stable prefix pagination and deletion"),
    "conditional_reads": ("media_reads", "GET/HEAD conditions and byte ranges"),
    "conditional_create": ("basic_objects", "Concurrent If-None-Match creates once"),
    "versions": ("frozen_versions", "Exact old versions, markers and pagination"),
    "mutable_null": ("frozen_versions", "Suspended mutable null slot"),
    "multipart": ("large_objects", "Part pagination, rejected completion and retry"),
    "multipart_abort": ("large_objects", "Interrupted multipart and abort absence"),
    "media": ("media_reads", "Generated PNG, MP4 and ZIP decode and SHA-256"),
    "signatures": ("browser_upload", "Actual StorageService internal/ML/proxy URLs"),
    "signature_rejections": ("browser_upload", "Tamper, credentials and true expiry"),
    "browser_same_origin": (
        "browser_upload",
        "Real browser relative PUT/GET/Range/retry",
    ),
    "browser_cross_origin": (
        "browser_upload",
        "Real browser CORS/preflight/headers/retry",
    ),
    "lifecycle_configuration": ("lifecycle", "Current application rules roundtrip"),
    "encryption_probe": ("checksums", "Explicit SSE-S3 request and full digest"),
    "restart": ("operations", "Restart retains confirmed bytes and metadata"),
    "kill_during_write": ("operations", "Kill during actual unconfirmed PUT"),
    "volume_restore": ("operations", "Stopped same-provider backup into fresh volume"),
    "performance": ("operations", "Identical single/four-way PUT/GET and CPU/RSS"),
    "native_expiry": (
        "lifecycle",
        "Native expiration and persistent preview retention",
    ),
    "real_tls": ("browser_upload", "Real deployment TLS and origin topology"),
    "disk_full": ("operations", "Disk-full recovery"),
    "multinode_failure": ("operations", "Multi-node failure"),
    "rolling_upgrade": ("operations", "Rolling upgrade"),
    "existing_frozen_business": (
        "frozen_versions",
        "Existing frozen business manifest consumers",
    ),
}
DEFERRED = set(CASES) - set(list(CASES)[:19])
PINNED = {
    "seaweedfs": "chrislusf/seaweedfs@sha256:3015781a9ae31319033c5f25658270efb23fd0ec44e11ee7f24b378ee42bfc3a",
    "rustfs": "rustfs/rustfs@sha256:128220dc9382bac81940a1a9616ee8c274a3f3b8f647eedbeb50df5bb8b49922",
    "proxy": "nginx@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c",
}


class ContractError(ValueError):
    """A bounded, public error code; never include a provider message."""


def require(condition, code):
    if not condition:
        raise ContractError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_lock(path, repository):
    lock = json.loads(Path(path).read_bytes())
    require(lock.get("schema") == "aap.storage-s3-candidate-lock/v1", "lock_schema")
    require(lock.get("license_file_base") == "repository", "license_base")
    require(len(lock.get("candidates", [])) == 2, "candidate_count")
    require(
        {c.get("name") for c in lock["candidates"]} == {"seaweedfs", "rustfs"},
        "candidate_names",
    )
    root = Path(repository).resolve()
    for candidate in lock["candidates"]:
        require(candidate.get("image") == PINNED[candidate["name"]], "image_digest")
        require(candidate.get("platform") == "linux/amd64", "image_platform")
        relative = Path(candidate["license_file"])
        require(
            not relative.is_absolute() and ".." not in relative.parts, "license_path"
        )
        license_path = (root / relative).resolve()
        require(license_path.is_relative_to(root), "license_escape")
        require(
            sha256(license_path.read_bytes()) == candidate.get("license_sha256"),
            "license_digest",
        )
        require(candidate.get("license_id") == "Apache-2.0", "license_id")
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", candidate.get("config_digest", "")),
            "config_digest",
        )
    require(lock.get("proxy", {}).get("image") == PINNED["proxy"], "proxy_digest")
    require(lock["proxy"].get("platform") == "linux/amd64", "proxy_platform")
    return lock


def safe_error(exc):
    """Only allowlisted structured codes cross the private/public boundary."""
    if isinstance(exc, ContractError):
        code = str(exc)
        return code if re.fullmatch(r"[a-z0-9_]{1,80}", code) else "assertion_failed"
    response = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code", "")
    allowed = {
        "NotImplemented",
        "NotSupported",
        "XNotImplemented",
        "InvalidRequest",
        "InvalidArgument",
        "AccessDenied",
        "SignatureDoesNotMatch",
        "ExpiredToken",
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "InvalidPart",
        "InvalidPartOrder",
        "NoSuchKey",
        "NoSuchUpload",
        "NoSuchVersion",
        "NoSuchLifecycleConfiguration",
        "404",
        "403",
        "412",
        "304",
        "501",
    }
    if code in allowed:
        return code
    return (
        type(exc).__name__
        if type(exc).__name__
        in {
            "TimeoutError",
            "ConnectionError",
            "EndpointConnectionError",
            "ReadTimeoutError",
            "ConnectTimeoutError",
            "BudgetExceeded",
            "KeyboardInterrupt",
        }
        else "unexpected_error"
    )


class BudgetExceeded(ContractError):
    pass


class Budget:
    def __init__(self, *, seconds=900, objects=256, bytes_per_direction=1024**3):
        for value in (seconds, objects, bytes_per_direction):
            require(
                isinstance(value, (int, float)) and math.isfinite(value) and value > 0,
                "budget_positive_finite",
            )
        self.deadline = time.monotonic() + seconds
        self.object_limit = objects
        self.byte_limit = bytes_per_direction
        self.bytes = {"upload": 0, "download": 0}
        self.keys = set()
        self.lock = threading.Lock()

    def check(self):
        if time.monotonic() >= self.deadline:
            raise BudgetExceeded("budget_time")

    def consume(self, direction, count, key=None):
        with self.lock:
            self.check()
            require(
                direction in self.bytes and isinstance(count, int) and count >= 0,
                "budget_accounting",
            )
            if self.bytes[direction] + count > self.byte_limit:
                raise BudgetExceeded("budget_bytes")
            if (
                key is not None
                and key not in self.keys
                and len(self.keys) >= self.object_limit
            ):
                raise BudgetExceeded("budget_objects")
            self.bytes[direction] += count
            if key is not None:
                self.keys.add(key)

    def hook(self, params, model, **_):
        self.check()
        if model.name in {"PutObject", "UploadPart"}:
            body = params.get("Body", b"")
            require(
                isinstance(body, (bytes, io.BytesIO)), "bounded_upload_body_required"
            )
            size = len(body) if isinstance(body, bytes) else body.getbuffer().nbytes
            self.consume("upload", size, (params["Bucket"], params["Key"]))

    def summary(self):
        return {
            "object_body_bytes_reserved_or_read": dict(self.bytes),
            "distinct_written_keys": len(self.keys),
            "max_objects": self.object_limit,
            "max_bytes_per_direction": self.byte_limit,
        }


def read_body(response, budget, *, expected=None, metadata=None):
    body = response["Body"]
    chunks = []
    try:
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            budget.consume("download", len(chunk))
            chunks.append(chunk)
    finally:
        body.close()
    data = b"".join(chunks)
    require(len(data) == response["ContentLength"], "body_length")
    if expected is not None:
        require(sha256(data) == sha256(expected), "body_digest")
    if metadata is not None:
        require(response.get("Metadata") == metadata, "body_metadata")
    return data


def collect_pages(client, operation, field, *, max_pages=260, **params):
    """Reject pagination loops, omitted cursors and inconsistent duplicates."""
    output, seen, cursors = [], set(), set()
    for _ in range(max_pages):
        response = getattr(client, operation)(**params)
        for item in response.get(field, []):
            identity = (
                (item.get("Key"), item.get("VersionId"))
                if field == "Versions"
                else item.get("Key", item.get("PartNumber"))
            )
            require(identity not in seen, "pagination_duplicate")
            seen.add(identity)
            output.append(item)
        if not response.get("IsTruncated"):
            return output
        if operation == "list_object_versions":
            cursor = (
                response.get("NextKeyMarker"),
                response.get("NextVersionIdMarker"),
            )
            require(bool(cursor[0]), "pagination_cursor_missing")
            params["KeyMarker"] = cursor[0]
            if cursor[1] is not None:
                params["VersionIdMarker"] = cursor[1]
            else:
                params.pop("VersionIdMarker", None)
        elif operation == "list_parts":
            cursor = response.get("NextPartNumberMarker")
            require(cursor is not None, "pagination_cursor_missing")
            params["PartNumberMarker"] = cursor
        else:
            cursor = response.get("NextContinuationToken")
            require(bool(cursor), "pagination_cursor_missing")
            params["ContinuationToken"] = cursor
        require(cursor not in cursors, "pagination_cycle")
        cursors.add(cursor)
    raise ContractError("pagination_budget")


def validate_owner(manifest, item, observed, checkout):
    require(manifest.get("schema") == "aap.storage-poc-owner/v1", "owner_schema")
    require(manifest.get("checkout") == str(Path(checkout).resolve()), "owner_checkout")
    require(re.fullmatch(r"[0-9a-f]{32}", manifest.get("nonce", "")), "owner_nonce")
    require(
        item.get("id") and item in manifest.get("resources", []),
        "owner_creation_record",
    )
    labels = (
        observed.get("Config", {}).get("Labels", {})
        if item["kind"] == "container"
        else observed.get("Labels", {})
    )
    require(labels.get(OWNER_LABEL) == manifest["nonce"], "owner_label")
    require(labels.get(CHECKOUT_LABEL) == manifest["checkout"], "owner_checkout_label")
    actual_id = observed.get("Name") if item["kind"] == "volume" else observed.get("Id")
    require(actual_id == item["id"], "owner_exact_id")
    if item["kind"] == "container":
        require(observed["Config"].get("Image") == item["image"], "owner_image")
    if item["kind"] == "volume":
        require(not observed.get("Options"), "owner_volume_options")
    return True


def summarize(report):
    require(set(report.get("cases", {})) == set(CASES), "case_matrix_incomplete")
    counts = Counter()
    for name, case in report["cases"].items():
        require(case.get("status") in STATUSES, "case_status")
        require(case.get("category") == CASES[name][0], "case_category")
        counts[case["status"]] += 1
    report["counts"] = {status: counts[status] for status in sorted(STATUSES)}
    report["local_baseline_passed"] = all(
        report["cases"][name]["status"] == "passed"
        for name in CASES
        if name not in DEFERRED
    )
    report["selection_ready"] = False
    report["production_ready"] = False
    return report


class Recorder:
    def __init__(self):
        self.cases = {
            name: {
                "category": category,
                "description": description,
                "status": "not_run",
                "stage": "pending",
                "evidence": {},
            }
            for name, (category, description) in CASES.items()
        }
        for name in DEFERRED:
            self.cases[name]["stage"] = "outside_local_qualification"

    @contextmanager
    def case(self, name):
        started = time.monotonic()
        item = self.cases[name]
        item["stage"] = name
        evidence = {}
        try:
            yield evidence
        except Exception as exc:
            item["status"] = (
                "not_supported"
                if safe_error(exc)
                in {"NotImplemented", "NotSupported", "XNotImplemented", "501"}
                else "failed"
            )
            item["error_code"] = safe_error(exc)
        else:
            item["status"] = "passed"
        finally:
            item["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            item["evidence"] = evidence


def public_bytes(report):
    """Fail closed on accidental signed URLs, provider messages or credentials."""
    encoded = json.dumps(report, ensure_ascii=False, indent=2).encode()
    require(len(encoded) <= 256 * 1024, "report_budget")
    lowered = encoded.lower()
    for token in (
        b"x-amz-signature",
        b"x-amz-credential",
        b"awsaccesskeyid=",
        b"secret_access_key",
        b"authorization",
        b"provider_message",
    ):
        require(token not in lowered, "report_sensitive")
    return encoded + b"\n"
