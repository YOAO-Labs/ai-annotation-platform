"""Immutable physical ownership and bounded I/O for one bug attachment.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials come from the saved registered target; legacy references are never guessed.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import io
import time
import uuid
import re
import unicodedata
import warnings
from urllib.parse import urlsplit

from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.db.models.storage_connection import StorageConnection
from app.services import storage_access
from app.services.managed_s3_target import version_matches
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
MAX_BYTES = 10 * 1024**2
MAX_PIXELS = 40_000_000
VERIFY_SECONDS = 600
RETENTION_SECONDS = 180 * 86400
CHUNK_BYTES = 1024 * 1024
_UUID_PART = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(
    r"bug-report-attachments/" + _UUID_PART + r"/" + _UUID_PART + r"\.(?:png|jpg|webp)"
)
_MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_IMAGE_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
_SHA256 = re.compile(r"[0-9a-f]{64}")


_MESSAGES = {
    "bug_attachment_storage_identity_changed": "附件存储身份与当前端点不匹配",
    "bug_attachment_storage_unpinned": "附件尚未保存存储归属，请重新上传",
    "bug_attachment_storage_invalid_key": "附件对象键与上传记录不匹配",
    "bug_attachment_storage_invalid_receipt": "附件内容回执或保存期限不合法",
    "bug_attachment_storage_invalid_file_name": "附件文件名不合法",
    "bug_attachment_storage_invalid_image": "附件必须是完整的 PNG、JPEG 或 WebP 图片，最多 10 MiB 和 4000 万像素",
    "bug_attachment_storage_binding_unproven": "附件存储绑定尚未登记或无法核实",
    "bug_attachment_storage_integrity_mismatch": "附件字节与独立内容回执不一致",
    "bug_attachment_storage_missing": "已保存的附件对象不存在",
    "bug_attachment_storage_budget": "附件核验超过读取预算",
    "bug_attachment_storage_unavailable": "附件存储暂不可用",
}


class BugAttachmentStorageError(ValueError):
    def __init__(self, code, *, status_code=409):
        # Only fixed messages are exposed; provider exception text is never safe.
        super().__init__(_MESSAGES.get(code, "附件存储暂不可用"))
        self.code = code
        self.status_code = status_code


def _reject(code="bug_attachment_storage_identity_changed"):
    raise BugAttachmentStorageError(code)


def _canonical_uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("bug_attachment_storage_invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("bug_attachment_storage_invalid_key")
    if str(value) != result:
        _reject("bug_attachment_storage_invalid_key")
    return result


def key_for(owner_id, upload_id, mime_type) -> str:
    if not isinstance(mime_type, str) or mime_type not in _MIME_EXTENSIONS:
        _reject("bug_attachment_storage_invalid_key")
    return (
        f"bug-report-attachments/{_canonical_uuid(owner_id)}/{_canonical_uuid(upload_id)}"
        f".{_MIME_EXTENSIONS[mime_type]}"
    )


def safe_file_name(value) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 255
        or value in {".", ".."}
        or any(char in value for char in "/\\")
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise BugAttachmentStorageError(
            "bug_attachment_storage_invalid_file_name", status_code=422
        )
    return value


def validate_image(data, mime_type) -> None:
    """Verify original bytes without normalization or changing image metadata."""
    try:
        if (
            type(data) is not bytes
            or not 0 < len(data) <= MAX_BYTES
            or not isinstance(mime_type, str)
            or mime_type not in _IMAGE_FORMATS
        ):
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if (
                    image.format != _IMAGE_FORMATS[mime_type]
                    or min(width, height) <= 0
                    or width * height > MAX_PIXELS
                ):
                    raise ValueError
                image.verify()
            # JPEG verify() alone does not decode its scan data. A bounded decode
            # also rejects truncated streams without rewriting the original bytes.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (
        ValueError,
        OSError,
        SyntaxError,
        EOFError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise BugAttachmentStorageError(
            "bug_attachment_storage_invalid_image", status_code=422
        ) from None


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
    def endpoint_url(self):
        return self._ref["endpoint_url"]

    @property
    def ref(self):
        return deepcopy(self._ref)


def resolve_ref(ref, *, expected_key=None) -> Owner:
    if ref is None:
        _reject("bug_attachment_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] not in {1, 2}
        or ref["purpose"] != "bug-reports"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["object_key"], str)
        or not isinstance(ref["storage_identity"], str)
        or _SHA256.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    try:
        identifier = uuid.UUID(ref["location_id"])
        if str(identifier) != ref["location_id"]:
            _reject()
    except (ValueError, TypeError, AttributeError):
        _reject()
    key = ref["object_key"]
    matched = re.search(
        r"bug-report-attachments/"
        + _UUID_PART
        + r"/"
        + _UUID_PART
        + r"\.(?:png|jpg|webp)$",
        key,
    )
    if matched is None:
        _reject()
    logical = matched[0]
    if expected_key is not None and logical != expected_key:
        _reject()
    if ref["schema"] == 1:
        if (
            identifier != LEGACY_LOCATION_ID
            or ref["config_revision"] != 1
            or ref["binding_generation"] != 1
            or key != logical
        ):
            _reject()
    else:
        from app.services.bug_attachment_storage_target import (
            MANAGED_NAMESPACE,
            managed_prefix,
        )

        if ref["binding_generation"] < 2 or len(key.encode("utf-8")) > 1024:
            _reject()
        if identifier == LEGACY_LOCATION_ID:
            if ref["config_revision"] != 1 or key != logical:
                _reject()
        else:
            prefix = key[: -len(logical)]
            if not prefix.endswith(MANAGED_NAMESPACE):
                _reject()
            if managed_prefix(prefix[: -len(MANAGED_NAMESPACE)]) != prefix:
                _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if (identifier == LEGACY_LOCATION_ID and endpoint != _runtime_endpoint()) or ref[
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
        _reject("bug_attachment_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record) -> Owner:
    key = key_for(
        getattr(record, "owner_id", None),
        getattr(record, "id", None),
        getattr(record, "mime_type", None),
    )
    sha256 = getattr(record, "sha256", None)
    size_bytes = getattr(record, "size_bytes", None)
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= MAX_BYTES
    ):
        _reject("bug_attachment_storage_invalid_receipt")
    try:
        safe_file_name(getattr(record, "file_name", None))
    except BugAttachmentStorageError:
        _reject("bug_attachment_storage_invalid_receipt")
    created = _instant(getattr(record, "created_at", None))
    expires = _instant(getattr(record, "expires_at", None))
    if not created < expires <= created + timedelta(seconds=RETENTION_SECONDS):
        _reject("bug_attachment_storage_invalid_receipt")
    from app.services.bug_attachment_storage_target import version_id

    version_id({"VersionId": getattr(record, "object_version_id", None)})
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


async def allocate(db, record) -> dict:
    """Create-only allocation; the caller persists the independent upload row."""
    key = key_for(record.owner_id, record.id, record.mime_type)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("bug_attachment_storage_binding_unproven")
    initial = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "bug-reports")
        .execution_options(populate_existing=True)
    )
    if initial is None:
        _reject("bug_attachment_storage_binding_unproven")
    binding_facts = initial.location_id, initial.bucket, initial.generation
    initial_location = await db.get(
        StorageLocation, initial.location_id, populate_existing=True
    )
    if initial_location is None:
        _reject("bug_attachment_storage_binding_unproven")
    connection_id = getattr(initial_location, "connection_id", None)
    if connection_id is not None:
        await db.scalar(
            select(StorageConnection)
            .where(StorageConnection.id == connection_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    location = await db.scalar(
        select(StorageLocation)
        .where(StorageLocation.id == binding_facts[0])
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "bug-reports")
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        location is None
        or location.id != binding_facts[0]
        or getattr(location, "connection_id", None) != connection_id
        or binding is None
        or (binding.location_id, binding.bucket, binding.generation) != binding_facts
    ):
        _reject("bug_attachment_storage_binding_unproven")
    from app.services.bug_attachment_storage_target import target_for

    target = await target_for(db, location)
    if (
        type(binding.generation) is not int
        or binding.generation < 1
        or binding.bucket != target.bucket
        or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
    ):
        _reject("bug_attachment_storage_binding_unproven")
    endpoint = target.endpoint_url
    return resolve_ref(
        {
            "schema": 1 if binding.generation == 1 else 2,
            "purpose": "bug-reports",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "object_key": target.key_prefix + key,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_key=key,
    ).ref


async def io_target(db, record):
    """Capture current credentials while keeping the saved physical address fixed."""
    owner = resolve(record)
    if owner.ref["schema"] == 1:
        return None
    from app.services.bug_attachment_storage_target import target_for

    identifier = uuid.UUID(owner.ref["location_id"])
    location = await db.get(StorageLocation, identifier, populate_existing=True)
    target = await target_for(db, location)
    if (
        target.endpoint_url != owner.endpoint_url
        or target.bucket != owner.bucket
        or target.key_prefix + key_for(record.owner_id, record.id, record.mime_type)
        != owner.object_key
    ):
        _reject()
    return target


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("bug_attachment_storage_budget")


def _unavailable(exc):
    error = BugAttachmentStorageError(
        "bug_attachment_storage_unavailable", status_code=503
    )
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _client_context(record, target, *, owned=False):
    owner = resolve(record)
    ref = owner.ref
    if target is None:
        if ref["location_id"] != str(LEGACY_LOCATION_ID):
            _reject("bug_attachment_storage_binding_unproven")
        if owned:
            from app.services.managed_s3_target import Target

            return Target(
                LEGACY_LOCATION_ID,
                ref["config_revision"],
                None,
                owner.endpoint_url,
                owner.bucket,
                "",
                purpose="bug-reports",
                _legacy_client=storage_service.client,
            ).client(owned=True)
        return nullcontext(storage_service.client)
    if (
        str(target.location_id) != ref["location_id"]
        or target.endpoint_url != owner.endpoint_url
        or target.bucket != owner.bucket
        or target.key_prefix + key_for(record.owner_id, record.id, record.mime_type)
        != owner.object_key
    ):
        _reject()
    return target.client(**({"owned": True} if owned else {}))


def read(record, *, missing_ok=False, target=None) -> bytes | None:
    with _client_context(record, target) as client:
        return _read(client, record, missing_ok=missing_ok)


def _read(client, record, *, missing_ok=False) -> bytes | None:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(record)
    deadline = time.monotonic() + VERIFY_SECONDS
    from app.services.bug_attachment_storage_target import object_params

    version = getattr(record, "object_version_id", None)
    params = object_params(owner.bucket, owner.object_key, version)
    try:
        head = client.head_object(**params)
        if version is not None and not version_matches(version, head.get("VersionId")):
            _reject("bug_attachment_storage_integrity_mismatch")
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != record.size_bytes
            or not isinstance(etag, str)
            or not 0 < len(etag) <= 1024
        ):
            _reject("bug_attachment_storage_integrity_mismatch")
        response = client.get_object(**params, IfMatch=etag)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                (
                    version is not None
                    and not version_matches(version, response.get("VersionId"))
                )
                or response.get("ETag") != etag
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != record.size_bytes
            ):
                _reject("bug_attachment_storage_integrity_mismatch")
            checksum, size, chunks = hashlib.sha256(), 0, []
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, record.size_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > record.size_bytes:
                    _reject("bug_attachment_storage_integrity_mismatch")
                checksum.update(chunk)
                chunks.append(chunk)
            if size != record.size_bytes or checksum.hexdigest() != record.sha256:
                _reject("bug_attachment_storage_integrity_mismatch")
        _budget(deadline)
        after = client.head_object(**params)
        if (
            (
                version is not None
                and not version_matches(version, after.get("VersionId"))
            )
            or after.get("ETag") != etag
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != record.size_bytes
        ):
            _reject("bug_attachment_storage_integrity_mismatch")
        _budget(deadline)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            if missing_ok:
                return None
            raise BugAttachmentStorageError("bug_attachment_storage_missing") from None
        if code in {"412", "PreconditionFailed"}:
            raise BugAttachmentStorageError(
                "bug_attachment_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except BugAttachmentStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
    return b"".join(chunks)


def write(record, data, *, target=None, accounting=None) -> str | None:
    """Conditionally create the saved physical key and return the provider receipt."""
    from app.services.managed_s3_target import _conditional_put
    from app.services.bug_attachment_storage_target import version_id, object_params

    owner = resolve(record)
    if (
        type(data) is not bytes
        or len(data) != record.size_bytes
        or hashlib.sha256(data).hexdigest() != record.sha256
    ):
        _reject("bug_attachment_storage_integrity_mismatch")
    validate_image(data, record.mime_type)
    try:
        with _client_context(record, target, owned=True) as client:
            from app.services.bug_attachment_accounting import Observation

            observation = (
                Observation(
                    accounting,
                    client,
                    owner.object_key,
                    {
                        "sha256": record.sha256,
                        "size_bytes": record.size_bytes,
                    },
                )
                if accounting is not None
                else None
            )

            def submit():
                return _conditional_put(
                    client,
                    {
                        **object_params(owner.bucket, owner.object_key),
                        "ContentType": record.mime_type,
                    },
                    data,
                )

            response = submit() if observation is None else observation.submit(submit)
            receipt = SimpleNamespace(
                **{
                    name: getattr(record, name)
                    for name in (
                        "id",
                        "owner_id",
                        "mime_type",
                        "file_name",
                        "created_at",
                        "expires_at",
                        "storage_ref",
                        "sha256",
                        "size_bytes",
                    )
                },
                object_version_id=version_id(response),
            )
            _read(client, receipt)
            if observation is not None:
                observation.verified(receipt.object_version_id)
            return receipt.object_version_id
    except BugAttachmentStorageError:
        raise
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in {
            "412",
            "PreconditionFailed",
            "ConditionalRequestConflict",
            "409",
        }:
            raise BugAttachmentStorageError(
                "bug_attachment_storage_integrity_mismatch"
            ) from None
        raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None


def delete(record, *, target=None, accounting=None) -> None:
    """Delete only this committed retirement's saved physical object/version."""
    from app.services.bug_attachment_storage_target import object_params

    owner = resolve(record)
    version = getattr(record, "object_version_id", None)
    try:
        if (
            target is None
            and version is None
            and owner.ref["location_id"] == str(LEGACY_LOCATION_ID)
        ):
            if accounting is None:
                storage_service.delete_object(owner.object_key, bucket=owner.bucket)
            else:
                from app.services.object_removal_accounting import Observation

                with _client_context(record, target, owned=True) as client:
                    observation = Observation(
                        accounting, client, owner.object_key, version
                    )
                    response = observation.submit(
                        lambda: client.delete_object(
                            Bucket=owner.bucket, Key=owner.object_key
                        )
                    )
                    observation.complete(response)
            return
        with _client_context(record, target, owned=accounting is not None) as client:
            # A null version is a mutable slot, so verify its saved bytes first.
            if (
                version in (None, "null")
                and _read(client, record, missing_ok=True) is None
            ):
                return
            params = object_params(owner.bucket, owner.object_key, version)
            if accounting is None:
                client.delete_object(**params)
            else:
                from app.services.object_removal_accounting import Observation

                observation = Observation(accounting, client, owner.object_key, version)
                response = observation.submit(lambda: client.delete_object(**params))
                observation.complete(response)
    except BugAttachmentStorageError:
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
