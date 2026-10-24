"""Saved UUID owners and fully verified image-pyramid output I/O.

Only allocation consults the media-cache binding. The caller owns durable Gate
permits, lease checks and publication; these synchronous helpers never commit.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

CHUNK_BYTES = 1024 * 1024
MANIFEST_MAX_BYTES = 1024 * 1024
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_PREFIX = re.compile(r"image-pyramids/objects/" + _UUID + "/")
_SHA = re.compile(r"[0-9a-f]{64}")
_TILE = re.compile(r"tiles/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)/(?:0|[1-9][0-9]*)\.webp")
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
    }
)


class ImagePyramidStorageError(ValueError):
    def __init__(self, code="identity_changed", status_code=None):
        suffix = code.removeprefix("image_pyramid_storage_")
        super().__init__("图片金字塔存储归属或完整性核验失败")
        self.code = "image_pyramid_storage_" + suffix
        self.status_code = status_code or {"missing": 404, "unavailable": 503}.get(
            suffix, 409
        )


def _reject(code="identity_changed"):
    raise ImagePyramidStorageError(code)


def _uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("invalid_key")
    if str(value) != result:
        _reject("invalid_key")
    return result


def prefix_for(identifier):
    return f"image-pyramids/objects/{_uuid(identifier)}/"


def _plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and all(32 < ord(char) < 127 for char in value)
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
        getattr(storage_service.client, "meta", None), "endpoint_url", None
    )
    if not isinstance(actual, str) or actual.rstrip("/") != expected:
        _reject()
    return _endpoint(expected)


def _safe_key(value):
    return (
        _plain(value, 1024)
        and not any(char in value for char in "\\?#%")
        and not value.startswith("/")
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
            _reject("invalid_key")
        return True

    def key(self, relative):
        if not _safe_key(relative):
            _reject("invalid_key")
        key = self.prefix + relative
        self.contains(key)
        return key


def resolve_ref(ref, *, expected_prefix=None):
    if ref is None:
        _reject("unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "media-cache"
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
        or not isinstance(ref["storage_identity"], str)
        or _SHA.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref[
        "storage_identity"
    ] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    return Owner(ref["bucket"], ref["prefix"], deepcopy(ref))


def resolve(record):
    return resolve_ref(
        getattr(record, "storage_ref", None),
        expected_prefix=prefix_for(getattr(record, "id", None)),
    )


async def allocate(db, output_id):
    """Read an already registered compatible binding for a newly allocated UUID."""
    prefix = prefix_for(output_id)
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "media-cache")
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
        or binding.purpose != "media-cache"
        or binding.location_id != LEGACY_LOCATION_ID
        or type(binding.generation) is not int
        or binding.generation != 1
        or binding.bucket != storage_service.media_cache_bucket
    ):
        _reject("binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "media-cache",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "prefix": prefix,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_prefix=prefix,
    ).ref


def _client(storage, owner):
    endpoint = getattr(getattr(storage.client, "meta", None), "endpoint_url", None)
    if (
        not isinstance(endpoint, str)
        or endpoint.rstrip("/") != owner.ref["endpoint_url"]
    ):
        _reject()
    return storage.client


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("budget")


def _receipt(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"sha256", "size_bytes"}
        or not isinstance(value["sha256"], str)
        or _SHA.fullmatch(value["sha256"]) is None
        or type(value["size_bytes"]) is not int
        or not 0 < value["size_bytes"] <= settings.image_pyramid_max_derived_bytes
    ):
        _reject("invalid_receipt")
    return value


def _inventory(output, owner):
    value = getattr(output, "inventory", None)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "objects"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["objects"], dict)
        or not 3 <= len(value["objects"]) <= settings.image_pyramid_max_tiles + 2
    ):
        _reject("invalid_receipt")
    if not {owner.key("manifest.json"), owner.key("overview.webp")} <= set(
        value["objects"]
    ):
        _reject("invalid_receipt")
    total = 0
    for key, receipt in value["objects"].items():
        owner.contains(key)
        relative = key[len(owner.prefix) :]
        if (
            relative not in {"manifest.json", "overview.webp"}
            and _TILE.fullmatch(relative) is None
        ):
            _reject("invalid_receipt")
        total += _receipt(receipt)["size_bytes"]
    if total > settings.image_pyramid_max_derived_bytes:
        _reject("budget")
    return value["objects"]


def _version(metadata):
    value = metadata.get("VersionId")
    if value in (None, "null"):
        return None
    if not _plain(value, 1024):
        _reject("integrity_mismatch")
    return value


def _read(client, owner, key, receipt, deadline, *, collect=False):
    owner.contains(key)
    _receipt(receipt)
    _budget(deadline)
    params = {"Bucket": owner.bucket, "Key": key}
    head = client.head_object(**params)
    etag, version = head.get("ETag"), _version(head)
    if (
        not _plain(etag, 1024)
        or type(head.get("ContentLength")) is not int
        or head["ContentLength"] != receipt["size_bytes"]
    ):
        _reject("integrity_mismatch")
    condition = {"IfMatch": etag}
    if version is not None:
        condition["VersionId"] = version
    response = client.get_object(**params, **condition)
    with storage_access.closing_registered(response["Body"]) as body:
        if (
            response.get("ETag") != etag
            or _version(response) != version
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != receipt["size_bytes"]
        ):
            _reject("integrity_mismatch")
        checksum, size, chunks = hashlib.sha256(), 0, []
        while True:
            _budget(deadline)
            chunk = body.read(min(CHUNK_BYTES, receipt["size_bytes"] - size + 1))
            if type(chunk) is not bytes:
                _reject("integrity_mismatch")
            if not chunk:
                break
            size += len(chunk)
            if size > receipt["size_bytes"]:
                _reject("integrity_mismatch")
            checksum.update(chunk)
            if collect:
                chunks.append(chunk)
        if size != receipt["size_bytes"] or checksum.hexdigest() != receipt["sha256"]:
            _reject("integrity_mismatch")
    _budget(deadline)
    after = client.head_object(**params)
    if (
        after.get("ETag") != etag
        or _version(after) != version
        or type(after.get("ContentLength")) is not int
        or after["ContentLength"] != receipt["size_bytes"]
    ):
        _reject("integrity_mismatch")
    _budget(deadline)
    return b"".join(chunks) if collect else deepcopy(receipt)


def _objects(client, owner, deadline):
    result, token = {}, None
    while True:
        _budget(deadline)
        params = {"Bucket": owner.bucket, "Prefix": owner.prefix, "MaxKeys": 1000}
        if token is not None:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        for row in response.get("Contents", []):
            key, size = row.get("Key"), row.get("Size")
            owner.contains(key)
            if key in result or type(size) is not int or size < 0:
                _reject("integrity_mismatch")
            result[key] = size
            if len(result) > settings.image_pyramid_max_tiles + 2:
                _reject("budget")
        if not response.get("IsTruncated"):
            return result
        next_token = response.get("NextContinuationToken")
        if not _plain(next_token, 8192) or next_token == token:
            _reject("integrity_mismatch")
        token = next_token


def _translate(exc):
    if isinstance(exc, ImagePyramidStorageError) and not getattr(
        exc, "storage_operation_unsettled", False
    ):
        return exc
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    suffix = (
        "missing"
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}
        else "integrity_mismatch"
        if code in {"412", "PreconditionFailed", "409", "ConditionalRequestConflict"}
        else "unavailable"
    )
    result = ImagePyramidStorageError(suffix)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(result)
    return result


def upload_and_verify(storage, output, tiles, overview_path, manifest_bytes, deadline):
    """Create an independent prefix, then fully read back every exact receipt."""
    owner = resolve(output)
    client = _client(storage, owner)
    if (
        not isinstance(tiles, list)
        or not 0 < len(tiles) <= settings.image_pyramid_max_tiles
        or type(manifest_bytes) is not bytes
        or not 0 < len(manifest_bytes) <= MANIFEST_MAX_BYTES
    ):
        _reject("invalid_receipt")
    paths = {}
    for path, level, x, y in tiles:
        if any(type(value) is not int or value < 0 for value in (level, x, y)):
            _reject("invalid_key")
        key = owner.key(f"tiles/{level}/{x}/{y}.webp")
        if key in paths:
            _reject("invalid_key")
        paths[key] = Path(path)
    overview = owner.key("overview.webp")
    manifest = owner.key("manifest.json")
    paths[overview] = Path(overview_path)
    inventory = {}
    try:
        if _objects(client, owner, deadline):
            _reject("integrity_mismatch")
        total = len(manifest_bytes)
        for key, path in paths.items():
            checksum, size = hashlib.sha256(), 0
            with storage_access.closing_registered(path.open("rb")) as stream:
                while chunk := stream.read(CHUNK_BYTES):
                    _budget(deadline)
                    size += len(chunk)
                    if size + total > settings.image_pyramid_max_derived_bytes:
                        _reject("budget")
                    checksum.update(chunk)
                if size <= 0:
                    _reject("invalid_receipt")
                stream.seek(0)
                client.put_object(
                    Bucket=owner.bucket,
                    Key=key,
                    Body=stream,
                    ContentLength=size,
                    ContentType="image/webp",
                    CacheControl="private, max-age=31536000, immutable",
                    IfNoneMatch="*",
                )
            total += size
            inventory[key] = {"sha256": checksum.hexdigest(), "size_bytes": size}
            _budget(deadline)
        client.put_object(
            Bucket=owner.bucket,
            Key=manifest,
            Body=manifest_bytes,
            ContentLength=len(manifest_bytes),
            ContentType="application/json",
            CacheControl="private, max-age=31536000, immutable",
            IfNoneMatch="*",
        )
        inventory[manifest] = {
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "size_bytes": len(manifest_bytes),
        }
        expected = {key: receipt["size_bytes"] for key, receipt in inventory.items()}
        if _objects(client, owner, deadline) != expected:
            _reject("integrity_mismatch")
        for key, receipt in inventory.items():
            _read(client, owner, key, receipt, deadline)
        if _objects(client, owner, deadline) != expected:
            _reject("integrity_mismatch")
        return manifest, overview, total, {"schema": 1, "objects": inventory}
    except Exception as exc:
        raise _translate(exc) from None


def verify_object(storage, output, key):
    owner = resolve(output)
    client = _client(storage, owner)
    owner.contains(key)
    receipt = _inventory(output, owner).get(key)
    if receipt is None:
        _reject("invalid_key")
    try:
        return _read(
            client,
            owner,
            key,
            receipt,
            time.monotonic() + settings.image_pyramid_job_timeout_seconds,
        )
    except Exception as exc:
        raise _translate(exc) from None


def read_manifest(storage, output, expected_digest):
    owner = resolve(output)
    client = _client(storage, owner)
    key = owner.key("manifest.json")
    receipt = _inventory(output, owner).get(key)
    if (
        receipt is None
        or receipt["size_bytes"] > MANIFEST_MAX_BYTES
        or expected_digest != "sha256:" + receipt["sha256"]
    ):
        _reject("invalid_receipt")
    try:
        data = _read(
            client,
            owner,
            key,
            receipt,
            time.monotonic() + settings.image_pyramid_job_timeout_seconds,
            collect=True,
        )
        result = json.loads(data)
        if not isinstance(result, dict):
            _reject("invalid_receipt")
        return result
    except Exception as exc:
        raise _translate(exc) from None


def delete(storage, output):
    """Delete only this saved UUID prefix and prove it is empty afterwards."""
    owner = resolve(output)
    client = _client(storage, owner)
    deadline = time.monotonic() + settings.image_pyramid_job_timeout_seconds
    try:
        objects = _objects(client, owner, deadline)
        for key in objects:
            _budget(deadline)
            client.delete_object(Bucket=owner.bucket, Key=key)
        if _objects(client, owner, deadline):
            _reject("integrity_mismatch")
        return len(objects)
    except Exception as exc:
        raise _translate(exc) from None
