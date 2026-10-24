"""Immutable physical ownership and bounded I/O for one export candidate.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import time
import uuid
import re
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "location_id",
        "config_revision",
        "binding_generation",
        "endpoint_url",
        "bucket",
        "object_key",
        "storage_identity",
    }
)
MAX_BYTES = 64 * 1024**3
VERIFY_SECONDS = 600
RETENTION_SECONDS = 7 * 24 * 3600
CHUNK_BYTES = 1024 * 1024
_UUID_PART = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(
    r"(?:image|video)/" + _UUID_PART + r"/" + _UUID_PART + r"/" + _UUID_PART + r"\.zip"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


_MESSAGES = {
    "export_storage_unpinned": "导出候选尚未保存存储归属",
    "export_storage_invalid_key": "导出对象键与候选记录不匹配",
    "export_storage_invalid_receipt": "导出内容回执或保存期限不合法",
    "export_storage_binding_unproven": "导出存储绑定尚未登记或无法核实",
    "export_storage_integrity_mismatch": "导出字节与独立内容回执不一致",
    "export_storage_missing": "已保存的导出对象不存在",
    "export_storage_expired": "导出产物已超过固定保存期限",
    "export_storage_budget": "导出核验超过读取预算",
    "export_storage_unavailable": "导出存储暂不可用",
}


class ExportStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(
            message
            if message is not None
            else _MESSAGES.get(code, "导出存储身份与当前端点不匹配")
        )
        self.code = code
        self.status_code = status_code


def _reject(code="export_storage_identity_changed"):
    raise ExportStorageError(code)


def _canonical_uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("export_storage_invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("export_storage_invalid_key")
    if str(value) != result:
        _reject("export_storage_invalid_key")
    return result


def key_for(media, project_id, job_id, output_id) -> str:
    if not isinstance(media, str) or media not in {"image", "video"}:
        _reject("export_storage_invalid_key")
    return (
        "/".join(
            [
                media,
                _canonical_uuid(project_id),
                _canonical_uuid(job_id),
                _canonical_uuid(output_id),
            ]
        )
        + ".zip"
    )


def _plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
    )


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
        ):
            _reject()
        parsed.port
    except ValueError:
        _reject()
    return value


def _runtime_endpoint():
    expected = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    actual = getattr(
        getattr(getattr(storage_service, "client", None), "meta", None),
        "endpoint_url",
        None,
    )
    if not isinstance(actual, str) or actual.rstrip("/") != expected:
        _reject()
    return _endpoint(expected)


@dataclass(frozen=True)
class Owner:
    bucket: str
    object_key: str
    _ref: dict = field(repr=False)

    @property
    def ref(self):
        return deepcopy(self._ref)


def resolve_ref(ref, *, expected_key=None) -> Owner:
    if ref is None:
        _reject("export_storage_unpinned")
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
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["object_key"], str)
        or _KEY.fullmatch(ref["object_key"]) is None
        or (expected_key is not None and ref["object_key"] != expected_key)
        or not isinstance(ref["storage_identity"], str)
        or _SHA256.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref[
        "storage_identity"
    ] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    value = deepcopy(ref)
    return Owner(value["bucket"], value["object_key"], value)


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _reject("export_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record) -> Owner:
    key = key_for(
        getattr(record, "media", None),
        getattr(record, "project_id", None),
        getattr(record, "job_id", None),
        getattr(record, "id", None),
    )
    sha256 = getattr(record, "sha256", None)
    size_bytes = getattr(record, "size_bytes", None)
    file_count = getattr(record, "file_count", None)
    output_format = getattr(record, "format", None)
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= MAX_BYTES
        or type(file_count) is not int
        or not 0 <= file_count <= 2147483647
        or not isinstance(output_format, str)
        or not 0 < len(output_format) <= 1000
        or any(ord(c) < 32 or ord(c) == 127 for c in output_format)
    ):
        _reject("export_storage_invalid_receipt")
    created = _instant(getattr(record, "created_at", None))
    expires = _instant(getattr(record, "expires_at", None))
    if not created < expires <= created + timedelta(seconds=RETENTION_SECONDS):
        _reject("export_storage_invalid_receipt")
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


async def allocate(db, output) -> dict:
    """Create-only allocation; the caller persists the independent candidate."""
    key = key_for(output.media, output.project_id, output.job_id, output.id)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("export_storage_binding_unproven")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "export")
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.id != LEGACY_LOCATION_ID
        or location.kind != "s3"
        or type(location.config_revision) is not int
        or location.config_revision != 1
        or location.enabled is not True
        or not isinstance(location.config, dict)
        or type(location.config.get("use_ssl")) is not bool
        or location.config
        != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
        or binding is None
        or binding.location_id != LEGACY_LOCATION_ID
        or type(binding.generation) is not int
        or binding.generation != 1
        or binding.bucket != storage_service.export_bucket
    ):
        _reject("export_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "export",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "object_key": key,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_key=key,
    ).ref


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("export_storage_budget")


def _unavailable(exc):
    error = ExportStorageError("export_storage_unavailable", status_code=503)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def file_receipt(path) -> tuple[str, int]:
    """Hash a bounded local output, without holding the ZIP in memory."""
    deadline = time.monotonic() + VERIFY_SECONDS
    checksum, size = hashlib.sha256(), 0
    try:
        with Path(path).open("rb") as source:
            while True:
                _budget(deadline)
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    _reject("export_storage_integrity_mismatch")
                checksum.update(chunk)
        _budget(deadline)
    except ExportStorageError:
        raise
    except OSError as exc:
        raise _unavailable(exc) from None
    if size <= 0:
        _reject("export_storage_integrity_mismatch")
    return checksum.hexdigest(), size


def verify(output, *, missing_ok=False) -> bool:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(output)
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        head = storage_service.client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != output.size_bytes
            or not isinstance(etag, str)
            or not 0 < len(etag) <= 1024
        ):
            _reject("export_storage_integrity_mismatch")
        response = storage_service.client.get_object(**params, IfMatch=etag)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                response.get("ETag") != etag
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != output.size_bytes
            ):
                _reject("export_storage_integrity_mismatch")
            checksum, size = hashlib.sha256(), 0
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, output.size_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > output.size_bytes:
                    _reject("export_storage_integrity_mismatch")
                checksum.update(chunk)
            if size != output.size_bytes or checksum.hexdigest() != output.sha256:
                _reject("export_storage_integrity_mismatch")
        _budget(deadline)
        after = storage_service.client.head_object(**params)
        if (
            after.get("ETag") != etag
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != output.size_bytes
        ):
            _reject("export_storage_integrity_mismatch")
        _budget(deadline)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            if missing_ok:
                return False
            raise ExportStorageError("export_storage_missing") from None
        if code in {"412", "PreconditionFailed"}:
            raise ExportStorageError("export_storage_integrity_mismatch") from None
        raise _unavailable(exc) from None
    except ExportStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
    return True


def write(output, path) -> None:
    """Upload one independent candidate; no failure authorizes deletion."""
    owner = resolve(output)
    if file_receipt(path) != (output.sha256, output.size_bytes):
        _reject("export_storage_integrity_mismatch")
    try:
        storage_service.upload_file(
            str(path),
            owner.object_key,
            bucket=owner.bucket,
            content_type="application/zip",
        )
    except Exception as exc:
        raise _unavailable(exc) from None
    verify(output)


def delete(output) -> None:
    """Delete only the precise object of a caller's committed retirement."""
    owner = resolve(output)
    try:
        storage_service.delete_object(owner.object_key, bucket=owner.bucket)
    except Exception as exc:
        raise _unavailable(exc) from None


def sign(output, *, download_name, now=None) -> str:
    """Issue only the remaining immutable retention window, without alignment."""
    owner = resolve(output)
    current = _instant(datetime.now(timezone.utc) if now is None else now)
    seconds = min(
        RETENTION_SECONDS, int((_instant(output.expires_at) - current).total_seconds())
    )
    if seconds <= 0:
        _reject("export_storage_expired")
    try:
        return storage_service.generate_download_url(
            owner.object_key,
            expires_in=seconds,
            bucket=owner.bucket,
            download_name=download_name,
            align=False,
        )
    except Exception as exc:
        raise _unavailable(exc) from None
