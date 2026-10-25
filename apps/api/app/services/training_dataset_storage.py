"""Pinned physical ownership of one training version's retained namespace.

The current training purpose chooses new allocations only. An existing version
always resolves its saved locator; current credentials are captured separately.
Historical adoption is explicit and read-only
candidate discovery here does not bootstrap or claim content verification.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.reference_keys import is_reference
from app.services.storage import storage_service
from app.services.training_dataset_bridge import TrainingDatasetError

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
        "origin",
        "evidence",
    }
)
MANAGED_NAMESPACE = "_aap_managed/training/"
MAX_MANAGED_PREFIX_CHARS = 266
MAX_MANAGED_PREFIX_BYTES = 707
_NAMESPACES = {
    "training-dataset": "datasets",
    "training-run": "runs",
    "training-preview": "previews",
    "sample-evaluation": "sample-evaluations",
    "active-learning": "active-learning",
}
_PREFIXES = {
    purpose: re.compile(
        r"projects/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/"
        + namespace
        + r"/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/"
    )
    for purpose, namespace in _NAMESPACES.items()
}


def _reject(code="training_storage_identity_changed"):
    messages = {
        "training_storage_unpinned": "训练对象尚未固定存储归属，请先显式核验保留副本",
        "training_storage_invalid_key": "对象不属于该训练对象的固定前缀",
        "training_storage_binding_unproven": "训练存储绑定尚未登记或无法核实",
    }
    raise TrainingDatasetError(
        code,
        messages.get(code, "训练对象存储身份与当前端点不匹配"),
        status_code=409,
    )


def _prefix_for(project_id, identifier, *, purpose) -> str:
    try:
        namespace = _NAMESPACES[purpose]
        return f"projects/{uuid.UUID(str(project_id))}/{namespace}/{uuid.UUID(str(identifier))}/"
    except (KeyError, TypeError, ValueError, AttributeError):
        _reject("training_storage_invalid_key")


def prefix_for(project_id, version_id) -> str:
    return _prefix_for(project_id, version_id, purpose="training-dataset")


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


def _safe_key(value):
    if not isinstance(value, str) or not value or is_reference(value):
        return False
    try:
        if len(value.encode("utf-8")) > 1024:
            return False
    except UnicodeError:
        return False
    return (
        "\\" not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


@dataclass(frozen=True)
class Owner:
    bucket: str
    prefix: str
    _ref: dict = field(repr=False)

    @property
    def ref(self):
        return deepcopy(self._ref)

    @property
    def location_id(self):
        return uuid.UUID(self._ref["location_id"])

    @property
    def key_prefix(self):
        return self._ref.get("key_prefix", "")

    @property
    def logical_prefix(self):
        return self.prefix[len(self.key_prefix) :]

    def contains(self, key):
        if (
            not _safe_key(key)
            or not key.startswith(self.prefix)
            or len(key) <= len(self.prefix)
        ):
            _reject("training_storage_invalid_key")
        return True

    def key(self, relative):
        if not _safe_key(relative):
            _reject("training_storage_invalid_key")
        result = self.prefix + relative
        self.contains(result)
        return result


def _resolve_ref(ref, *, purpose, expected_prefix=None) -> Owner:
    if ref is None:
        _reject("training_storage_unpinned")
    if isinstance(ref, dict) and type(ref.get("schema")) is int and ref["schema"] == 2:
        return _resolve_new_ref(ref, purpose=purpose, expected_prefix=expected_prefix)
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != purpose
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] != 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] != 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["prefix"], str)
        or _PREFIXES[purpose].fullmatch(ref["prefix"]) is None
        or (expected_prefix is not None and ref["prefix"] != expected_prefix)
        or not isinstance(ref["origin"], str)
        or ref["origin"] not in {"new", "verified-retained"}
        or (ref["evidence"] is not None and not isinstance(ref["evidence"], dict))
        or (ref["origin"] == "new" and ref["evidence"] is not None)
    ):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref[
        "storage_identity"
    ] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    try:
        # Reject non-JSON/non-finite evidence instead of returning mutable data.
        value = json.loads(json.dumps(ref, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject()
    return Owner(value["bucket"], value["prefix"], value)


def managed_prefix(base):
    from app.services.managed_s3_target import managed_prefix as shared

    return shared(base, purpose="training")


def _resolve_new_ref(ref, *, purpose, expected_prefix=None):
    if (
        set(ref) != _FIELDS | {"key_prefix"}
        or ref["purpose"] != purpose
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@%:")
        or not isinstance(ref["prefix"], str)
        or not isinstance(ref["key_prefix"], str)
        or (expected_prefix is not None and ref["prefix"] != expected_prefix)
        or ref["origin"] != "new"
        or ref["evidence"] is not None
    ):
        _reject()
    try:
        identifier = uuid.UUID(ref["location_id"])
        if str(identifier) != ref["location_id"]:
            _reject()
    except (ValueError, TypeError, AttributeError):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    root = ref["key_prefix"]
    if identifier == LEGACY_LOCATION_ID:
        if root or ref["config_revision"] != 1 or endpoint != _runtime_endpoint():
            _reject()
    elif (
        not root.endswith(MANAGED_NAMESPACE)
        or managed_prefix(root[: -len(MANAGED_NAMESPACE)]) != root
        or ref["binding_generation"] < 2
    ):
        _reject()
    if (
        not ref["prefix"].startswith(root)
        or _PREFIXES[purpose].fullmatch(ref["prefix"][len(root) :]) is None
        or not _safe_key(ref["prefix"].rstrip("/"))
        or ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"])
    ):
        _reject()
    return Owner(ref["bucket"], ref["prefix"], deepcopy(ref))


def make_ref(target, generation, logical_prefix, *, purpose="training-dataset"):
    if (
        not isinstance(logical_prefix, str)
        or _PREFIXES[purpose].fullmatch(logical_prefix) is None
    ):
        _reject("training_storage_invalid_key")
    return _resolve_ref(
        {
            "schema": 2,
            "purpose": purpose,
            "location_id": str(target.location_id),
            "config_revision": target.config_revision,
            "binding_generation": generation,
            "endpoint_url": target.endpoint_url,
            "bucket": target.bucket,
            "prefix": target.key_prefix + logical_prefix,
            "storage_identity": managed_storage_identity(
                target.endpoint_url, target.bucket
            ),
            "origin": "new",
            "evidence": None,
            "key_prefix": target.key_prefix,
        },
        purpose=purpose,
    ).ref


async def target_for(db, location, *, require_enabled=True):
    from app.services.managed_s3_target import target_for as shared

    return await shared(
        db, location, purpose="training", require_enabled=require_enabled
    )


async def target_for_ref(db, ref, *, require_enabled=True):
    purpose = ref.get("purpose") if isinstance(ref, dict) else None
    if purpose not in _NAMESPACES:
        _reject()
    owner = _resolve_ref(ref, purpose=purpose)
    location = await db.get(StorageLocation, owner.location_id, populate_existing=True)
    target = await target_for(db, location, require_enabled=require_enabled)
    if owner.location_id == LEGACY_LOCATION_ID:
        target = replace(target, bucket=owner.bucket)
    if (target.endpoint_url, target.bucket, target.key_prefix) != (
        ref["endpoint_url"],
        owner.bucket,
        owner.key_prefix,
    ):
        _reject()
    return target


def resolve_ref(ref, *, expected_prefix=None) -> Owner:
    return _resolve_ref(
        ref, purpose="training-dataset", expected_prefix=expected_prefix
    )


def _resolve(record, *, purpose) -> Owner:
    owner = _resolve_ref(
        getattr(record, "storage_ref", None),
        purpose=purpose,
    )
    if owner.logical_prefix != _prefix_for(
        record.project_id, record.id, purpose=purpose
    ):
        _reject("training_storage_invalid_key")
    return owner


def resolve(version) -> Owner:
    return _resolve(version, purpose="training-dataset")


async def _adoption_candidate(db, record, *, purpose) -> dict:
    """Read the persisted candidate; content adoption belongs to its verifier."""
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    if (
        location is None
        or location.kind != "s3"
        or location.config_revision != 1
        or not location.enabled
        or location.config
        != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    ):
        _reject("training_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    candidate = {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(location.id),
        "config_revision": location.config_revision,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": storage_service.training_bucket,
        "prefix": _prefix_for(record.project_id, record.id, purpose=purpose),
        "storage_identity": managed_storage_identity(
            endpoint, storage_service.training_bucket
        ),
        "origin": "verified-retained",
        "evidence": None,
    }
    return _resolve_ref(candidate, purpose=purpose).ref


async def adoption_candidate(db, version) -> dict:
    """Read the persisted candidate; content adoption belongs to its verifier."""
    return await _adoption_candidate(db, version, purpose="training-dataset")


async def _allocate(db, record, *, purpose) -> dict:
    if getattr(record, "storage_ref", None) is not None:
        return _resolve(record, purpose=purpose).ref
    connection = await db.connection()
    savepoint = await connection.begin_nested()
    try:
        await ensure_legacy_bindings(db)
        initial = await db.get(StorageBinding, "training", populate_existing=True)
        if initial is None:
            _reject("training_storage_binding_unproven")
        facts = initial.location_id, initial.bucket, initial.generation
        location = await db.get(StorageLocation, facts[0], populate_existing=True)
        if location is None:
            _reject("training_storage_binding_unproven")
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
            .where(StorageBinding.purpose == "training")
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if (
            location is None
            or location.connection_id != connection_id
            or binding is None
            or (binding.location_id, binding.bucket, binding.generation) != facts
        ):
            _reject("training_storage_binding_unproven")
        target = await target_for(db, location)
        if (
            binding.bucket != target.bucket
            or type(binding.generation) is not int
            or binding.generation < 1
            or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
        ):
            _reject("training_storage_binding_unproven")
        await storage_access.check(db, location.id, lock=True)
        candidate = make_ref(
            target,
            binding.generation,
            _prefix_for(record.project_id, record.id, purpose=purpose),
            purpose=purpose,
        )
    except BaseException as exc:
        await savepoint.rollback()
        if isinstance(exc, DBAPIError) and storage_access.unavailable(exc):
            _reject("training_storage_busy")
        if isinstance(exc, ValueError) and not isinstance(exc, TrainingDatasetError):
            _reject("training_storage_binding_unproven")
        raise
    else:
        await savepoint.commit()
    record.storage_ref = candidate
    await db.flush()
    return _resolve(record, purpose=purpose).ref


async def allocate(db, version) -> dict:
    return await _allocate(db, version, purpose="training-dataset")
