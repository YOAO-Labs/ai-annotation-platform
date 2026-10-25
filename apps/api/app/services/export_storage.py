"""Immutable physical ownership and bounded I/O for one export candidate.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

from contextlib import contextmanager
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
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access, storage_presigning
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
        or ref["schema"] not in {1, 2}
        or ref["purpose"] != "export"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["storage_identity"], str)
        or _SHA256.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    identifier = _canonical_uuid(ref["location_id"])
    key = ref["object_key"]
    match = (
        re.search(
            r"(?:image|video)/"
            + _UUID_PART
            + r"/"
            + _UUID_PART
            + r"/"
            + _UUID_PART
            + r"\.zip$",
            key,
        )
        if isinstance(key, str)
        else None
    )
    if match is None or (expected_key is not None and match[0] != expected_key):
        _reject()
    logical = match[0]
    if ref["schema"] == 1:
        if (
            identifier != str(LEGACY_LOCATION_ID)
            or ref["config_revision"] != 1
            or ref["binding_generation"] != 1
            or key != logical
        ):
            _reject()
    else:
        if ref["binding_generation"] < 2 or len(key.encode("utf-8")) > 1024:
            _reject()
        if identifier == str(LEGACY_LOCATION_ID):
            if ref["config_revision"] != 1 or key != logical:
                _reject()
        else:
            from app.services.export_storage_target import (
                MANAGED_NAMESPACE,
                managed_prefix,
            )

            prefix = key[: -len(logical)]
            if (
                not prefix.endswith(MANAGED_NAMESPACE)
                or managed_prefix(prefix[: -len(MANAGED_NAMESPACE)]) != prefix
            ):
                _reject("export_storage_invalid_key")
    endpoint = _endpoint(ref["endpoint_url"])
    if (
        identifier == str(LEGACY_LOCATION_ID) and endpoint != _runtime_endpoint()
    ) or ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    return Owner(ref["bucket"], key, deepcopy(ref))


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


async def allocate(db, row) -> dict:
    """Create-only allocation; the caller persists the independent upload row."""
    key = key_for(row.media, row.project_id, row.job_id, row.id)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("export_storage_binding_unproven")
    initial = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "export")
        .execution_options(populate_existing=True)
    )
    if initial is None:
        _reject("export_storage_binding_unproven")
    binding_facts = initial.location_id, initial.bucket, initial.generation
    initial_location = await db.get(
        StorageLocation, initial.location_id, populate_existing=True
    )
    if initial_location is None:
        _reject("export_storage_binding_unproven")
    connection_id = getattr(initial_location, "connection_id", None)
    if connection_id is not None:
        await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == connection_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    location = await db.scalar(
        select(StorageLocation)
        .where(StorageLocation.id == binding_facts[0])
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "export")
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.id != binding_facts[0]
        or getattr(location, "connection_id", None) != connection_id
        or binding is None
        or (binding.location_id, binding.bucket, binding.generation) != binding_facts
    ):
        _reject("export_storage_binding_unproven")
    from app.services.export_storage_target import target_for

    target = await target_for(db, location)
    if (
        type(binding.generation) is not int
        or binding.generation < 1
        or binding.bucket != target.bucket
        or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
    ):
        _reject("export_storage_binding_unproven")
    endpoint = target.endpoint_url
    return resolve_ref(
        {
            "schema": 1 if binding.generation == 1 else 2,
            "purpose": "export",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "object_key": target.key_prefix + key,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_key=key,
    ).ref


async def io_target(db, row):
    """Capture current credentials while keeping the saved physical address fixed."""
    owner = resolve(row)
    if owner.ref["schema"] == 1:
        return None
    from app.services.export_storage_target import target_for

    identifier = uuid.UUID(owner.ref["location_id"])
    location = await db.get(StorageLocation, identifier, populate_existing=True)
    target = await target_for(db, location)
    if (
        target.endpoint_url != owner.ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix + key_for(row.media, row.project_id, row.job_id, row.id)
        != owner.object_key
    ):
        _reject()
    return target


@contextmanager
def client_context(row, target):
    owner = resolve(row)
    if target is None:
        if owner.ref["schema"] != 1:
            _reject("export_storage_binding_unproven")
        yield storage_service.client
        return
    if (
        str(target.location_id) != owner.ref["location_id"]
        or target.endpoint_url != owner.ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix + key_for(row.media, row.project_id, row.job_id, row.id)
        != owner.object_key
    ):
        _reject()
    with target.client() as client:
        yield client


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


def verify(output, *, missing_ok=False, target=None) -> bool:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(output)
    with client_context(output, target) as client:
        return _verify(client, output, owner, missing_ok=missing_ok)


def _verify(client, output, owner, *, missing_ok=False):
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        head = client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != output.size_bytes
            or not isinstance(etag, str)
            or not 0 < len(etag) <= 1024
        ):
            _reject("export_storage_integrity_mismatch")
        response = client.get_object(**params, IfMatch=etag)
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
        after = client.head_object(**params)
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


def write(output, path, *, target=None) -> None:
    """Upload one independent candidate; no failure authorizes deletion."""
    owner = resolve(output)
    if file_receipt(path) != (output.sha256, output.size_bytes):
        _reject("export_storage_integrity_mismatch")
    try:
        with client_context(output, target) as client:
            if target is None:
                storage_service.upload_file(
                    str(path),
                    owner.object_key,
                    bucket=owner.bucket,
                    content_type="application/zip",
                )
            else:
                client.upload_file(
                    str(path),
                    owner.bucket,
                    owner.object_key,
                    ExtraArgs={"ContentType": "application/zip"},
                )
            _verify(client, output, owner)
    except Exception as exc:
        raise _unavailable(exc) from None


def delete(output, *, target=None) -> None:
    """Delete only the precise object of a caller's committed retirement."""
    owner = resolve(output)
    try:
        with client_context(output, target) as client:
            if target is None:
                storage_service.delete_object(owner.object_key, bucket=owner.bucket)
            else:
                client.delete_object(Bucket=owner.bucket, Key=owner.object_key)
    except Exception as exc:
        raise _unavailable(exc) from None


def sign(output, *, download_name, now=None, target=None) -> str:
    """Issue only the remaining immutable retention window, without alignment."""
    owner = resolve(output)
    current = _instant(datetime.now(timezone.utc) if now is None else now)
    seconds = min(
        RETENTION_SECONDS, int((_instant(output.expires_at) - current).total_seconds())
    )
    if seconds <= 0:
        _reject("export_storage_expired")
    try:
        with client_context(output, target) as client:
            if owner.ref["location_id"] == str(LEGACY_LOCATION_ID):
                return storage_service.generate_download_url(
                    owner.object_key,
                    expires_in=seconds,
                    bucket=owner.bucket,
                    download_name=download_name,
                    align=False,
                )
            return storage_presigning.generate_url(
                client,
                "get_object",
                params={
                    "Bucket": owner.bucket,
                    "Key": owner.object_key,
                    "ResponseContentDisposition": f'attachment; filename="{download_name}"',
                },
                expires_in=seconds,
                audience="internal",
            )
    except Exception as exc:
        raise _unavailable(exc) from None
