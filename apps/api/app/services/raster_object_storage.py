"""Strict receipts and physical I/O for one independent RLE candidate.

Only allocation consults the current annotations binding. Logical portable keys
never select a physical bucket or replace the candidate's UUID locator.
"""

from copy import deepcopy
from dataclasses import dataclass, field, make_dataclass, replace
from datetime import datetime
import hashlib
import json
import re
import time
import sys
import uuid
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from app.config import settings
from app.services import annotations_storage, storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.utils.raster_mask_gzip import (
    MAX_COMPRESSED_BYTES,
    MAX_EXPANSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    decompress_mask_gzip,
)
from app.utils.raster_mask_rle import (
    MAX_MASK_DIMENSION,
    MAX_MASK_PIXELS,
    MAX_MASK_RUNS,
    validate_coco_rle,
)

MAX_BYTES = MAX_UNCOMPRESSED_BYTES
VERIFY_SECONDS = 600
CHUNK_BYTES = 1024 * 1024
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(r"raster-masks/objects/" + _UUID + r"\.json(?:\.gz)?")
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
        "object_key",
        "storage_identity",
    }
)
_CONTENT = (
    "logical_key",
    "canonical_sha256",
    "canonical_size_bytes",
    "height",
    "width",
    "runs",
    "storage_encoding",
    "stored_sha256",
    "stored_size_bytes",
)
_FACTS = ("id", *_CONTENT, "created_at")
_MESSAGES = {
    "unpinned": "掩码尚未保存独立存储归属",
    "identity_changed": "掩码存储身份与当前端点不匹配",
    "invalid_key": "掩码对象键与独立候选不匹配",
    "invalid_receipt": "掩码内容回执不合法",
    "binding_unproven": "掩码存储绑定尚未登记或无法核实",
    "integrity_mismatch": "掩码字节与独立内容回执不一致",
    "missing": "已保存的掩码对象不存在",
    "budget": "掩码核验超过读取预算",
    "unavailable": "掩码存储暂不可用",
}


class RasterObjectStorageError(ValueError):
    def __init__(self, code, status_code=None):
        suffix = code.removeprefix("raster_object_storage_")
        super().__init__(_MESSAGES.get(suffix, _MESSAGES["unavailable"]))
        self.code = "raster_object_storage_" + suffix
        self.status_code = (
            status_code
            if status_code is not None
            else {"missing": 404, "unavailable": 503}.get(suffix, 409)
        )


def _reject(code="identity_changed"):
    raise RasterObjectStorageError(code)


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


def key_for(identifier, storage_encoding="identity"):
    if storage_encoding not in ("identity", "gzip"):
        _reject("invalid_key")
    return f"raster-masks/objects/{_uuid(identifier)}.json" + (
        ".gz" if storage_encoding == "gzip" else ""
    )


def _logical(sha, encoding):
    return f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json" + (
        ".gz" if encoding == "gzip" else ""
    )


def _integer(value, maximum):
    return type(value) is int and 0 < value <= maximum


def _facts(record):
    facts = {name: getattr(record, name, None) for name in _CONTENT}
    if (
        any(
            not isinstance(facts[name], str) or _SHA.fullmatch(facts[name]) is None
            for name in ("canonical_sha256", "stored_sha256")
        )
        or facts["storage_encoding"] not in ("identity", "gzip")
        or not _integer(facts["canonical_size_bytes"], MAX_BYTES)
        or not _integer(facts["stored_size_bytes"], MAX_COMPRESSED_BYTES)
        or not _integer(facts["height"], MAX_MASK_DIMENSION)
        or not _integer(facts["width"], MAX_MASK_DIMENSION)
        or facts["height"] * facts["width"] > MAX_MASK_PIXELS
        or not _integer(facts["runs"], MAX_MASK_RUNS)
        or facts["logical_key"]
        != _logical(facts["canonical_sha256"], facts["storage_encoding"])
    ):
        _reject("invalid_receipt")
    if facts["storage_encoding"] == "identity":
        if (
            facts["canonical_sha256"] != facts["stored_sha256"]
            or facts["canonical_size_bytes"] != facts["stored_size_bytes"]
        ):
            _reject("invalid_receipt")
    elif (
        facts["canonical_size_bytes"] > facts["stored_size_bytes"] * MAX_EXPANSION_RATIO
    ):
        _reject("invalid_receipt")
    return facts


def reference(record):
    facts = _facts(record)
    result = {
        "encoding": "coco_rle_ref",
        "size": [facts["height"], facts["width"]],
        "object_key": facts["logical_key"],
        "sha256": facts["canonical_sha256"],
        "runs": facts["runs"],
        "bytes": facts["canonical_size_bytes"],
    }
    if facts["storage_encoding"] == "gzip":
        result["storage_encoding"] = "gzip"
    return result


def content(reference, encoded_data):
    """Validate portable metadata and exact canonical bytes before allocation."""
    required = {"encoding", "size", "object_key", "sha256", "runs", "bytes"}
    if (
        not isinstance(reference, dict)
        or not required <= set(reference) <= required | {"storage_encoding"}
        or reference["encoding"] not in ("coco_rle_ref", "coco_rle_gzip")
        or not isinstance(reference["object_key"], str)
        or type(encoded_data) is not bytes
        or not 0 < len(encoded_data) <= MAX_COMPRESSED_BYTES
    ):
        _reject("invalid_receipt")
    encoding = "gzip" if reference["object_key"].endswith(".json.gz") else "identity"
    if reference.get("storage_encoding") not in (None, encoding):
        _reject("invalid_receipt")
    try:
        canonical = (
            decompress_mask_gzip(encoded_data) if encoding == "gzip" else encoded_data
        )
        if not 0 < len(canonical) <= MAX_BYTES:
            _reject("invalid_receipt")
        payload = json.loads(canonical)
        height, width, counts = validate_coco_rle(payload)
        exact = json.dumps(
            {"encoding": "coco_rle", "size": [height, width], "counts": counts},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if canonical != exact:
            _reject("invalid_receipt")
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        OverflowError,
        RecursionError,
    ):
        _reject("invalid_receipt")
    digest = hashlib.sha256(canonical).hexdigest()
    if (
        reference["object_key"] != _logical(digest, encoding)
        or reference["sha256"] != digest
        or not isinstance(reference["size"], list)
        or len(reference["size"]) != 2
        or any(type(value) is not int for value in reference["size"])
        or reference["size"] != [height, width]
        or type(reference["runs"]) is not int
        or reference["runs"] != len(counts)
        or type(reference["bytes"]) is not int
        or reference["bytes"] != len(canonical)
    ):
        _reject("invalid_receipt")
    return {
        "logical_key": reference["object_key"],
        "canonical_sha256": digest,
        "canonical_size_bytes": len(canonical),
        "height": height,
        "width": width,
        "runs": len(counts),
        "storage_encoding": encoding,
        "stored_sha256": hashlib.sha256(encoded_data).hexdigest(),
        "stored_size_bytes": len(encoded_data),
    }


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
    def ref(self):
        return deepcopy(self._ref)


def resolve_ref(ref, *, expected_key=None):
    if isinstance(ref, dict) and type(ref.get("schema")) is int and ref["schema"] == 2:
        try:
            saved = annotations_storage.resolve_ref(ref, expected_key=expected_key)
            if _KEY.fullmatch(saved.logical_key) is None:
                _reject()
        except ValueError:
            _reject()
        return Owner(saved.bucket, saved.object_key, saved.ref)
    if ref is None:
        _reject("unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] != 1
        or ref["purpose"] != "annotations"
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] != 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] != 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["object_key"], str)
        or _KEY.fullmatch(ref["object_key"]) is None
        or (expected_key is not None and ref["object_key"] != expected_key)
        or not isinstance(ref["storage_identity"], str)
        or _SHA.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref[
        "storage_identity"
    ] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    return Owner(ref["bucket"], ref["object_key"], deepcopy(ref))


def resolve(record):
    _facts(record)
    created = getattr(record, "created_at", None)
    if (
        not isinstance(created, datetime)
        or created.tzinfo is None
        or created.utcoffset() is None
    ):
        _reject("invalid_receipt")
    return resolve_ref(
        getattr(record, "storage_ref", None),
        expected_key=key_for(getattr(record, "id", None), record.storage_encoding),
    )


_Snapshot = make_dataclass(
    "RasterObjectSnapshot",
    [(name, object) for name in _FACTS] + [("_ref_json", str)],
    frozen=True,
    namespace={"storage_ref": property(lambda self: json.loads(self._ref_json))},
)


def snapshot(record):
    resolve(record)
    return _Snapshot(
        *(deepcopy(getattr(record, name)) for name in _FACTS),
        json.dumps(record.storage_ref, sort_keys=True),
    )


def same(record, receipt):
    return all(
        getattr(record, name, None) == getattr(receipt, name, None) for name in _FACTS
    ) and getattr(record, "storage_ref", None) == getattr(receipt, "storage_ref", None)


def _client(client, owner):
    endpoint = getattr(getattr(client, "meta", None), "endpoint_url", None)
    if (
        not isinstance(endpoint, str)
        or endpoint.rstrip("/") != owner.ref["endpoint_url"]
    ):
        _reject()


async def allocate(db, record) -> dict:
    """Capture the current target; the caller durably owns it before provider I/O."""
    _facts(record)
    try:
        return await annotations_storage.allocate(
            db, key_for(record.id, record.storage_encoding)
        )
    except ValueError:
        _reject("binding_unproven")


async def io_target(db, record):
    """Capture current credentials without changing the saved physical receipt."""
    owner = resolve(record)
    try:
        ref = (
            annotations_storage.legacy_ref(owner.bucket, owner.object_key)
            if owner.ref["schema"] == 1
            else owner.ref
        )
        return await annotations_storage.target_for_ref(db, ref)
    except ValueError:
        _reject()


def run_io(target, client, operation, record, *args, **kwargs):
    """Open and close the captured transport inside the caller's existing permit."""
    owner = resolve(record)
    ref = owner.ref
    if (
        str(target.location_id) != ref["location_id"]
        or target.endpoint_url != ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix != ref.get("key_prefix", "")
    ):
        _reject()
    if target.location_id == LEGACY_LOCATION_ID:
        # Keep the existing explicit legacy-client injection for local callers.
        target = replace(target, _legacy_client=client)
    try:
        context = target.client()
        physical_client = context.__enter__()
    except Exception as exc:
        storage_access.mark_unsettled(exc)
        raise _unavailable(exc) from None
    try:
        return globals()[operation](physical_client, record, *args, **kwargs)
    finally:
        try:
            context.__exit__(*sys.exc_info())
        except BaseException as exc:
            storage_access.mark_unsettled(exc)
            if isinstance(exc, Exception):
                raise _unavailable(exc) from None
            raise


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("budget")


def _unavailable(exc):
    error = RasterObjectStorageError("unavailable")
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _version(metadata):
    value = metadata.get("VersionId")
    if value is None or value == "null":
        return None
    if not _plain(value, 1024):
        _reject("integrity_mismatch")
    return value


def _verified_data(record, data):
    try:
        facts = content(reference(record), data)
    except RasterObjectStorageError:
        _reject("integrity_mismatch")
    if any(facts[name] != getattr(record, name) for name in _CONTENT):
        _reject("integrity_mismatch")
    canonical = (
        decompress_mask_gzip(data) if record.storage_encoding == "gzip" else data
    )
    return json.loads(canonical)


def read(client, record, *, missing_ok=False):
    """Conditionally read the entire saved object and its canonical RLE."""
    owner = resolve(record)
    _client(client, owner)
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        head = client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != record.stored_size_bytes
            or not _plain(etag, 1024)
        ):
            _reject("integrity_mismatch")
        version = _version(head)
        condition = {"IfMatch": etag}
        if version is not None:
            condition["VersionId"] = version
        response = client.get_object(**params, **condition)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                response.get("ETag") != etag
                or _version(response) != version
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != record.stored_size_bytes
            ):
                _reject("integrity_mismatch")
            checksum, size, chunks = hashlib.sha256(), 0, []
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, record.stored_size_bytes - size + 1))
                if type(chunk) is not bytes:
                    _reject("integrity_mismatch")
                if not chunk:
                    break
                size += len(chunk)
                if size > record.stored_size_bytes:
                    _reject("integrity_mismatch")
                checksum.update(chunk)
                chunks.append(chunk)
            if (
                size != record.stored_size_bytes
                or checksum.hexdigest() != record.stored_sha256
            ):
                _reject("integrity_mismatch")
        _budget(deadline)
        after = client.head_object(**params)
        if (
            after.get("ETag") != etag
            or _version(after) != version
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != record.stored_size_bytes
        ):
            _reject("integrity_mismatch")
        result = _verified_data(record, b"".join(chunks))
        _budget(deadline)
        return result
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
            if missing_ok:
                return None
            raise RasterObjectStorageError("missing") from None
        if code in {"412", "PreconditionFailed"}:
            raise RasterObjectStorageError("integrity_mismatch") from None
        raise _unavailable(exc) from None
    except RasterObjectStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None


def write(client, record, data):
    """Create without overwriting and fully verify the exact saved receipt."""
    owner = resolve(record)
    _client(client, owner)
    _verified_data(record, data)
    try:
        client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=data,
            ContentLength=len(data),
            ContentType="application/gzip"
            if record.storage_encoding == "gzip"
            else "application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"412", "PreconditionFailed"}:
            if code in {"409", "ConditionalRequestConflict"}:
                raise RasterObjectStorageError("integrity_mismatch") from None
            raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None
    read(client, record)


def delete(client, record, *, missing_ok=True):
    """Verify present bytes before deleting only this saved UUID candidate."""
    owner = resolve(record)
    _client(client, owner)
    if read(client, record, missing_ok=missing_ok) is None:
        return
    try:
        client.delete_object(Bucket=owner.bucket, Key=owner.object_key)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        if str(exc.response.get("Error", {}).get("Code", "")) in {
            "404",
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
        }:
            if missing_ok:
                return
            raise RasterObjectStorageError("missing") from None
        raise _unavailable(exc) from None
    except Exception as exc:
        raise _unavailable(exc) from None
