"""Immutable physical ownership and bounded I/O for one guide image.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, make_dataclass
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
import re
import unicodedata
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.config import settings
from app.schemas.guide_asset import ALLOWED_GUIDE_ASSET_TYPES, GuideAssetEntry
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
MAX_BYTES = 5 * 1024**2
VERIFY_SECONDS = 600
CHUNK_BYTES = 1024 * 1024
_UUID_PART = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(r"projects/" + _UUID_PART + "/guide/" + _UUID_PART)
ALLOWED_TYPES = ALLOWED_GUIDE_ASSET_TYPES
_FACTS = (
    "id",
    "owner_id",
    "project_id",
    "sha256",
    "size_bytes",
    "mime_type",
    "file_name",
    "created_at",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


_MESSAGES = {
    "guide_asset_storage_identity_changed": "附件存储身份与当前端点不匹配",
    "guide_asset_storage_unpinned": "附件尚未保存存储归属，请重新上传",
    "guide_asset_storage_invalid_key": "附件对象键与上传记录不匹配",
    "guide_asset_storage_invalid_receipt": "指南图片内容回执不合法",
    "guide_asset_storage_invalid_file_name": "附件文件名不合法",
    "guide_asset_storage_invalid_mime_type": "附件 MIME 类型不合法",
    "guide_asset_storage_binding_unproven": "附件存储绑定尚未登记或无法核实",
    "guide_asset_storage_integrity_mismatch": "附件字节与独立内容回执不一致",
    "guide_asset_storage_missing": "已保存的附件对象不存在",
    "guide_asset_storage_budget": "附件核验超过读取预算",
    "guide_asset_storage_unavailable": "附件存储暂不可用",
}


class GuideAssetStorageError(ValueError):
    def __init__(self, code, status_code=409):
        # Only fixed messages are exposed; provider exception text is never safe.
        super().__init__(_MESSAGES.get(code, "附件存储暂不可用"))
        self.code = code
        self.status_code = status_code


def _reject(code="guide_asset_storage_identity_changed"):
    raise GuideAssetStorageError(code)


def _canonical_uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("guide_asset_storage_invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("guide_asset_storage_invalid_key")
    if str(value) != result:
        _reject("guide_asset_storage_invalid_key")
    return result


def key_for(project_id, upload_id) -> str:
    return f"projects/{_canonical_uuid(project_id)}/guide/{_canonical_uuid(upload_id)}"


def safe_file_name(value) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 255
        or value in {".", ".."}
        or any(char in value for char in "/\\")
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise GuideAssetStorageError(
            "guide_asset_storage_invalid_file_name", status_code=422
        )
    return value


def normalize_mime_type(value) -> str:
    if not isinstance(value, str) or any(
        unicodedata.category(char) == "Cc" for char in value
    ):
        raise GuideAssetStorageError("guide_asset_storage_invalid_mime_type", 422)
    normalized = value.split(";", 1)[0].strip().lower()
    if normalized not in ALLOWED_TYPES:
        raise GuideAssetStorageError("guide_asset_storage_invalid_mime_type", 422)
    return normalized


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
        _reject("guide_asset_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "annotations"
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
        _reject("guide_asset_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record) -> Owner:
    key = key_for(getattr(record, "project_id", None), getattr(record, "id", None))
    _canonical_uuid(getattr(record, "owner_id", None))
    sha256, size_bytes = (
        getattr(record, "sha256", None),
        getattr(record, "size_bytes", None),
    )
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 1 <= size_bytes <= MAX_BYTES
    ):
        _reject("guide_asset_storage_invalid_receipt")
    try:
        safe_file_name(getattr(record, "file_name", None))
        mime_type = getattr(record, "mime_type", None)
        if normalize_mime_type(mime_type) != mime_type:
            _reject("guide_asset_storage_invalid_receipt")
    except GuideAssetStorageError:
        _reject("guide_asset_storage_invalid_receipt")
    _instant(getattr(record, "created_at", None))
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


# A copied locator must never be mutable through a frozen receipt's attributes.
_Snapshot = make_dataclass(
    "GuideAssetSnapshot",
    [(name, object) for name in _FACTS] + [("_ref_json", str)],
    frozen=True,
    namespace={"storage_ref": property(lambda self: json.loads(self._ref_json))},
)


def snapshot(record):
    resolve(record)
    return _Snapshot(
        *(deepcopy(getattr(record, name, None)) for name in _FACTS),
        json.dumps(record.storage_ref, sort_keys=True),
    )


def same(record, receipt) -> bool:
    return all(
        getattr(record, name, None) == getattr(receipt, name, None) for name in _FACTS
    ) and getattr(record, "storage_ref", None) == getattr(receipt, "storage_ref", None)


def access(ref) -> Owner:
    return resolve_ref(ref)


def _client(client, owner):
    endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
    if (
        not isinstance(endpoint, str)
        or endpoint.rstrip("/") != owner.ref["endpoint_url"]
    ):
        _reject()


async def allocate(db, record) -> dict:
    """Create-only allocation; the caller persists the independent upload."""
    key = key_for(record.project_id, record.id)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("guide_asset_storage_binding_unproven")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "annotations")
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
        or binding.purpose != "annotations"
        or binding.location_id != LEGACY_LOCATION_ID
        or type(binding.generation) is not int
        or binding.generation != 1
        or binding.bucket != storage_service.bucket
    ):
        _reject("guide_asset_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "annotations",
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
        _reject("guide_asset_storage_budget")


def _unavailable(exc):
    error = GuideAssetStorageError("guide_asset_storage_unavailable", status_code=503)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _version(metadata):
    value = metadata.get("VersionId")
    if value is None or value == "null":
        return None
    if not _plain(value, 1024):
        _reject("guide_asset_storage_integrity_mismatch")
    return value


def read(client, record, *, missing_ok=False) -> bytes | None:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(record)
    _client(client, owner)
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        head = client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != record.size_bytes
            or not _plain(etag, 1024)
        ):
            _reject("guide_asset_storage_integrity_mismatch")
        version = _version(head)
        condition = {"IfMatch": etag}
        if version is not None:
            condition["VersionId"] = version
        response = client.get_object(**params, **condition)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                response.get("ETag") != etag
                or _version(response) != version
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != record.size_bytes
            ):
                _reject("guide_asset_storage_integrity_mismatch")
            checksum, size, chunks = hashlib.sha256(), 0, []
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, record.size_bytes - size + 1))
                if type(chunk) is not bytes:
                    _reject("guide_asset_storage_integrity_mismatch")
                if not chunk:
                    break
                size += len(chunk)
                if size > record.size_bytes:
                    _reject("guide_asset_storage_integrity_mismatch")
                checksum.update(chunk)
                chunks.append(chunk)
            if size != record.size_bytes or checksum.hexdigest() != record.sha256:
                _reject("guide_asset_storage_integrity_mismatch")
        _budget(deadline)
        after = client.head_object(**params)
        if (
            after.get("ETag") != etag
            or _version(after) != version
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != record.size_bytes
        ):
            _reject("guide_asset_storage_integrity_mismatch")
        _budget(deadline)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
            if missing_ok:
                return None
            raise GuideAssetStorageError("guide_asset_storage_missing", 404) from None
        if code in {"412", "PreconditionFailed"}:
            raise GuideAssetStorageError(
                "guide_asset_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except GuideAssetStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
    return b"".join(chunks)


def write(client, record, data) -> None:
    """Create only the saved key; no failure authorizes compensation deletion."""
    owner = resolve(record)
    _client(client, owner)
    if (
        type(data) is not bytes
        or len(data) != record.size_bytes
        or hashlib.sha256(data).hexdigest() != record.sha256
    ):
        _reject("guide_asset_storage_integrity_mismatch")
    try:
        client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=data,
            ContentLength=len(data),
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
            raise GuideAssetStorageError(
                "guide_asset_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None
    read(client, record)


def delete(client, record, *, missing_ok=True) -> None:
    """Delete only the precise object of a caller's committed retirement."""
    owner = resolve(record)
    _client(client, owner)
    try:
        client.delete_object(Bucket=owner.bucket, Key=owner.object_key)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        if str(exc.response.get("Error", {}).get("Code", "")) in {
            "404",
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
        }:
            if missing_ok:
                return
            raise GuideAssetStorageError("guide_asset_storage_missing", 404) from None
        raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None


def entry(record) -> dict:
    """Use one JSON representation for persisted lists and wire responses."""
    owner = resolve(record)
    return GuideAssetEntry(
        key=owner.object_key,
        original_name=record.file_name,
        content_type=record.mime_type,
        size=record.size_bytes,
        uploaded_at=_instant(record.created_at),
    ).model_dump(mode="json")
