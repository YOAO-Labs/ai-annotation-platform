"""Pinned audit archive ownership and bounded, authenticated full-byte I/O."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, make_dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import math
import re
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zlib

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.config import settings
from app.db.models.dataset_asset import StorageBinding, StorageLocation
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID, ensure_legacy_bindings
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service

MAX_BYTES = 64 * 1024**3
MAX_SOURCE_BYTES = 256 * 1024**3
MAX_ROW_BYTES = 16 * 1024**2
MAX_ROWS = 10_000_000
VERIFY_SECONDS = 600
CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
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
_ROW_FIELDS = frozenset(
    {
        "id",
        "actor_id",
        "actor_email",
        "actor_role",
        "action",
        "target_type",
        "target_id",
        "method",
        "path",
        "status_code",
        "ip",
        "detail_json",
        "request_id",
        "created_at",
    }
)
_FACTS = (
    "id",
    "job_id",
    "year",
    "month",
    "source_schema",
    "source_table",
    "parent_oid",
    "source_oid",
    "month_start",
    "month_end",
    "format_revision",
    "row_count",
    "source_size_bytes",
    "source_sha256",
    "sha256",
    "size_bytes",
    "created_at",
    "state",
    "published_at",
)
_MESSAGES = {
    "audit_archive_storage_identity_changed": "归档存储身份与已保存的归属不匹配",
    "audit_archive_storage_unpinned": "归档尚未保存独立存储归属",
    "audit_archive_storage_invalid_key": "归档对象键与归档记录不匹配",
    "audit_archive_storage_invalid_receipt": "归档身份或内容回执不合法",
    "audit_archive_storage_binding_unproven": "归档存储绑定尚未登记或无法核实",
    "audit_archive_storage_integrity_mismatch": "归档字节与独立内容回执不一致",
    "audit_archive_storage_missing": "已保存的归档对象不存在",
    "audit_archive_storage_budget": "归档核验超过读取预算",
    "audit_archive_storage_unavailable": "归档存储暂不可用",
}


class AuditArchiveStorageError(ValueError):
    def __init__(self, code, *, status_code=409):
        super().__init__(_MESSAGES.get(code, "归档存储暂不可用"))
        self.code = code
        self.status_code = status_code


def _reject(code="audit_archive_storage_integrity_mismatch"):
    raise AuditArchiveStorageError(code)


def _uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("audit_archive_storage_invalid_receipt")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("audit_archive_storage_invalid_receipt")
    if str(value) != result:
        _reject("audit_archive_storage_invalid_receipt")
    return result


def _month(year, month):
    if (
        type(year) is not int
        or type(month) is not int
        or not 2000 <= year <= 9999
        or not 1 <= month <= 12
        or (year, month) == (9999, 12)
    ):
        _reject("audit_archive_storage_invalid_receipt")
    return date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)


def key_for(year, month, archive_id):
    _month(year, month)
    return f"{year}/{month:02d}/{_uuid(archive_id)}.jsonl.gz"


def _plain(value, maximum):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and not any(ord(char) <= 32 or ord(char) == 127 for char in value)
    )


def _endpoint(value):
    if not _plain(value, 2048) or value.endswith("/") or "\\" in value:
        _reject("audit_archive_storage_identity_changed")
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
            _reject("audit_archive_storage_identity_changed")
        parsed.port
    except ValueError:
        _reject("audit_archive_storage_identity_changed")
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
        _reject("audit_archive_storage_identity_changed")
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
    if ref is None:
        _reject("audit_archive_storage_unpinned")
    if not isinstance(ref, dict) or set(ref) != _FIELDS:
        _reject("audit_archive_storage_identity_changed")
    if (
        any(
            type(ref[name]) is not int or ref[name] != 1
            for name in ("schema", "config_revision", "binding_generation")
        )
        or ref["purpose"] != "audit-archive"
        or ref["location_id"] != str(LEGACY_LOCATION_ID)
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["storage_identity"], str)
        or _SHA256.fullmatch(ref["storage_identity"]) is None
    ):
        _reject("audit_archive_storage_identity_changed")
    key = ref["object_key"]
    if not isinstance(key, str):
        _reject("audit_archive_storage_invalid_key")
    match = re.fullmatch(r"([0-9]{4})/([0-9]{2})/([0-9a-f-]{36})\.jsonl\.gz", key)
    if (
        match is None
        or key != key_for(int(match[1]), int(match[2]), match[3])
        or (expected_key is not None and key != expected_key)
    ):
        _reject("audit_archive_storage_invalid_key")
    endpoint = _endpoint(ref["endpoint_url"])
    if endpoint != _runtime_endpoint() or ref[
        "storage_identity"
    ] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject("audit_archive_storage_identity_changed")
    return Owner(ref["bucket"], key, deepcopy(ref))


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _reject("audit_archive_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def _source_facts(row):
    start, end = _month(getattr(row, "year", None), getattr(row, "month", None))
    _uuid(getattr(row, "id", None))
    _uuid(getattr(row, "job_id", None))
    if (
        getattr(row, "source_schema", None) != "public"
        or getattr(row, "source_table", None)
        != f"audit_logs_y{row.year}m{row.month:02d}"
        or type(getattr(row, "month_start", None)) is not date
        or row.month_start != start
        or type(getattr(row, "month_end", None)) is not date
        or row.month_end != end
    ):
        _reject("audit_archive_storage_invalid_receipt")
    for name, low, high in (
        ("parent_oid", 1, 2**32 - 1),
        ("source_oid", 1, 2**32 - 1),
        ("format_revision", 1, 1),
        ("row_count", 0, MAX_ROWS),
        ("source_size_bytes", 0, MAX_SOURCE_BYTES),
    ):
        value = getattr(row, name, None)
        if type(value) is not int or not low <= value <= high:
            _reject("audit_archive_storage_invalid_receipt")
    if (
        not isinstance(getattr(row, "source_sha256", None), str)
        or _SHA256.fullmatch(row.source_sha256) is None
    ):
        _reject("audit_archive_storage_invalid_receipt")


def resolve(row):
    _source_facts(row)
    if (
        type(getattr(row, "size_bytes", None)) is not int
        or not 0 < row.size_bytes <= MAX_BYTES
        or not isinstance(getattr(row, "sha256", None), str)
        or _SHA256.fullmatch(row.sha256) is None
    ):
        _reject("audit_archive_storage_invalid_receipt")
    created = _instant(getattr(row, "created_at", None))
    state, published = getattr(row, "state", None), getattr(row, "published_at", None)
    if (
        state not in {"pending", "ready"}
        or (state == "pending" and published is not None)
        or (state == "ready" and (published is None or _instant(published) < created))
    ):
        _reject("audit_archive_storage_invalid_receipt")
    return resolve_ref(
        getattr(row, "storage_ref", None),
        expected_key=key_for(row.year, row.month, row.id),
    )


_Snapshot = make_dataclass(
    "ArchiveSnapshot",
    [(name, object) for name in _FACTS] + [("_ref_json", str)],
    frozen=True,
    namespace={"storage_ref": property(lambda self: json.loads(self._ref_json))},
)


def snapshot(row):
    resolve(row)
    return _Snapshot(
        *(deepcopy(getattr(row, name)) for name in _FACTS),
        json.dumps(row.storage_ref, sort_keys=True),
    )


def same(row, original):
    return all(
        getattr(row, name, None) == getattr(original, name, None) for name in _FACTS
    ) and getattr(row, "storage_ref", None) == getattr(original, "storage_ref", None)


async def allocate(db, row):
    key = key_for(row.year, row.month, row.id)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("audit_archive_storage_binding_unproven")
    location = await db.get(StorageLocation, LEGACY_LOCATION_ID, populate_existing=True)
    binding = await db.scalar(
        select(StorageBinding)
        .where(StorageBinding.purpose == "audit-archive")
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
        or binding.purpose != "audit-archive"
        or binding.location_id != LEGACY_LOCATION_ID
        or type(binding.generation) is not int
        or binding.generation != 1
        or binding.bucket != storage_service.audit_archive_bucket
    ):
        _reject("audit_archive_storage_binding_unproven")
    endpoint = _runtime_endpoint()
    return resolve_ref(
        {
            "schema": 1,
            "purpose": "audit-archive",
            "location_id": str(location.id),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "object_key": key,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_key=key,
    ).ref


def _json_value(value):
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    _reject()


def _encoded(value):
    try:
        result = (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject()
    if len(result) > MAX_ROW_BYTES:
        _reject()
    return result


def canonical_line(mapping):
    """Encode the actual audit row shape, without lossy default string coercion."""
    if not isinstance(mapping, Mapping) or set(mapping) != _ROW_FIELDS:
        _reject()
    value = dict(mapping)
    if (
        type(value["id"]) is not int
        or not -(2**63) <= value["id"] < 2**63
        or (
            value["status_code"] is not None
            and (
                type(value["status_code"]) is not int
                or not -32768 <= value["status_code"] <= 32767
            )
        )
    ):
        _reject()
    for key in _ROW_FIELDS - {
        "id",
        "status_code",
        "actor_id",
        "created_at",
        "ip",
        "detail_json",
    }:
        if value[key] is not None and not isinstance(value[key], str):
            _reject()
    if not isinstance(value["action"], str):
        _reject()
    if value["actor_id"] is not None:
        value["actor_id"] = _uuid(value["actor_id"])
    try:
        if isinstance(value["created_at"], str):
            value["created_at"] = datetime.fromisoformat(value["created_at"])
        value["created_at"] = _instant(value["created_at"]).isoformat(
            timespec="microseconds"
        )
        if value["ip"] is not None:
            if not isinstance(
                value["ip"],
                (
                    str,
                    ipaddress.IPv4Address,
                    ipaddress.IPv6Address,
                    ipaddress.IPv4Interface,
                    ipaddress.IPv6Interface,
                ),
            ):
                _reject()
            address = str(value["ip"])
            value["ip"] = str(
                ipaddress.ip_interface(address)
                if "/" in address
                else ipaddress.ip_address(address)
            )
        value["detail_json"] = _json_value(value["detail_json"])
    except (ValueError, TypeError, RecursionError):
        _reject()
    return _encoded(value)


def metadata(row):
    """The trailer can be built before the compressed receipt is known."""
    _source_facts(row)
    return {
        "_partition_meta": {
            "archive_id": _uuid(row.id),
            "job_id": _uuid(row.job_id),
            "year": row.year,
            "month": row.month,
            "source_schema": row.source_schema,
            "source_table": row.source_table,
            "parent_oid": row.parent_oid,
            "source_oid": row.source_oid,
            "month_start": row.month_start.isoformat(),
            "month_end": row.month_end.isoformat(),
            "format_revision": row.format_revision,
            "row_count": row.row_count,
            "source_size_bytes": row.source_size_bytes,
            "source_sha256": row.source_sha256,
        }
    }


def encode_meta(row):
    return _encoded(metadata(row))


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("audit_archive_storage_budget")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _semantic(fileobject, row, limit, offset, deadline):
    """Retain one bounded page while validating every source row and the trailer."""
    fileobject.seek(0)
    digest, size, count, previous, items = hashlib.sha256(), 0, 0, None, []
    try:
        with gzip.GzipFile(fileobj=fileobject, mode="rb") as stream:
            while True:
                _budget(deadline)
                line = stream.readline(MAX_ROW_BYTES + 1)
                if not line or len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
                    _reject()
                value = json.loads(
                    line.decode("utf-8"), object_pairs_hook=_unique_pairs
                )
                if count == row.row_count:
                    if line != encode_meta(row) or value != metadata(row):
                        _reject()
                    if offset <= count < offset + limit:
                        items.append(value)
                    if stream.read(1):
                        _reject()
                    break
                if line != canonical_line(value):
                    _reject()
                instant = datetime.fromisoformat(value["created_at"])
                if not row.month_start <= instant.date() < row.month_end:
                    _reject()
                order = (value["id"], instant)
                if previous is not None and order <= previous:
                    _reject()
                previous = order
                count += 1
                size += len(line)
                if (
                    count > MAX_ROWS
                    or size > row.source_size_bytes
                    or size > MAX_SOURCE_BYTES
                ):
                    _reject()
                digest.update(line)
                if offset <= count - 1 < offset + limit:
                    items.append(value)
        if (
            count != row.row_count
            or size != row.source_size_bytes
            or digest.hexdigest() != row.source_sha256
        ):
            _reject()
        _budget(deadline)
    except (
        OSError,
        EOFError,
        zlib.error,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
    ) as exc:
        if isinstance(exc, AuditArchiveStorageError):
            raise
        _reject()
    finally:
        fileobject.seek(0)
    return {"items": items, "total": row.row_count + 1}


def _unavailable(exc):
    error = AuditArchiveStorageError(
        "audit_archive_storage_unavailable", status_code=503
    )
    if getattr(exc, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)
    return error


def _provider_error(exc):
    if getattr(exc, "storage_operation_unsettled", False):
        return _unavailable(exc)
    if isinstance(exc, AuditArchiveStorageError):
        return exc
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return AuditArchiveStorageError(
                "audit_archive_storage_missing", status_code=404
            )
        if code in {"412", "PreconditionFailed", "ConditionalRequestConflict", "409"}:
            return AuditArchiveStorageError("audit_archive_storage_integrity_mismatch")
    return _unavailable(exc)


def _download(row, owner, limit, offset, deadline):
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    try:
        with storage_access.closing_registered(tempfile.TemporaryFile()) as output:
            head = storage_service.client.head_object(**params)
            _budget(deadline)
            etag = head.get("ETag")
            if (
                type(head.get("ContentLength")) is not int
                or head["ContentLength"] != row.size_bytes
                or not isinstance(etag, str)
                or not 0 < len(etag) <= 1024
            ):
                _reject()
            response = storage_service.client.get_object(**params, IfMatch=etag)
            with storage_access.closing_registered(response["Body"]) as body:
                if (
                    response.get("ETag") != etag
                    or type(response.get("ContentLength")) is not int
                    or response["ContentLength"] != row.size_bytes
                ):
                    _reject()
                digest, size = hashlib.sha256(), 0
                while True:
                    _budget(deadline)
                    chunk = body.read(min(CHUNK_BYTES, row.size_bytes - size + 1))
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        _reject()
                    size += len(chunk)
                    if size > row.size_bytes:
                        _reject()
                    digest.update(chunk)
                    output.write(chunk)
                if size != row.size_bytes or digest.hexdigest() != row.sha256:
                    _reject()
            _budget(deadline)
            after = storage_service.client.head_object(**params)
            if (
                after.get("ETag") != etag
                or type(after.get("ContentLength")) is not int
                or after["ContentLength"] != row.size_bytes
            ):
                _reject()
            return _semantic(output, row, limit, offset, deadline)
    except Exception as exc:
        raise _provider_error(exc) from None


def read(row, *, limit=100, offset=0):
    if (
        type(limit) is not int
        or not 1 <= limit <= 1000
        or type(offset) is not int
        or offset < 0
    ):
        _reject("audit_archive_storage_invalid_receipt")
    original = snapshot(row)
    return _download(
        original, resolve(original), limit, offset, time.monotonic() + VERIFY_SECONDS
    )


def write(row, fileobject):
    """Create only; failures preserve the independent owner and never delete."""
    original = snapshot(row)
    owner = resolve(original)
    deadline = time.monotonic() + VERIFY_SECONDS
    try:
        fileobject.seek(0)
        digest, size = hashlib.sha256(), 0
        while True:
            _budget(deadline)
            chunk = fileobject.read(min(CHUNK_BYTES, original.size_bytes - size + 1))
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                _reject()
            size += len(chunk)
            if size > original.size_bytes:
                _reject()
            digest.update(chunk)
        if size != original.size_bytes or digest.hexdigest() != original.sha256:
            _reject()
        _semantic(fileobject, original, 1, original.row_count + 1, deadline)
        _budget(deadline)
        storage_service.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=fileobject,
            ContentLength=original.size_bytes,
            ContentType="application/gzip",
            IfNoneMatch="*",
        )
        _budget(deadline)
        _download(original, owner, 1, original.row_count + 1, deadline)
    except Exception as exc:
        raise _provider_error(exc) from None
    finally:
        fileobject.seek(0)
