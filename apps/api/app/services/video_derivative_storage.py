"""Saved UUID owners and fully verified video-derivative output I/O.

Only allocation consults the media-cache binding. The caller owns durable Gate
permits, lease checks and publication; these synchronous helpers never commit.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sys
import stat
from urllib.parse import urlsplit
import uuid
from types import SimpleNamespace

from botocore.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import media_cache_accounting
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_presigning
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import StorageService, storage_service

CHUNK_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024**3
MAX_RESULT_BYTES = 64 * 1024**2
ALLOWED_RELATIVE_KEYS = frozenset(
    {
        "metadata.json",
        "poster.webp",
        "playback.mp4",
        "frame.webp",
        "frame.jpeg",
        "chunk.mp4",
    }
)
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_PREFIX = re.compile(r"video-derivatives/objects/" + _UUID + "/")
_SHA = re.compile(r"[0-9a-f]{64}")
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


class VideoDerivativeStorageError(ValueError):
    def __init__(self, code="identity_changed", status_code=None):
        suffix = code.removeprefix("video_derivative_storage_")
        super().__init__("视频派生媒体存储归属或完整性核验失败")
        self.code = "video_derivative_storage_" + suffix
        self.status_code = status_code or {"missing": 404, "unavailable": 503}.get(
            suffix, 409
        )


def _reject(code="identity_changed"):
    raise VideoDerivativeStorageError(code)


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
    return f"video-derivatives/objects/{_uuid(identifier)}/"


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
            (
                not _safe_key(key)
                if self._ref["schema"] == 1
                else not isinstance(key, str)
                or len(key) > 1000
                or len(key.encode("utf-8")) > 1024
                or not key.startswith(self.prefix)
                or not _safe_key(key[len(self.prefix) :])
            )
            or not key.startswith(self.prefix)
            or key[len(self.prefix) :] not in ALLOWED_RELATIVE_KEYS
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
    if isinstance(ref, dict) and type(ref.get("schema")) is int and ref["schema"] == 2:
        from app.services import media_cache_storage

        try:
            saved = media_cache_storage.resolve_ref(
                ref, expected_prefix=expected_prefix
            )
            if _PREFIX.fullmatch(saved.logical_prefix) is None:
                _reject()
            return Owner(saved.bucket, saved.prefix, saved.ref)
        except ValueError:
            _reject()
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


def _field(record, name):
    return record.get(name) if isinstance(record, dict) else getattr(record, name, None)


def resolve(record):
    return resolve_ref(
        _field(record, "storage_ref"), expected_prefix=prefix_for(_field(record, "id"))
    )


async def allocate(db, output_id):
    """Save a new attempt's target; subsequent I/O never follows the binding."""
    from app.services import media_cache_storage

    try:
        return await media_cache_storage.allocate(db, prefix_for(output_id))
    except ValueError:
        _reject("binding_unproven")


async def transport_target(db, output, *, require_enabled=True):
    """Capture current transport metadata without provider I/O or business locks."""
    owner = resolve(output)
    if owner.ref["schema"] == 1:
        return None
    from app.services import media_cache_storage

    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as session:
        try:
            return await media_cache_storage.target_for_ref(
                session, owner.ref, require_enabled=require_enabled
            )
        except ValueError:
            _reject()


@dataclass(frozen=True)
class ReadStorage:
    """Explicit ready-only transports for the selected immutable members."""

    reference: dict
    members: tuple
    source: object
    readers: object


def _reading_client(storage, owner, key, receipt):
    if not isinstance(storage, ReadStorage):
        return _client(storage, owner)
    if storage.reference != owner.ref:
        _reject()
    selected = [member for member in storage.members if member[0] == key]
    if len(selected) != 1 or selected[0][1:3] != (
        receipt["sha256"],
        receipt["size_bytes"],
    ):
        _reject()
    route = selected[0][3]
    if route is None:
        return _client(storage.source, owner)
    if not isinstance(storage.readers, migration_reads.ReadClientSet):
        _reject()
    reader = storage.readers.reader_for(owner.bucket, key)
    if (
        reader.route != route
        or str(route.source_location_id) != owner.ref["location_id"]
        or route.source_endpoint_url != owner.ref["endpoint_url"]
        or route.source_bucket != owner.bucket
        or route.source_object_key != key
        or route.sha256 != receipt["sha256"]
        or route.size_bytes != receipt["size_bytes"]
    ):
        _reject()
    return reader


def _client(storage, owner):
    endpoint = getattr(getattr(storage.client, "meta", None), "endpoint_url", None)
    if (
        not isinstance(endpoint, str)
        or endpoint.rstrip("/") != owner.ref["endpoint_url"]
    ):
        _reject()
    return storage.client


def _translate(exc):
    if isinstance(exc, VideoDerivativeStorageError) and not getattr(
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
    result = VideoDerivativeStorageError(suffix)
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(result)
    return result


@contextmanager
def open_storage(factory=None, *, target=None, single_attempt=False, owned=False):
    """Keep constructor/close uncertainty inside the caller's output permit."""
    if target is not None:
        try:
            context = target.client(
                read_timeout=30,
                **({"single_attempt": True} if single_attempt else {}),
                **({"owned": True} if owned else {}),
            )
            client = context.__enter__()
        except BaseException as exc:
            if isinstance(exc, Exception):
                raise _translate(storage_access.mark_unsettled(exc)) from None
            raise
        try:
            yield SimpleNamespace(client=client, media_cache_bucket=target.bucket)
        finally:
            try:
                context.__exit__(*sys.exc_info())
            except Exception as exc:
                raise _translate(storage_access.mark_unsettled(exc)) from None
        return
    try:
        storage = (
            factory()
            if factory is not None
            else StorageService(
                **(
                    {
                        "client_config": Config(
                            retries={"total_max_attempts": 1, "mode": "standard"}
                        )
                    }
                    if single_attempt
                    else {}
                )
            )
        )
        client = storage.client
    except BaseException as exc:
        storage_access.mark_unsettled(exc)
        if isinstance(exc, Exception):
            raise _translate(storage_access.mark_unsettled(exc)) from None
        raise
    try:
        yield storage
    finally:
        try:
            storage_access.close_registered(client)
        except Exception as exc:
            raise _translate(storage_access.mark_unsettled(exc)) from None


def _receipt(value, *, maximum=MAX_OUTPUT_BYTES):
    if (
        not isinstance(value, dict)
        or set(value) != {"sha256", "size_bytes"}
        or not isinstance(value["sha256"], str)
        or _SHA.fullmatch(value["sha256"]) is None
        or type(value["size_bytes"]) is not int
        or not 0 < value["size_bytes"] <= maximum
    ):
        _reject("invalid_receipt")
    return deepcopy(value)


def validate_inventory(output, inventory=None):
    """Validate owned names and aggregate bytes without provider I/O or mutation."""
    owner = resolve(output)
    value = _field(output, "inventory") if inventory is None else inventory
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "objects"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["objects"], dict)
        or not 1 <= len(value["objects"]) <= len(ALLOWED_RELATIVE_KEYS)
        or owner.key("metadata.json") not in value["objects"]
    ):
        _reject("invalid_receipt")
    objects = {}
    for key, receipt in value["objects"].items():
        owner.contains(key)
        maximum = (
            MAX_RESULT_BYTES if key.endswith("/metadata.json") else MAX_OUTPUT_BYTES
        )
        objects[key] = _receipt(receipt, maximum=maximum)
    if sum(value["size_bytes"] for value in objects.values()) > MAX_OUTPUT_BYTES:
        _reject("budget")
    return objects


def _expected(output, owner, key, expected):
    owner.contains(key)
    if expected is None:
        expected = validate_inventory(output).get(key)
    maximum = (
        MAX_RESULT_BYTES if key == owner.key("metadata.json") else MAX_OUTPUT_BYTES
    )
    return _receipt(expected, maximum=maximum)


def _version(metadata):
    value = metadata.get("VersionId")
    if value in (None, "null"):
        return None
    if not _plain(value, 1024):
        _reject("integrity_mismatch")
    return value


def _http_ok(response):
    metadata = response.get("ResponseMetadata")
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("HTTPStatusCode")) is not int
        or metadata["HTTPStatusCode"] != 200
        or response.get("ContentRange") is not None
    ):
        _reject("integrity_mismatch")


def _read(client, owner, key, receipt, *, collect=False, accounting_attempt=None):
    owner.contains(key)
    _receipt(receipt)
    params = {"Bucket": owner.bucket, "Key": key}
    head = client.head_object(**params)
    _http_ok(head)
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
        _http_ok(response)
        if (
            response.get("ETag") != etag
            or _version(response) != version
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != receipt["size_bytes"]
        ):
            _reject("integrity_mismatch")
        checksum, size, chunks = hashlib.sha256(), 0, []
        while True:
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
    after = client.head_object(**params)
    _http_ok(after)
    if (
        after.get("ETag") != etag
        or _version(after) != version
        or type(after.get("ContentLength")) is not int
        or after["ContentLength"] != receipt["size_bytes"]
    ):
        _reject("integrity_mismatch")
    if accounting_attempt is not None:
        accounting_attempt.verified(client, version)
    return b"".join(chunks) if collect else deepcopy(receipt)


def _objects(client, owner):
    result, token, seen_tokens = {}, None, set()
    while True:
        params = {"Bucket": owner.bucket, "Prefix": owner.prefix, "MaxKeys": 1000}
        if token is not None:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        _http_ok(response)
        rows = response.get("Contents", [])
        if not isinstance(rows, list) or type(response.get("IsTruncated")) is not bool:
            _reject("integrity_mismatch")
        for row in rows:
            if not isinstance(row, dict):
                _reject("integrity_mismatch")
            key, size = row.get("Key"), row.get("Size")
            owner.contains(key)
            if key in result or type(size) is not int or size < 0:
                _reject("integrity_mismatch")
            result[key] = size
            if (
                len(result) > len(ALLOWED_RELATIVE_KEYS)
                or sum(result.values()) > MAX_OUTPUT_BYTES
            ):
                _reject("budget")
        if not response["IsTruncated"]:
            return result
        next_token = response.get("NextContinuationToken")
        if not _plain(next_token, 8192) or next_token in seen_tokens:
            _reject("integrity_mismatch")
        seen_tokens.add(next_token)
        token = next_token


def _file_receipt(stream):
    checksum, size = hashlib.sha256(), 0
    while True:
        chunk = stream.read(min(CHUNK_BYTES, MAX_OUTPUT_BYTES - size + 1))
        if type(chunk) is not bytes:
            _reject("integrity_mismatch")
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            _reject("budget")
        checksum.update(chunk)
    if size <= 0:
        _reject("invalid_receipt")
    return {"sha256": checksum.hexdigest(), "size_bytes": size}


def _file(path):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        _reject("invalid_receipt")
    if info.st_size > MAX_OUTPUT_BYTES:
        _reject("budget")
    return path


def file_receipt(path):
    """Hash one bounded regular file without retaining its bytes in memory."""
    try:
        path = _file(path)
        with storage_access.closing_registered(path.open("rb")) as stream:
            return _file_receipt(stream)
    except Exception as exc:
        raise _translate(exc) from None


def _upload(client, owner, key, body, receipt, content_type, *, accounting=None):
    existing = _objects(client, owner)
    if key in existing:
        _reject("integrity_mismatch")
    if sum(existing.values()) + receipt["size_bytes"] > MAX_OUTPUT_BYTES:
        _reject("budget")
    result, attempt = media_cache_accounting.put(
        accounting,
        client,
        key,
        receipt,
        Bucket=owner.bucket,
        Key=key,
        Body=body,
        ContentLength=receipt["size_bytes"],
        ContentType=content_type,
        CacheControl="private, max-age=31536000, immutable",
        IfNoneMatch="*",
    )
    _http_ok(result)
    _read(client, owner, key, receipt, accounting_attempt=attempt)
    return deepcopy(receipt)


def upload_file(storage, output, path, relative, content_type, *, accounting=None):
    """Create an exclusive object and fully verify it while the local FD stays owned."""
    owner = resolve(output)
    client = _client(storage, owner)
    key = owner.key(relative)
    expected_type = {
        "poster.webp": "image/webp",
        "frame.webp": "image/webp",
        "frame.jpeg": "image/jpeg",
        "playback.mp4": "video/mp4",
        "chunk.mp4": "video/mp4",
    }.get(relative)
    if content_type != expected_type or expected_type is None:
        _reject("invalid_receipt")
    try:
        path = _file(path)
        with storage_access.closing_registered(path.open("rb")) as stream:
            receipt = _file_receipt(stream)
            stream.seek(0)
            return _upload(
                client, owner, key, stream, receipt, content_type, accounting=accounting
            )
    except Exception as exc:
        raise _translate(exc) from None


def _json_result(result):
    if not isinstance(result, dict):
        _reject("invalid_receipt")
    try:
        data = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if json.loads(data) != result:
            _reject("invalid_receipt")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject("invalid_receipt")
    if not 0 < len(data) <= MAX_RESULT_BYTES:
        _reject("budget")
    return data


def write_result(storage, output, result, *, accounting=None):
    """Write canonical result metadata; publication compares its exact content."""
    owner = resolve(output)
    client = _client(storage, owner)
    data = _json_result(result)
    receipt = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    try:
        return _upload(
            client,
            owner,
            owner.key("metadata.json"),
            data,
            receipt,
            "application/json",
            accounting=accounting,
        )
    except Exception as exc:
        raise _translate(exc) from None


def verify_file(storage, output, key, expected=None):
    """Verify complete binary content without collecting chunk/playback bytes."""
    owner = resolve(output)
    receipt = _expected(output, owner, key, expected)
    client = _reading_client(storage, owner, key, receipt)
    try:
        return _read(client, owner, key, receipt)
    except Exception as exc:
        raise _translate(exc) from None


def verify_object(storage, output, key, expected=None, *, max_bytes=MAX_RESULT_BYTES):
    """Read a small verified object; callers must stream large video via verify_file."""
    owner = resolve(output)
    receipt = _expected(output, owner, key, expected)
    client = _reading_client(storage, owner, key, receipt)
    if (
        type(max_bytes) is not int
        or not 0 < max_bytes <= MAX_RESULT_BYTES
        or receipt["size_bytes"] > max_bytes
    ):
        _reject("budget")
    try:
        return _read(client, owner, key, receipt, collect=True)
    except Exception as exc:
        raise _translate(exc) from None


def read_bytes(storage, output, key, max_bytes=MAX_RESULT_BYTES):
    """Read only a saved inventory entry, within the small-result memory budget."""
    return verify_object(storage, output, key, max_bytes=max_bytes)


def verify_inventory(storage, output, inventory):
    """Require an exact prefix and fully hash every object, including metadata."""
    owner = resolve(output)
    client = _client(storage, owner)
    objects = validate_inventory(output, inventory)
    expected = {key: value["size_bytes"] for key, value in objects.items()}
    try:
        if _objects(client, owner) != expected:
            _reject("integrity_mismatch")
        for key, receipt in objects.items():
            verify_file(storage, output, key, receipt)
        if _objects(client, owner) != expected:
            _reject("integrity_mismatch")
        return {"schema": 1, "objects": objects}
    except Exception as exc:
        raise _translate(exc) from None


def signed_url(storage, output, key, expires=3600):
    """Sign one saved inventory key at its saved endpoint/bucket, without I/O."""
    owner = resolve(output)
    owner.contains(key)
    if (
        key not in validate_inventory(output)
        or type(expires) is not int
        or not 0 < expires <= 3600
    ):
        _reject("invalid_receipt")
    client = _reading_client(storage, owner, key, validate_inventory(output)[key])
    if owner.ref["schema"] == 2:
        from app.services import media_cache_urls

        return media_cache_urls.sign(
            "video-derivative",
            _field(output, "id"),
            key,
            owner.ref,
            validate_inventory(output)[key],
            expires,
        )
    try:
        if isinstance(client, migration_reads.ReadClient):
            return storage_presigning.generate_url(
                client,
                "get_object",
                params={"Bucket": owner.bucket, "Key": key},
                expires_in=expires,
                audience="internal",
            )
        source = storage.source if isinstance(storage, ReadStorage) else storage
        return source.generate_download_url(
            key, expires_in=expires, bucket=owner.bucket, align=False
        )
    except Exception as exc:
        raise _translate(exc) from None


def delete_output(storage, output, *, accounting=None):
    """Delete only the saved UUID prefix after the caller retires its owner."""
    owner = resolve(output)
    client = _client(storage, owner)
    try:
        objects = _objects(client, owner)
        for key in objects:
            if accounting is None:
                client.delete_object(Bucket=owner.bucket, Key=key)
            else:
                from app.services.key_removal_accounting import Observation

                observation = Observation(accounting, client, key)
                response = observation.submit(
                    lambda: client.delete_object(Bucket=owner.bucket, Key=key)
                )
                observation.complete(response)
        if _objects(client, owner):
            _reject("integrity_mismatch")
        return len(objects)
    except Exception as exc:
        raise _translate(exc) from None
