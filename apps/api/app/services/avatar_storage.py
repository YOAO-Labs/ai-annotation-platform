"""Immutable physical ownership for one normalized avatar object.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.services.avatar_image import MAX_AVATAR_FILE_BYTES
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
_TOKEN = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


_MESSAGES = {
    "avatar_storage_unpinned": "头像尚未保存存储归属，请重新上传",
    "avatar_storage_invalid_key": "头像对象键与上传记录不匹配",
    "avatar_storage_invalid_receipt": "头像内容回执不完整或不合法",
    "avatar_storage_binding_unproven": "头像存储绑定尚未登记或无法核实",
}


class AvatarStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(
            message
            if message is not None
            else _MESSAGES.get(code, "头像存储身份与当前端点不匹配")
        )
        self.code = code
        self.status_code = status_code


def _reject(code="avatar_storage_identity_changed"):
    raise AvatarStorageError(code)


def object_key_for(token) -> str:
    if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
        _reject("avatar_storage_invalid_key")
    return f"{token}.webp"


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
        _reject("avatar_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "avatars"
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] != 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] != 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["object_key"], str)
        or re.fullmatch(r"[0-9a-f]{32}\.webp", ref["object_key"]) is None
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


def resolve(record) -> Owner:
    key = object_key_for(getattr(record, "token", None))
    sha256 = getattr(record, "sha256", None)
    size_bytes = getattr(record, "size_bytes", None)
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= MAX_AVATAR_FILE_BYTES
    ):
        _reject("avatar_storage_invalid_receipt")
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


async def allocate(db, token) -> dict:
    """Create-only allocation; the caller persists the independent upload row."""
    key = object_key_for(token)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("avatar_storage_binding_unproven")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "avatars")
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.id != LEGACY_LOCATION_ID
        or location.kind != "s3"
        or location.config_revision != 1
        or not location.enabled
        or location.config
        != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
        or binding is None
        or binding.location_id != LEGACY_LOCATION_ID
        or binding.generation != 1
        or binding.bucket != storage_service.avatars_bucket
    ):
        _reject("avatar_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "avatars",
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
