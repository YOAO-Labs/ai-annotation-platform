"""Saved dataset original addresses; allocation alone follows the binding."""

from dataclasses import dataclass, replace
import uuid

from app.db.models.dataset_asset import StorageLocation
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.export_storage import _endpoint as _parse_endpoint, _plain
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
        "key_prefix",
        "object_key",
        "storage_identity",
    }
)


class DatasetStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "数据集原件存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


def _reject(code="dataset_storage_identity_changed"):
    raise DatasetStorageError(code)


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
    def ref(self):
        return {
            "schema": 1,
            "purpose": "datasets",
            "location_id": str(self.location_id),
            "config_revision": self.config_revision,
            "binding_generation": self.binding_generation,
            "endpoint_url": self.endpoint_url,
            "bucket": self.bucket,
            "key_prefix": self.key_prefix,
            "object_key": self.object_key,
            "storage_identity": self.storage_identity,
        }


def resolve_ref(ref):
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "datasets"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@%:")
        or not valid_key(ref["object_key"])
        or not isinstance(ref["key_prefix"], str)
    ):
        _reject("dataset_storage_invalid_receipt")
    try:
        identifier = uuid.UUID(ref["location_id"])
        if str(identifier) != ref["location_id"]:
            _reject()
        endpoint = _endpoint(ref["endpoint_url"])
        if endpoint != ref["endpoint_url"]:
            _reject()
    except (ValueError, TypeError, AttributeError):
        _reject("dataset_storage_invalid_receipt")
    prefix = ref["key_prefix"]
    if identifier == LEGACY_LOCATION_ID:
        if prefix or ref["config_revision"] != 1 or endpoint != _runtime_endpoint():
            _reject()
    else:
        from app.services.dataset_storage_target import (
            MANAGED_NAMESPACE,
            managed_prefix,
        )

        if (
            not prefix.endswith(MANAGED_NAMESPACE)
            or managed_prefix(prefix[: -len(MANAGED_NAMESPACE)]) != prefix
            or not ref["object_key"].startswith(prefix)
            or ref["object_key"] == prefix
        ):
            _reject("dataset_storage_invalid_key")
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
            "schema": 1,
            "purpose": "datasets",
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
            purpose="datasets",
            _legacy_client=storage_service.client,
        ),
        1,
        object_key,
    )


async def target_for_ref(db, ref, *, require_enabled=True):
    from app.services.dataset_storage_target import target_for

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
