"""Saved media-cache roots and current credentials; only allocation follows Binding."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.db.models.thumbnail_output import media_cache_ref_check
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.export_storage import _endpoint as _parse_endpoint, _plain
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service as storage_service

ref_check = media_cache_ref_check

MANAGED_NAMESPACE = "_aap_managed/media-cache/"
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_LOGICAL = re.compile(
    r"(?:(?:image-pyramids|video-derivatives|thumbnails)/objects/"
    + _UUID
    + r"/|original-cache/v1/"
    + _UUID
    + r"/[0-9a-f]{64}/"
    + _UUID
    + r"/)"
)
_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "location_id",
        "config_revision",
        "binding_generation",
        "endpoint_url",
        "bucket",
        "prefix",
        "storage_identity",
        "key_prefix",
    }
)


class MediaCacheStorageError(ValueError):
    def __init__(self, code="media_cache_storage_identity_changed", status_code=409):
        super().__init__("媒体缓存存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


def _reject(code="media_cache_storage_identity_changed"):
    raise MediaCacheStorageError(code)


def _endpoint(value):
    try:
        return _parse_endpoint(value)
    except ValueError:
        _reject()


def _runtime_endpoint():
    from app.config import settings

    return _endpoint(
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


def managed_prefix(base):
    from app.services.managed_s3_target import managed_prefix as shared

    return shared(base, purpose="media-cache")


def _logical(value):
    if not isinstance(value, str) or not _LOGICAL.fullmatch(value):
        _reject("media_cache_storage_invalid_key")
    return value


@dataclass(frozen=True)
class Owner:
    bucket: str
    prefix: str
    key_prefix: str
    logical_prefix: str
    _ref: dict = field(repr=False)

    @property
    def ref(self):
        return deepcopy(self._ref)

    @property
    def location_id(self):
        return uuid.UUID(self._ref["location_id"])

    def key(self, relative):
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or any(ord(c) < 32 or ord(c) == 127 for c in relative)
            or any(p in {"", ".", ".."} for p in relative.split("/"))
        ):
            _reject("media_cache_storage_invalid_key")
        key = self.prefix + relative
        if len(key) > 1000 or len(key.encode()) > 1024:
            _reject("media_cache_storage_invalid_key")
        return key

    def contains(self, key):
        if not isinstance(key, str) or not key.startswith(self.prefix):
            _reject("media_cache_storage_invalid_key")
        self.key(key[len(self.prefix) :])


def resolve_ref(ref, *, expected_prefix=None):
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 2
        or ref["purpose"] != "media-cache"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(c in ref["bucket"] for c in "/\\?#@%:")
        or not isinstance(ref["key_prefix"], str)
        or not isinstance(ref["prefix"], str)
    ):
        _reject("media_cache_storage_invalid_receipt")
    try:
        identifier = uuid.UUID(ref["location_id"])
        endpoint = _endpoint(ref["endpoint_url"])
        if str(identifier) != ref["location_id"] or endpoint != ref["endpoint_url"]:
            _reject()
    except (TypeError, ValueError, AttributeError):
        _reject("media_cache_storage_invalid_receipt")
    root = ref["key_prefix"]
    if identifier == LEGACY_LOCATION_ID:
        if root or ref["config_revision"] != 1 or endpoint != _runtime_endpoint():
            _reject()
    elif (
        not root.endswith(MANAGED_NAMESPACE)
        or managed_prefix(root[: -len(MANAGED_NAMESPACE)]) != root
    ):
        _reject("media_cache_storage_invalid_key")
    if not ref["prefix"].startswith(root):
        _reject("media_cache_storage_invalid_key")
    logical = _logical(ref["prefix"][len(root) :])
    if expected_prefix is not None and logical != _logical(expected_prefix):
        _reject("media_cache_storage_invalid_key")
    if (
        len(ref["prefix"]) > 1000
        or len(ref["prefix"].encode()) > 1024
        or ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"])
    ):
        _reject()
    return Owner(ref["bucket"], ref["prefix"], root, logical, deepcopy(ref))


async def target_for(db, location, *, require_enabled=True):
    from app.services.managed_s3_target import target_for as shared

    return await shared(
        db, location, purpose="media-cache", require_enabled=require_enabled
    )


async def allocate(db, logical_prefix):
    logical = _logical(logical_prefix)
    connection = await db.connection()
    savepoint = await connection.begin_nested()
    try:
        await ensure_legacy_bindings(db)
        initial = await db.get(StorageBinding, "media-cache", populate_existing=True)
        if initial is None:
            _reject("media_cache_storage_binding_unproven")
        facts = initial.location_id, initial.bucket, initial.generation
        location = await db.get(StorageLocation, facts[0], populate_existing=True)
        if location is None:
            _reject("media_cache_storage_binding_unproven")
        connection_id = location.connection_id
        if connection_id is not None:
            await db.execute(
                select(StorageConnection.id)
                .where(StorageConnection.id == connection_id)
                .with_for_update(read=True, nowait=True)
            )
        location = await db.scalar(
            select(StorageLocation)
            .where(StorageLocation.id == facts[0])
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        binding = await db.scalar(
            select(StorageBinding)
            .where(StorageBinding.purpose == "media-cache")
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if (
            location is None
            or location.connection_id != connection_id
            or binding is None
            or (binding.location_id, binding.bucket, binding.generation) != facts
        ):
            _reject("media_cache_storage_binding_unproven")
        target = await target_for(db, location)
        if (
            binding.bucket != target.bucket
            or type(binding.generation) is not int
            or binding.generation < 1
            or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
        ):
            _reject("media_cache_storage_binding_unproven")
        await storage_access.check(db, location.id, lock=True)
        result = resolve_ref(
            {
                "schema": 2,
                "purpose": "media-cache",
                "location_id": str(location.id),
                "config_revision": location.config_revision,
                "binding_generation": binding.generation,
                "endpoint_url": target.endpoint_url,
                "bucket": target.bucket,
                "key_prefix": target.key_prefix,
                "prefix": target.key_prefix + logical,
                "storage_identity": managed_storage_identity(
                    target.endpoint_url, target.bucket
                ),
            },
            expected_prefix=logical,
        ).ref
    except BaseException as exc:
        await savepoint.rollback()
        if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
            raise MediaCacheStorageError("media_cache_storage_busy") from None
        raise
    else:
        await savepoint.commit()
        return result


async def target_for_ref(db, ref, *, require_enabled=True):
    owner = resolve_ref(ref)
    location = await db.get(StorageLocation, owner.location_id, populate_existing=True)
    target = await target_for(db, location, require_enabled=require_enabled)
    if owner.location_id == LEGACY_LOCATION_ID:
        target = replace(target, bucket=owner.bucket)
    if (
        target.endpoint_url != ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix != owner.key_prefix
    ):
        _reject()
    return target
