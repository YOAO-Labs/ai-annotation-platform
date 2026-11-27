"""Saved media-cache roots and current credentials; only allocation follows Binding."""

import hashlib
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace

from botocore.exceptions import ClientError
from sqlalchemy.exc import DBAPIError

from app.db.models.thumbnail_output import media_cache_ref_check
from app.services import storage_access
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage import storage_service as storage_service
from app.services.storage_errors import MediaCacheStorageError
from app.services.storage_identity import LEGACY_LOCATION_ID, managed_storage_identity
from app.services.storage_identity import parse_endpoint as _parse_endpoint
from app.services.storage_identity import plain as _plain
from app.services.storage_registry_data import get_binding, get_connection, get_location

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
    from app.services import fixture_media_execution
    from app.services.managed_s3_target import target_origin_for

    execution = fixture_media_execution.current()
    if execution is not None:
        binding = await execution.target()
        try:
            location_id = uuid.UUID(binding["location_id"])
        except (ValueError, TypeError, AttributeError):
            _reject("media_cache_storage_binding_unproven")
        location = await get_location(db, location_id, lock="share_nowait")
        target = await target_origin_for(db, location, purpose="media-cache")
        if (
            target.endpoint_url != binding["endpoint"]
            or target.bucket != binding["bucket"]
            or target.key_prefix != MANAGED_NAMESPACE
            or managed_storage_identity(target.endpoint_url, target.bucket)
            != binding["storage_identity"]
        ):
            _reject("media_cache_storage_binding_unproven")
        await storage_access.check(db, location.id, lock=True)
        return resolve_ref(
            {
                "schema": 2,
                "purpose": "media-cache",
                "location_id": str(location.id),
                "config_revision": location.config_revision,
                "binding_generation": binding["binding_generation"],
                "endpoint_url": target.endpoint_url,
                "bucket": target.bucket,
                "key_prefix": target.key_prefix,
                "prefix": target.key_prefix + logical,
                "storage_identity": binding["storage_identity"],
            },
            expected_prefix=logical,
        ).ref
    connection = await db.connection()
    savepoint = await connection.begin_nested()
    try:
        await ensure_legacy_bindings(db)
        initial = await get_binding(db, "media-cache")
        if initial is None:
            _reject("media_cache_storage_binding_unproven")
        facts = initial.location_id, initial.bucket, initial.generation
        location = await get_location(db, facts[0])
        if location is None:
            _reject("media_cache_storage_binding_unproven")
        connection_id = location.connection_id
        if connection_id is not None:
            await get_connection(db, connection_id, lock="share_nowait")
        location = await get_location(db, facts[0], lock="share_nowait")
        binding = await get_binding(db, "media-cache", lock="share_nowait")
        if (
            location is None
            or location.connection_id != connection_id
            or binding is None
            or (binding.location_id, binding.bucket, binding.generation) != facts
        ):
            _reject("media_cache_storage_binding_unproven")
        target = await target_origin_for(db, location, purpose="media-cache")
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
    location = await get_location(db, owner.location_id)
    target = await target_for(db, location, require_enabled=require_enabled)
    if owner.location_id == LEGACY_LOCATION_ID:
        target = replace(target, bucket=owner.bucket)
    if (
        target.endpoint_url != ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix != owner.key_prefix
    ):
        _reject()
    from app.services import fixture_media_execution

    execution = fixture_media_execution.current()
    if execution is not None:
        binding = await execution.target()
        if str(owner.location_id) == binding["location_id"]:
            if (
                owner.bucket != binding["bucket"]
                or ref["endpoint_url"] != binding["endpoint"]
                or ref["storage_identity"] != binding["storage_identity"]
                or ref["binding_generation"] != binding["binding_generation"]
            ):
                _reject()
            from yoao_assets_media_runtime.fixture_native_outputs import load_target

            from app.services.saved_media_io import scoped_fixture_target

            control_url = fixture_media_execution._control_url().render_as_string(
                hide_password=False
            )
            writer = load_target(
                "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_TARGET_FILE",
                control_url,
                "aap_fixture_media_",
            )
            if any(
                writer[name] != binding[name]
                for name in (
                    "run_id",
                    "endpoint",
                    "bucket",
                    "storage_identity",
                    "receipt_sha256",
                )
            ):
                _reject()
            target = await scoped_fixture_target(db, target, writer)
    return target


def retire_fixture_versions(client, owner, versions, checkpoint):
    """Retire only native-recorded object versions at this exact owned root."""
    from app.services.content_snapshot import ContentError

    expected = {}
    for receipt in versions:
        if receipt["storage_ref"] != owner.ref:
            raise ContentError("fixture_media_owner_changed", 409)
        key, version = receipt["key"], receipt["version_id"]
        owner.contains(key)
        identity = (key, version if version is not None else "null")
        if identity in expected:
            raise ContentError("fixture_media_version_conflict", 409)
        expected[identity] = receipt

    found = set()
    marker = {}
    visited = set()
    while True:
        page = client.list_object_versions(
            Bucket=owner.bucket, Prefix=owner.prefix, **marker
        )
        if (
            page.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or type(page.get("IsTruncated")) is not bool
            or not isinstance(page.get("Versions", []), list)
            or not isinstance(page.get("DeleteMarkers", []), list)
        ):
            raise ContentError("fixture_media_version_unproven", 503)
        if page.get("DeleteMarkers"):
            raise ContentError("fixture_media_version_untracked", 409)
        for value in page.get("Versions", []):
            identity = (value.get("Key"), value.get("VersionId"))
            if identity in found or identity not in expected:
                raise ContentError("fixture_media_version_untracked", 409)
            if expected[identity]["state"] != "prepared":
                raise ContentError("fixture_media_version_changed", 409)
            found.add(identity)
        if not page["IsTruncated"]:
            break
        key_marker = page.get("NextKeyMarker")
        version_marker = page.get("NextVersionIdMarker")
        cursor = (key_marker, version_marker)
        if not isinstance(key_marker, str) or cursor in visited:
            raise ContentError("fixture_media_version_unproven", 503)
        visited.add(cursor)
        marker = {"KeyMarker": key_marker}
        if version_marker is not None:
            marker["VersionIdMarker"] = version_marker

    def absent(exc):
        return isinstance(exc, ClientError) and exc.response.get("Error", {}).get(
            "Code"
        ) in {
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
            "404",
        }

    for identity, receipt in expected.items():
        state = receipt["state"]
        params = {"Bucket": owner.bucket, "Key": receipt["key"]}
        if receipt["version_id"] is not None:
            params["VersionId"] = receipt["version_id"]
        if state == "prepared":
            if identity not in found:
                raise ContentError("fixture_media_version_unproven", 409)
            if receipt["version_id"] is None:
                versioning = client.get_bucket_versioning(Bucket=owner.bucket)
                if versioning.get("Status") is not None:
                    raise ContentError("fixture_media_version_unproven", 409)
            head = client.head_object(**params, IfMatch=receipt["etag"])
            if (
                head.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                or head.get("ContentRange") is not None
                or head.get("ETag") != receipt["etag"]
                or head.get("VersionId") != receipt["version_id"]
                or head.get("ContentLength") != receipt["size_bytes"]
                or head.get("DeleteMarker")
            ):
                raise ContentError("fixture_media_version_changed", 409)
            response = client.get_object(**params, IfMatch=receipt["etag"])
            body = response["Body"]
            digest, size = hashlib.sha256(), 0
            try:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > receipt["size_bytes"]:
                        raise ContentError("fixture_media_version_changed", 409)
                    digest.update(chunk)
            finally:
                storage_access.close_registered(body)
            if (
                response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                or response.get("ContentRange") is not None
                or response.get("ContentLength") != receipt["size_bytes"]
                or response.get("ETag") != receipt["etag"]
                or response.get("VersionId") != receipt["version_id"]
                or size != receipt["size_bytes"]
                or digest.hexdigest() != receipt["sha256"]
            ):
                raise ContentError("fixture_media_version_changed", 409)
            checkpoint(receipt["id"], "submitted")
            try:
                deleted = client.delete_object(**params, IfMatch=receipt["etag"])
                if (
                    deleted.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    not in (200, 204)
                    or deleted.get("DeleteMarker")
                    or deleted.get("VersionId", receipt["version_id"])
                    != receipt["version_id"]
                ):
                    raise ContentError("fixture_media_delete_unproven", 503)
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            checkpoint(receipt["id"], "deleted")
            state = "deleted"
        if state in {"deleted", "absent"}:
            try:
                client.head_object(**params)
            except ClientError as exc:
                if not absent(exc):
                    raise
            else:
                raise ContentError("fixture_media_delete_unproven", 503)
            if state == "deleted":
                checkpoint(receipt["id"], "absent")
        else:
            raise ContentError("fixture_media_version_unsettled", 409)
    return len(versions)
