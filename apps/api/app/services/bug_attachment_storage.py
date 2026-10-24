"""Immutable physical ownership and bounded I/O for one bug attachment.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import io
import time
import uuid
import re
import unicodedata
import warnings
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError
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
MAX_BYTES = 10 * 1024**2
MAX_PIXELS = 40_000_000
VERIFY_SECONDS = 600
RETENTION_SECONDS = 180 * 86400
CHUNK_BYTES = 1024 * 1024
_UUID_PART = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(
    r"bug-report-attachments/" + _UUID_PART + r"/" + _UUID_PART + r"\.(?:png|jpg|webp)"
)
_MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


_MESSAGES = {
    "bug_attachment_storage_identity_changed": "附件存储身份与当前端点不匹配",
    "bug_attachment_storage_unpinned": "附件尚未保存存储归属，请重新上传",
    "bug_attachment_storage_invalid_key": "附件对象键与上传记录不匹配",
    "bug_attachment_storage_invalid_receipt": "附件内容回执或保存期限不合法",
    "bug_attachment_storage_invalid_file_name": "附件文件名不合法",
    "bug_attachment_storage_invalid_image": "附件必须是完整的 PNG、JPEG 或 WebP 图片，最多 10 MiB 和 4000 万像素",
    "bug_attachment_storage_binding_unproven": "附件存储绑定尚未登记或无法核实",
    "bug_attachment_storage_integrity_mismatch": "附件字节与独立内容回执不一致",
    "bug_attachment_storage_missing": "已保存的附件对象不存在",
    "bug_attachment_storage_budget": "附件核验超过读取预算",
    "bug_attachment_storage_unavailable": "附件存储暂不可用",
}


class BugAttachmentStorageError(ValueError):
    def __init__(self, code, *, status_code=409):
        # Only fixed messages are exposed; provider exception text is never safe.
        super().__init__(_MESSAGES.get(code, "附件存储暂不可用"))
        self.code = code
        self.status_code = status_code


def _reject(code="bug_attachment_storage_identity_changed"):
    raise BugAttachmentStorageError(code)


def _canonical_uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("bug_attachment_storage_invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("bug_attachment_storage_invalid_key")
    if str(value) != result:
        _reject("bug_attachment_storage_invalid_key")
    return result


def key_for(owner_id, upload_id, mime_type) -> str:
    if not isinstance(mime_type, str) or mime_type not in _MIME_EXTENSIONS:
        _reject("bug_attachment_storage_invalid_key")
    return (
        f"bug-report-attachments/{_canonical_uuid(owner_id)}/{_canonical_uuid(upload_id)}"
        f".{_MIME_EXTENSIONS[mime_type]}"
    )


def safe_file_name(value) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 255
        or value in {".", ".."}
        or any(char in value for char in "/\\")
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise BugAttachmentStorageError(
            "bug_attachment_storage_invalid_file_name", status_code=422
        )
    return value


def validate_image(data, mime_type) -> None:
    """Verify original bytes without normalization or changing image metadata."""
    try:
        if (
            type(data) is not bytes
            or not 0 < len(data) <= MAX_BYTES
            or not isinstance(mime_type, str)
            or mime_type not in _IMAGE_FORMATS
        ):
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if (
                    image.format != _IMAGE_FORMATS[mime_type]
                    or min(width, height) <= 0
                    or width * height > MAX_PIXELS
                ):
                    raise ValueError
                image.verify()
            # JPEG verify() alone does not decode its scan data. A bounded decode
            # also rejects truncated streams without rewriting the original bytes.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        ValueError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise BugAttachmentStorageError(
            "bug_attachment_storage_invalid_image", status_code=422
        ) from None


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
        _reject("bug_attachment_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "bug-reports"
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
        _reject("bug_attachment_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record) -> Owner:
    key = key_for(
        getattr(record, "owner_id", None),
        getattr(record, "id", None),
        getattr(record, "mime_type", None),
    )
    sha256 = getattr(record, "sha256", None)
    size_bytes = getattr(record, "size_bytes", None)
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= MAX_BYTES
    ):
        _reject("bug_attachment_storage_invalid_receipt")
    try:
        safe_file_name(getattr(record, "file_name", None))
    except BugAttachmentStorageError:
        _reject("bug_attachment_storage_invalid_receipt")
    created = _instant(getattr(record, "created_at", None))
    expires = _instant(getattr(record, "expires_at", None))
    if not created < expires <= created + timedelta(seconds=RETENTION_SECONDS):
        _reject("bug_attachment_storage_invalid_receipt")
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


async def allocate(db, record) -> dict:
    """Create-only allocation; the caller persists the independent upload."""
    key = key_for(record.owner_id, record.id, record.mime_type)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("bug_attachment_storage_binding_unproven")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "bug-reports")
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
        or binding.purpose != "bug-reports"
        or binding.location_id != LEGACY_LOCATION_ID
        or type(binding.generation) is not int
        or binding.generation != 1
        or binding.bucket != storage_service.bug_reports_bucket
    ):
        _reject("bug_attachment_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "bug-reports",
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
        _reject("bug_attachment_storage_budget")


def _unavailable(exc):
    error = BugAttachmentStorageError(
        "bug_attachment_storage_unavailable", status_code=503
    )
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def read(record, *, missing_ok=False) -> bytes | None:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(record)
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        head = storage_service.client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != record.size_bytes
            or not isinstance(etag, str)
            or not 0 < len(etag) <= 1024
        ):
            _reject("bug_attachment_storage_integrity_mismatch")
        response = storage_service.client.get_object(**params, IfMatch=etag)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                response.get("ETag") != etag
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != record.size_bytes
            ):
                _reject("bug_attachment_storage_integrity_mismatch")
            checksum, size, chunks = hashlib.sha256(), 0, []
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, record.size_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > record.size_bytes:
                    _reject("bug_attachment_storage_integrity_mismatch")
                checksum.update(chunk)
                chunks.append(chunk)
            if size != record.size_bytes or checksum.hexdigest() != record.sha256:
                _reject("bug_attachment_storage_integrity_mismatch")
        _budget(deadline)
        after = storage_service.client.head_object(**params)
        if (
            after.get("ETag") != etag
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != record.size_bytes
        ):
            _reject("bug_attachment_storage_integrity_mismatch")
        _budget(deadline)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            if missing_ok:
                return None
            raise BugAttachmentStorageError("bug_attachment_storage_missing") from None
        if code in {"412", "PreconditionFailed"}:
            raise BugAttachmentStorageError(
                "bug_attachment_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except BugAttachmentStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
    return b"".join(chunks)


def write(record, data) -> None:
    """Create only the saved key; no failure authorizes compensation deletion."""
    owner = resolve(record)
    if (
        type(data) is not bytes
        or len(data) != record.size_bytes
        or hashlib.sha256(data).hexdigest() != record.sha256
    ):
        _reject("bug_attachment_storage_integrity_mismatch")
    validate_image(data, record.mime_type)
    try:
        storage_service.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=data,
            ContentType=record.mime_type,
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        if str(exc.response.get("Error", {}).get("Code", "")) in {
            "412",
            "PreconditionFailed",
            "ConditionalRequestConflict",
            "409",
        }:
            raise BugAttachmentStorageError(
                "bug_attachment_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None
    read(record)


def delete(record) -> None:
    """Delete only the precise object of a caller's committed retirement."""
    owner = resolve(record)
    try:
        storage_service.delete_object(owner.object_key, bucket=owner.bucket)
    except Exception as exc:
        raise _unavailable(exc) from None
