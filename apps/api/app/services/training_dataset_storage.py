"""Pinned physical ownership of one training version's retained namespace.

The current training purpose chooses new allocations only. An existing version
always resolves its saved locator through the same platform endpoint; credentials
remain owned by StorageService. Historical adoption is explicit and read-only
candidate discovery here does not bootstrap or claim content verification.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
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
_PREFIX = re.compile(
    r"projects/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/"
    r"datasets/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/"
)


def _reject(code="training_storage_identity_changed"):
    messages = {
        "training_storage_unpinned": "训练版本尚未固定存储归属，请先显式核验保留副本",
        "training_storage_invalid_key": "对象不属于该训练版本的固定前缀",
        "training_storage_binding_unproven": "训练存储绑定尚未登记或无法核实",
    }
    raise TrainingDatasetError(
        code,
        messages.get(code, "训练版本存储身份与当前端点不匹配"),
        status_code=409,
    )


def prefix_for(project_id, version_id) -> str:
    try:
        return f"projects/{uuid.UUID(str(project_id))}/datasets/{uuid.UUID(str(version_id))}/"
    except (TypeError, ValueError, AttributeError):
        _reject("training_storage_invalid_key")


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


def resolve_ref(ref, *, expected_prefix=None) -> Owner:
    if ref is None:
        _reject("training_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "training-dataset"
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] != 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] != 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["prefix"], str)
        or _PREFIX.fullmatch(ref["prefix"]) is None
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


def resolve(version) -> Owner:
    return resolve_ref(
        getattr(version, "storage_ref", None),
        expected_prefix=prefix_for(version.project_id, version.id),
    )


async def adoption_candidate(db, version) -> dict:
    """Read the persisted candidate; content adoption belongs to its verifier."""
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "training")
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.kind != "s3"
        or location.config_revision != 1
        or not location.enabled
        or location.config
        != {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
        or binding is None
        or binding.location_id != LEGACY_LOCATION_ID
        or binding.generation != 1
    ):
        _reject("training_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    candidate = {
        "schema": 1,
        "purpose": "training-dataset",
        "location_id": str(location.id),
        "config_revision": location.config_revision,
        "binding_generation": binding.generation,
        "endpoint_url": endpoint,
        "bucket": binding.bucket,
        "prefix": prefix_for(version.project_id, version.id),
        "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        "origin": "verified-retained",
        "evidence": None,
    }
    return resolve_ref(candidate).ref


async def allocate(db, version) -> dict:
    if getattr(version, "storage_ref", None) is not None:
        return resolve(version).ref
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("training_storage_binding_unproven")
    candidate = await adoption_candidate(db, version)
    if candidate["bucket"] != storage_service.training_bucket:
        _reject("training_storage_binding_unproven")
    candidate["origin"] = "new"
    version.storage_ref = candidate
    await db.flush()
    return resolve(version).ref
