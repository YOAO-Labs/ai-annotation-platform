"""Saved import addresses; only allocation follows the current binding."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
import re
import uuid

from sqlalchemy import select

from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access, storage_presigning
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.export_storage import (
    _endpoint as _export_endpoint,
    _plain,
    _runtime_endpoint as _export_runtime_endpoint,
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
        "storage_identity",
    }
)
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_LOGICAL = re.compile(
    r"(?:prediction-inputs/"
    + _UUID
    + r"/|__aap_upload_staging__/"
    + _UUID
    + r"/|mask-formats/"
    + _UUID
    + r"/"
    + _UUID
    + r"/"
    + _UUID
    + r"/)"
)


class ImportStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "导入存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


def _reject(code="import_storage_identity_changed"):
    raise ImportStorageError(code)


def _endpoint(value):
    try:
        return _export_endpoint(value)
    except ValueError:
        _reject()


def _runtime_endpoint():
    try:
        return _export_runtime_endpoint()
    except ValueError:
        _reject()


def _logical(value):
    if not isinstance(value, str) or _LOGICAL.fullmatch(value) is None:
        _reject("import_storage_invalid_key")
    return value


@dataclass(frozen=True)
class Owner:
    bucket: str
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
            or any(ord(char) < 32 or ord(char) == 127 for char in relative)
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            _reject("import_storage_invalid_key")
        key = self.key_prefix + relative
        if len(key.encode("utf-8")) > 1024:
            _reject("import_storage_invalid_key")
        return key


def resolve_ref(ref, *, expected_prefix):
    logical = _logical(expected_prefix)
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] not in {1, 2}
        or ref["purpose"] != "import"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@%:")
        or not isinstance(ref["key_prefix"], str)
    ):
        _reject("import_storage_invalid_receipt")
    try:
        identifier = uuid.UUID(ref["location_id"])
        if str(identifier) != ref["location_id"]:
            _reject()
        endpoint = _endpoint(ref["endpoint_url"])
    except (ValueError, TypeError, AttributeError):
        _reject("import_storage_invalid_receipt")
    prefix = ref["key_prefix"]
    if ref["schema"] == 1:
        if (
            identifier != LEGACY_LOCATION_ID
            or ref["config_revision"] != 1
            or ref["binding_generation"] != 1
            or prefix != logical
        ):
            _reject()
    elif ref["binding_generation"] < 2:
        _reject()
    elif identifier == LEGACY_LOCATION_ID:
        if ref["config_revision"] != 1 or prefix != logical:
            _reject()
    else:
        from app.services.import_storage_target import MANAGED_NAMESPACE, managed_prefix

        if not prefix.endswith(logical):
            _reject("import_storage_invalid_key")
        root = prefix[: -len(logical)]
        if (
            not root.endswith(MANAGED_NAMESPACE)
            or managed_prefix(root[: -len(MANAGED_NAMESPACE)]) != root
        ):
            _reject("import_storage_invalid_key")
    if (
        len(prefix.encode("utf-8")) > 1024
        or (identifier == LEGACY_LOCATION_ID and endpoint != _runtime_endpoint())
        or ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"])
    ):
        _reject()
    return Owner(ref["bucket"], prefix, logical, deepcopy(ref))


def legacy_ref(logical_prefix, *, endpoint_url=None, bucket=None):
    endpoint = endpoint_url or _runtime_endpoint()
    bucket = bucket or storage_service.import_bucket
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "import",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": bucket,
            "key_prefix": _logical(logical_prefix),
            "storage_identity": managed_storage_identity(endpoint, bucket),
        },
        expected_prefix=logical_prefix,
    ).ref


async def allocate(db, logical_prefix):
    logical = _logical(logical_prefix)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("import_storage_binding_unproven")
    initial = await db.get(StorageBinding, "import", populate_existing=True)
    if initial is None:
        _reject("import_storage_binding_unproven")
    facts = initial.location_id, initial.bucket, initial.generation
    initial_location = await db.get(StorageLocation, facts[0], populate_existing=True)
    if initial_location is None:
        _reject("import_storage_binding_unproven")
    connection_id = initial_location.connection_id
    if connection_id is not None:
        await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == connection_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    location = await db.scalar(
        select(StorageLocation)
        .where(StorageLocation.id == facts[0])
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "import")
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.connection_id != connection_id
        or binding is None
        or (binding.location_id, binding.bucket, binding.generation) != facts
    ):
        _reject("import_storage_binding_unproven")
    from app.services.import_storage_target import target_for

    target = await target_for(db, location)
    if (
        type(binding.generation) is not int
        or binding.generation < 1
        or binding.bucket != target.bucket
        or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
    ):
        _reject("import_storage_binding_unproven")
    return resolve_ref(
        {
            "schema": 1 if binding.generation == 1 else 2,
            "purpose": "import",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": target.endpoint_url,
            "bucket": target.bucket,
            "key_prefix": target.key_prefix + logical,
            "storage_identity": managed_storage_identity(
                target.endpoint_url, target.bucket
            ),
        },
        expected_prefix=logical,
    ).ref


async def io_target(db, owner, *, lock=False):
    from app.services.import_storage_target import target_for

    query = select(StorageLocation).where(StorageLocation.id == owner.location_id)
    if lock:
        query = query.with_for_update(read=True)
    location = await db.scalar(query.execution_options(populate_existing=True))
    target = await target_for(db, location, require_enabled=False)
    if owner.location_id == LEGACY_LOCATION_ID:
        target = replace(target, bucket=owner.bucket)
    if (
        target.endpoint_url != owner.ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix + owner.logical_prefix != owner.key_prefix
    ):
        _reject()
    return target


@contextmanager
def client_context(target):
    with target.client() as client:
        yield client


async def check_location(db, owner, *, expected_revision=None, lock=False):
    await io_target(db, owner, lock=lock)
    return await storage_access.check(
        db, owner.location_id, expected_revision=expected_revision, lock=lock
    )


async def prepare_upload_url(db, owner, relative, content_type, *, expires_in=900):
    target = await io_target(db, owner)
    revision = await storage_access.check(db, owner.location_id)

    async def operation():
        async with await storage_access.acquire(
            db, owner.location_id, "import_upload_sign", expected_revision=revision
        ):

            def sign():
                with target.client() as client:
                    return storage_presigning.generate_url(
                        client,
                        "put_object",
                        params={
                            "Bucket": owner.bucket,
                            "Key": owner.key(relative),
                            "ContentType": content_type,
                        },
                        expires_in=expires_in,
                        audience="browser"
                        if owner.location_id == LEGACY_LOCATION_ID
                        else "internal",
                    )

            from app.services.prediction_inputs import settled_io

            return await settled_io(sign)

    result = await operation()
    await check_location(db, owner, expected_revision=revision)
    return result
