"""Saved annotation-file addresses; allocation alone follows the binding."""

from dataclasses import dataclass, replace
import re

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
import uuid

from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.export_storage import _endpoint as _parse_endpoint, _plain
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

MANAGED_NAMESPACE = "_aap_managed/annotations/"
MAX_MANAGED_PREFIX_CHARS = 896
MAX_MANAGED_PREFIX_BYTES = 920
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_LOGICAL = re.compile(
    r"(?:__aap_task_originals__/" + _UUID + r"/" + _UUID + r"/payload"
    r"|raster-masks/objects/" + _UUID + r"[.]json(?:[.]gz)?"
    r"|comment-attachments/"
    + _UUID
    + r"/"
    + _UUID
    + r"|projects/"
    + _UUID
    + r"/guide/"
    + _UUID
    + r")"
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
        "key_prefix",
        "object_key",
        "storage_identity",
    }
)


class AnnotationsStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "标注文件存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


def _reject(code="annotations_storage_identity_changed"):
    raise AnnotationsStorageError(code)


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


def valid_key(value):
    try:
        return (
            isinstance(value, str)
            and 0 < len(value) <= 1000
            and len(value.encode("utf-8")) <= 1024
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except UnicodeError:
        return False


@dataclass(frozen=True)
class SavedRef:
    location_id: uuid.UUID
    config_revision: int
    binding_generation: int
    endpoint_url: str
    bucket: str
    key_prefix: str
    object_key: str
    storage_identity: str

    @property
    def logical_key(self):
        return self.object_key[len(self.key_prefix) :]

    @property
    def ref(self):
        return {
            "schema": 2,
            "purpose": "annotations",
            "location_id": str(self.location_id),
            "config_revision": self.config_revision,
            "binding_generation": self.binding_generation,
            "endpoint_url": self.endpoint_url,
            "bucket": self.bucket,
            "key_prefix": self.key_prefix,
            "object_key": self.object_key,
            "storage_identity": self.storage_identity,
        }


def _logical(value):
    if not isinstance(value, str) or not _LOGICAL.fullmatch(value):
        _reject("annotations_storage_invalid_key")
    return value


def managed_prefix(base):
    from app.services.managed_s3_target import managed_prefix as shared

    return shared(base, purpose="annotations")


def resolve_ref(ref, expected_key=None):
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 2
        or ref["purpose"] != "annotations"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@%:")
        or not valid_key(ref["object_key"])
        or not isinstance(ref["key_prefix"], str)
    ):
        _reject("annotations_storage_invalid_receipt")
    try:
        identifier = uuid.UUID(ref["location_id"])
        if str(identifier) != ref["location_id"]:
            _reject()
        endpoint = _endpoint(ref["endpoint_url"])
        if endpoint != ref["endpoint_url"]:
            _reject()
    except (ValueError, TypeError, AttributeError):
        _reject("annotations_storage_invalid_receipt")
    prefix = ref["key_prefix"]
    if identifier == LEGACY_LOCATION_ID:
        if prefix or ref["config_revision"] != 1 or endpoint != _runtime_endpoint():
            _reject()
    else:
        if (
            not prefix.endswith(MANAGED_NAMESPACE)
            or managed_prefix(prefix[: -len(MANAGED_NAMESPACE)]) != prefix
            or not ref["object_key"].startswith(prefix)
            or ref["object_key"] == prefix
        ):
            _reject("annotations_storage_invalid_key")
    if not ref["object_key"].startswith(prefix):
        _reject("annotations_storage_invalid_key")
    logical = _logical(ref["object_key"][len(prefix) :])
    if expected_key is not None and logical != _logical(expected_key):
        _reject("annotations_storage_invalid_key")
    if ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    return SavedRef(
        identifier,
        ref["config_revision"],
        ref["binding_generation"],
        endpoint,
        ref["bucket"],
        prefix,
        ref["object_key"],
        ref["storage_identity"],
    )


def make_ref(target, binding_generation, object_key):
    return resolve_ref(
        {
            "schema": 2,
            "purpose": "annotations",
            "location_id": str(target.location_id),
            "config_revision": target.config_revision,
            "binding_generation": binding_generation,
            "endpoint_url": target.endpoint_url,
            "bucket": target.bucket,
            "key_prefix": target.key_prefix,
            "object_key": object_key,
            "storage_identity": managed_storage_identity(
                target.endpoint_url, target.bucket
            ),
        }
    ).ref


def legacy_ref(bucket, object_key):
    from app.services.managed_s3_target import Target

    return make_ref(
        Target(
            LEGACY_LOCATION_ID,
            1,
            None,
            _runtime_endpoint(),
            bucket,
            "",
            purpose="annotations",
            _legacy_client=storage_service.client,
        ),
        1,
        object_key,
    )


async def target_for(db, location, *, require_enabled=True):
    from app.services.managed_s3_target import target_for as shared

    return await shared(
        db, location, purpose="annotations", require_enabled=require_enabled
    )


async def target_for_ref(db, ref, *, require_enabled=True):
    saved = resolve_ref(ref)
    location = await db.get(StorageLocation, saved.location_id, populate_existing=True)
    target = await target_for(db, location, require_enabled=require_enabled)
    if saved.location_id == LEGACY_LOCATION_ID:
        target = replace(target, bucket=saved.bucket)
    if (target.endpoint_url, target.bucket, target.key_prefix) != (
        saved.endpoint_url,
        saved.bucket,
        saved.key_prefix,
    ):
        _reject()
    return target


async def allocate(db, logical_key):
    """Save an allocation in the caller transaction before any provider I/O."""
    logical = _logical(logical_key)
    connection = await db.connection()
    savepoint = await connection.begin_nested()
    try:
        await ensure_legacy_bindings(db)
        initial = await db.get(StorageBinding, "annotations", populate_existing=True)
        if initial is None:
            _reject("annotations_storage_binding_unproven")
        facts = initial.location_id, initial.bucket, initial.generation
        location = await db.get(StorageLocation, facts[0], populate_existing=True)
        if location is None:
            _reject("annotations_storage_binding_unproven")
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
            .where(StorageBinding.purpose == "annotations")
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if (
            location is None
            or location.connection_id != connection_id
            or binding is None
            or (binding.location_id, binding.bucket, binding.generation) != facts
        ):
            _reject("annotations_storage_binding_unproven")
        target = await target_for(db, location)
        if (
            binding.bucket != target.bucket
            or type(binding.generation) is not int
            or binding.generation < 1
            or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
        ):
            _reject("annotations_storage_binding_unproven")
        await storage_access.check(db, location.id, lock=True)
        result = make_ref(target, binding.generation, target.key_prefix + logical)
    except BaseException as exc:
        await savepoint.rollback()
        if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
            raise AnnotationsStorageError("annotations_storage_busy") from None
        raise
    else:
        await savepoint.commit()
        return result
