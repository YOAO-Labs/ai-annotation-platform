"""Own temporary prediction inputs independently of business rows and workers."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
import hashlib
import io
import json
import logging
import math
import os
import stat
import re
import time
from urllib.parse import urlsplit
import uuid

from botocore.config import Config
from PIL import Image, ImageOps
from sqlalchemy import Text, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, array
from sqlalchemy.orm import load_only

from app.config import settings
from app.db.models.prediction_input import PredictionInputLease as InputRecord
from app.db.models.storage_maintenance import StorageOperation
from app.services import storage_access, storage_usage_accounting as accounting
from app.services.content_snapshot import ContentError, MAX_BYTES, MAX_PIXELS, Snapshot
from app.services.managed_original_reader import BinaryStage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import StorageService

log = logging.getLogger(__name__)
ACTIVE_SECONDS = 2 * 60 * 60
RECOVERY_DAYS = 7
RETRY_SECONDS = 60
MAX_VIDEO_BYTES = 5 * 1024**3
VIDEO_KINDS = frozenset(
    {
        "video_batch_predict",
        "video_prediction_retry",
        "video_predict_test",
        "video_interactive",
        "video_tracker",
    }
)
IMAGE_KINDS = frozenset(
    {
        "batch_predict",
        "prediction_retry",
        "interactive_predict",
        "predict_test",
        "mask_repair",
        "secondary_inference",
        "frame_predict",
        "frame_interactive",
    }
)
_VIDEO_EXTENSIONS = {
    "mov": "mov",
    "matroska": "mkv",
    "avi": "avi",
    "asf": "asf",
    "flv": "flv",
}
_VIDEO_RELATIVE = re.compile(r"video/[0-9a-f]{64}\.(?:mov|mkv|avi|asf|flv)")
MAX_OBJECTS = 10_000
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_INVENTORY_BYTES = 4 * 1024**2
MAX_SOURCE_BYTES = 1024**2
URL_SECONDS = 60 * 60
IO_SECONDS = 300
CHUNK_BYTES = 256 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_RELATIVE = re.compile(
    r"(?:original/[0-9a-f]{64}\.(?:jpg|png|webp)|crops/(?:0|[1-9][0-9]{0,9})-[0-9a-f]{64}\.jpg)"
)
_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_REF_FIELDS = frozenset(
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
_FACTS = (
    "id",
    "owner_id",
    "owner_kind",
    "task_id",
    "contract_version",
    "bucket",
    "storage_identity",
    "storage_ref",
    "maintenance_revision",
    "created_at",
    "expires_at",
    "retain_until",
    "source_ref",
)
_COLUMNS = tuple(getattr(InputRecord, name) for name in _FACTS) + (
    InputRecord.state,
    InputRecord.uncertain_write,
    InputRecord.next_cleanup_at,
    InputRecord.deleted_at,
    InputRecord.object_count,
    InputRecord.size_bytes,
)


def now():
    return datetime.now(timezone.utc)


def reject(code="invalid_receipt", status=409):
    raise ContentError("prediction_input_" + code, status)


def input_prefix(identity):
    return f"prediction-inputs/{uuid.UUID(str(identity))}/"


def purpose(identity):
    return "prediction_input_" + uuid.UUID(str(getattr(identity, "id", identity))).hex


def make_storage():
    return StorageService(
        client_config=Config(
            connect_timeout=3,
            read_timeout=15,
            retries={"total_max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
            signature_version="s3v4",
        )
    )


def _identity(endpoint, bucket):
    return hashlib.sha256(
        json.dumps([endpoint.rstrip("/"), bucket]).encode()
    ).hexdigest()


def storage_identity(storage):
    return _identity(storage.client.meta.endpoint_url, storage.import_bucket)


def _endpoint():
    return (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


def _header(value, limit=1024):
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value.isascii()
        and all(32 < ord(c) < 127 for c in value)
    )


def _document(value, limit):
    try:
        raw = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError, RecursionError):
        reject()
    if len(raw.encode()) > limit:
        reject("budget_exceeded", 413)
    return deepcopy(value)


def resolve_ref(ref, *, expected_prefix):
    """Validate saved addresses without adopting the current binding."""
    from app.services import import_storage

    try:
        return import_storage.resolve_ref(ref, expected_prefix=expected_prefix).ref
    except import_storage.ImportStorageError:
        reject("legacy_unpinned" if ref is None else "invalid_receipt")


def _location(record):
    return uuid.UUID(_resolve(record)["location_id"])


async def _target_for_record(db, record):
    from app.services import import_storage

    owner = import_storage.resolve_ref(
        _resolve(record), expected_prefix=input_prefix(record.id)
    )
    if owner.location_id == LEGACY_LOCATION_ID:
        return None
    return await import_storage.io_target(db, owner)


def _resolve(record):
    if type(record.contract_version) is not int or record.contract_version not in {
        2,
        3,
    }:
        reject("legacy_unpinned")
    if record.owner_kind not in (
        VIDEO_KINDS if record.contract_version == 3 else IMAGE_KINDS
    ):
        reject("scope_invalid")
    ref = resolve_ref(record.storage_ref, expected_prefix=input_prefix(record.id))
    if (
        record.bucket != ref["bucket"]
        or record.storage_identity != ref["storage_identity"]
        or type(record.maintenance_revision) is not int
        or record.maintenance_revision < 1
    ):
        reject()
    return ref


async def _drain(pending):
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except BaseException:
            continue
    return pending.result()


def _uncertainty(original, later):
    if getattr(later, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(original)


async def settled_io(call, *args, **kwargs):
    """Retain the real Future through every interruption, including soft limits."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await _drain(pending)
        except BaseException as late:
            _uncertainty(interrupted, late)
        raise


async def _async(call, *, late=None):
    pending = asyncio.create_task(call)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            result = await _drain(pending)
        except BaseException as later:
            _uncertainty(interrupted, later)
        else:
            if late is not None:
                try:
                    await late(result)
                except BaseException as closing:
                    _uncertainty(interrupted, closing)
        raise


async def _settle(permit):
    await _async(permit._settle())


@dataclass(frozen=True)
class InputSnapshot:
    id: uuid.UUID
    owner_id: uuid.UUID
    owner_kind: str
    task_id: uuid.UUID
    contract_version: int
    bucket: str
    storage_identity: str
    storage_ref: dict
    maintenance_revision: int
    created_at: datetime
    expires_at: datetime
    retain_until: datetime
    source_ref: dict | None


def _snapshot(row):
    result = InputSnapshot(**{name: deepcopy(getattr(row, name)) for name in _FACTS})
    _resolve(result)
    return result


async def _row(db, identifier, *, lock=False):
    query = (
        select(InputRecord)
        .options(load_only(*_COLUMNS))
        .where(InputRecord.id == identifier)
    )
    if lock:
        query = query.with_for_update(nowait=True)
    row = await db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        reject("expired")
    return row


def _same(row, record):
    return all(getattr(row, name) == getattr(record, name) for name in _FACTS)


async def _allocate_ref(db, identifier):
    from app.services import import_storage

    return await import_storage.allocate(db, input_prefix(identifier))


async def check_metadata(db, inputs, *, lock=True):
    """Join final caller locks: metadata/Gate only, no guard, commit or new session."""
    row = await _row(db, inputs.record.id, lock=lock)
    if (
        inputs._closed
        or inputs._unsettled
        or row.state != "active"
        or row.expires_at <= now()
        or not _same(row, inputs.record)
    ):
        reject("expired")
    _resolve(inputs.record)
    await _target_for_record(db, inputs.record)
    await storage_access.check(
        db,
        _location(inputs.record),
        expected_revision=inputs.record.maintenance_revision,
        lock=lock,
    )
    pending = await db.scalar(
        select(
            func.jsonb_path_exists(
                InputRecord.inventory,
                cast(
                    '$.objects.* ? (@.state != "verified" || @.version_id == null || @.version_id == "null")',
                    JSONPATH,
                ),
            )
        ).where(InputRecord.id == row.id)
    )
    if pending:
        reject("object_not_verified")
    if inputs.record.contract_version == 3:
        await inputs._check_video_inventory(db, row)
    return inputs.record


def _probe(value):
    """Validate the exact H2 descriptor receipt without accepting legacy estimates."""
    if (
        not isinstance(value, dict)
        or set(value) != {"probe_contract", "video"}
        or type(value["probe_contract"]) is not int
        or value["probe_contract"] != 1
        or not isinstance(value["video"], dict)
        or set(value["video"])
        != {"width", "height", "fps", "frame_count", "duration_ms", "codec"}
    ):
        reject("video_probe_invalid")
    info = value["video"]
    try:
        finite_fps = type(info["fps"]) in (int, float) and math.isfinite(info["fps"])
    except OverflowError:
        finite_fps = False
    if (
        any(
            type(info[key]) is not int or not 0 < info[key] <= 2**31 - 1
            for key in ("width", "height")
        )
        or type(info["frame_count"]) is not int
        or not 0 < info["frame_count"] <= 1_000_000
        or type(info["fps"]) not in (int, float)
        or not finite_fps
        or info["fps"] <= 0
        or (
            info["duration_ms"] is not None
            and (
                type(info["duration_ms"]) is not int
                or not 0 <= info["duration_ms"] <= 2**63 - 1
            )
        )
        or not isinstance(info["codec"], str)
        or not 0 < len(info["codec"]) <= 128
    ):
        reject("video_probe_invalid")
    return _document(value, 4096)


def _limits(record):
    _resolve(record)
    return (
        (1, MAX_VIDEO_BYTES)
        if record.contract_version == 3
        else (MAX_OBJECTS, MAX_TOTAL_BYTES)
    )


def _entry(relative, entry, *, contract_version=2):
    video = type(contract_version) is int and contract_version == 3
    if type(contract_version) is not int or contract_version not in {2, 3}:
        reject()
    pattern = _VIDEO_RELATIVE if video else _RELATIVE
    if not isinstance(relative, str) or pattern.fullmatch(relative) is None:
        reject("scope_invalid")
    fields = {
        "sha256",
        "size_bytes",
        "media_type",
        "state",
        "etag",
        "version_id",
        "write_nonce",
    }
    if video:
        fields |= {"container", "probe"}
    if not isinstance(entry, dict) or set(entry) != fields:
        reject()
    if video:
        _probe(entry["probe"])
        if (
            not isinstance(entry["container"], str)
            or entry["container"] not in _VIDEO_EXTENSIONS
            or entry["media_type"] != "application/octet-stream"
        ):
            reject()
        extension = _VIDEO_EXTENSIONS[entry["container"]]
    else:
        extension = (
            _TYPES.get(entry["media_type"])
            if isinstance(entry["media_type"], str)
            else None
        )
    try:
        nonce = uuid.UUID(entry["write_nonce"])
        nonce_valid = str(nonce) == entry["write_nonce"] and nonce.version == 4
    except (TypeError, ValueError, AttributeError):
        nonce_valid = False
    if (
        not nonce_valid
        or not isinstance(entry["sha256"], str)
        or _SHA.fullmatch(entry["sha256"]) is None
        or type(entry["size_bytes"]) is not int
        or not 0 < entry["size_bytes"] <= (MAX_VIDEO_BYTES if video else MAX_BYTES)
        or extension is None
        or entry["state"] not in {"planned", "verified"}
        or not relative.endswith(entry["sha256"] + "." + extension)
    ):
        reject()
    if entry["state"] == "planned":
        if entry["etag"] is not None or entry["version_id"] is not None:
            reject()
    elif (
        (video and entry["version_id"] in (None, "null"))
        or not _header(entry["etag"])
        or (entry["version_id"] is not None and not _header(entry["version_id"]))
    ):
        reject()
    return deepcopy(entry)


def _http(response, statuses=(200,)):
    if (
        not isinstance(response, dict)
        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in statuses
    ):
        reject("storage_unavailable", 503)


def _version(response):
    value = response.get("VersionId")
    if value in (None, "null"):
        return None
    if not _header(value):
        reject("integrity_mismatch")
    return value


def _immutable_version(response):
    version = _version(response)
    if version is None:
        reject("immutable_version_required")
    return version


def _versioned_bucket(client, bucket):
    result = client.get_bucket_versioning(Bucket=bucket)
    _http(result)
    if result.get("Status") != "Enabled":
        reject("bucket_versioning_required", 503)


def _deadline(deadline):
    if time.monotonic() > deadline:
        reject("storage_timeout", 504)


def _owned(response, lease_id, entry):
    metadata = response.get("Metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("aap-prediction-lease") != str(lease_id)
        or metadata.get("aap-prediction-write") != entry["write_nonce"]
    ):
        reject("ownership_mismatch")


def _read(client, bucket, key, entry, deadline, *, lease_id, written=None):
    _deadline(deadline)
    expected = written or (entry if entry["state"] == "verified" else None)
    pinned = None
    if expected is not None:
        pinned = _immutable_version(
            {"VersionId": expected["version_id"]}
            if "version_id" in expected
            else expected
        )
    head = client.head_object(
        Bucket=bucket, Key=key, **({"VersionId": pinned} if pinned else {})
    )
    _http(head)
    _owned(head, lease_id, entry)
    etag, version = head.get("ETag"), _immutable_version(head)
    if (
        not _header(etag)
        or type(head.get("ContentLength")) is not int
        or head["ContentLength"] != entry["size_bytes"]
        or ("container" in entry and head.get("ContentType") != entry["media_type"])
    ):
        reject("integrity_mismatch")
    if expected is not None and (
        expected.get("etag", expected.get("ETag")) != etag or pinned != version
    ):
        reject("integrity_mismatch")
    params = {"Bucket": bucket, "Key": key, "IfMatch": etag, "VersionId": version}
    response = client.get_object(**params)
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        raise storage_access.mark_unsettled(
            ContentError("prediction_input_storage_unavailable", 503)
        )
    with storage_access.closing_registered(body):
        _http(response)
        _owned(response, lease_id, entry)
        if (
            response.get("ETag") != etag
            or _version(response) != version
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != entry["size_bytes"]
            or (
                "container" in entry
                and response.get("ContentType") != entry["media_type"]
            )
        ):
            reject("integrity_mismatch")
        digest, size = hashlib.sha256(), 0
        while True:
            _deadline(deadline)
            block = body.read(min(CHUNK_BYTES, entry["size_bytes"] - size + 1))
            if type(block) is not bytes:
                reject("integrity_mismatch")
            if not block:
                break
            size += len(block)
            if size > entry["size_bytes"]:
                reject("integrity_mismatch")
            digest.update(block)
        if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
            reject("integrity_mismatch")
    _deadline(deadline)
    after = client.head_object(Bucket=bucket, Key=key, VersionId=version)
    _http(after)
    _owned(after, lease_id, entry)
    if (
        after.get("ETag") != etag
        or _version(after) != version
        or type(after.get("ContentLength")) is not int
        or after["ContentLength"] != entry["size_bytes"]
        or ("container" in entry and after.get("ContentType") != entry["media_type"])
    ):
        reject("integrity_mismatch")
    return {**entry, "state": "verified", "etag": etag, "version_id": version}


def _using_storage(record, call, provided=None, *, close=True, target=None):
    ref = _resolve(record)
    if target is not None and provided is None:
        with target.client() as client:
            admitted = object.__new__(StorageService)
            admitted.client = client
            return _using_storage(record, call, admitted, close=False)
    try:
        storage = provided or make_storage()
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        if storage.client.meta.endpoint_url.rstrip("/") != ref["endpoint_url"]:
            reject("storage_identity_changed")
        target = urlsplit(ref["endpoint_url"])

        def same_origin(request, **_kwargs):
            destination = urlsplit(request.url)
            if (destination.scheme, destination.netloc) != (
                target.scheme,
                target.netloc,
            ):
                reject("redirect_denied")

        storage.client.meta.events.register("before-send.s3", same_origin)
        return call(storage)
    finally:
        if close:
            storage_access.close_registered(storage.client)


class PredictionInputs:
    def __init__(self, factory, record, storage=None, *, guard=None):
        self.factory, self.record, self.storage = factory, record, storage
        self.guard = guard
        self.prefix = _resolve(record)["key_prefix"]
        self._objects, self._uploaded = {}, set()
        self._video_receipt = None
        self._snapshot, self._images = None, []
        self._closed = self._uncertain_write = self._unsettled = False
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        factory,
        *,
        owner_id,
        owner_kind,
        task_id,
        storage=None,
        guard=None,
        source_ref=None,
    ):
        if owner_kind not in IMAGE_KINDS | VIDEO_KINDS:
            raise ValueError("unsupported prediction input owner")
        if guard is not None and not callable(guard):
            raise TypeError("guard must be an asynchronous authority callback")
        source_ref = (
            None if source_ref is None else _document(source_ref, MAX_SOURCE_BYTES)
        )
        if source_ref is not None and not isinstance(source_ref, dict):
            reject()
        identifier, started = uuid.uuid4(), now()

        async def allocate():
            async with factory() as db:
                if guard is not None:
                    await guard(db)
                ref = await _allocate_ref(db, identifier)
                revision = await storage_access.check(db, uuid.UUID(ref["location_id"]))
                row = InputRecord(
                    id=identifier,
                    owner_id=uuid.UUID(str(owner_id)),
                    owner_kind=owner_kind,
                    task_id=uuid.UUID(str(task_id)),
                    contract_version=3 if owner_kind in VIDEO_KINDS else 2,
                    storage_ref=ref,
                    bucket=ref["bucket"],
                    storage_identity=ref["storage_identity"],
                    maintenance_revision=revision,
                    source_ref=source_ref,
                    inventory={"schema": 1, "objects": {}},
                    object_count=0,
                    size_bytes=0,
                    state="active",
                    uncertain_write=False,
                    created_at=started,
                    expires_at=started + timedelta(seconds=ACTIVE_SECONDS),
                    retain_until=started
                    + timedelta(seconds=ACTIVE_SECONDS, days=RECOVERY_DAYS),
                    next_cleanup_at=started + timedelta(seconds=ACTIVE_SECONDS),
                )
                db.add(row)
                result = _snapshot(row)
                await db.commit()
                return result

        record = await _async(allocate())
        return cls(factory, record, storage, guard=guard)

    async def _fresh(self, db, *, lock=False):
        row = await _row(db, self.record.id, lock=lock)
        if (
            self._closed
            or self._unsettled
            or row.state != "active"
            or row.expires_at <= now()
        ):
            reject("expired")
        if not _same(row, self.record):
            reject()
        if self.guard is not None:
            await self.guard(db)
        await _target_for_record(db, self.record)
        await storage_access.check(
            db,
            _location(self.record),
            expected_revision=self.record.maintenance_revision,
        )
        return row

    async def check_live(self):
        """Fresh metadata/authority only; call before acquiring final business locks."""

        async def check():
            async with self.factory() as db:
                await self._fresh(db)
                pending = await db.scalar(
                    select(
                        func.jsonb_path_exists(
                            InputRecord.inventory,
                            cast(
                                '$.objects.* ? (@.state != "verified" || @.version_id == null || @.version_id == "null")',
                                JSONPATH,
                            ),
                        )
                    ).where(InputRecord.id == self.record.id)
                )
                if pending:
                    reject("object_not_verified")
                if self.record.contract_version == 3:
                    await self._check_video_inventory(db)

        await _async(check())

    @property
    def video_receipt(self):
        if self.record.contract_version != 3 or self._video_receipt is None:
            reject("object_not_verified")
        return deepcopy(self._video_receipt)

    async def _check_video_inventory(self, db, row=None):
        row = row or await _row(db, self.record.id)
        size = await db.scalar(
            select(func.octet_length(cast(InputRecord.inventory, Text))).where(
                InputRecord.id == self.record.id
            )
        )
        if size is None or size > MAX_INVENTORY_BYTES:
            reject("budget_exceeded", 413)
        value = await db.scalar(
            select(InputRecord.inventory).where(InputRecord.id == self.record.id)
        )
        objects = _inventory(row, value)
        if len(objects) != 1:
            reject("object_not_verified")
        relative, entry = next(iter(objects.items()))
        if entry["state"] != "verified" or entry["version_id"] in {None, "null"}:
            reject("object_not_verified")
        if {"relative": relative, **entry} != self._video_receipt:
            reject("invalid_receipt")

    async def _reserve(self, relative, expected):
        expected = _entry(
            relative, expected, contract_version=self.record.contract_version
        )

        async def reserve():
            async with self.factory() as db:
                row = await self._fresh(db, lock=True)
                old = await db.scalar(
                    select(InputRecord.inventory["objects"][relative]).where(
                        InputRecord.id == row.id
                    )
                )
                if old is not None:
                    old = _entry(
                        relative, old, contract_version=self.record.contract_version
                    )
                    if any(
                        old[k] != expected[k]
                        for k in (
                            set(expected)
                            - {"state", "etag", "version_id", "write_nonce"}
                        )
                    ):
                        reject()
                    return old
                max_objects, max_bytes = _limits(self.record)
                if (
                    row.object_count >= max_objects
                    or row.size_bytes + expected["size_bytes"] > max_bytes
                ):
                    reject("budget_exceeded", 413)
                await db.execute(
                    update(InputRecord)
                    .where(InputRecord.id == row.id)
                    .values(
                        inventory=func.jsonb_set(
                            InputRecord.inventory,
                            array(["objects", relative]),
                            cast(expected, JSONB),
                            True,
                        ),
                        object_count=row.object_count + 1,
                        size_bytes=row.size_bytes + expected["size_bytes"],
                    )
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
                return expected

        entry = await _async(reserve())
        self._objects[relative] = entry["size_bytes"]
        return entry

    async def _acquire(self):
        async with self.factory() as db:
            await self._fresh(db)
            return await storage_access.acquire(
                db,
                _location(self.record),
                purpose(self.record),
                expected_revision=self.record.maintenance_revision,
            )

    async def _io(self, call, *, publish=None):
        permit = await _async(self._acquire(), late=_settle)
        failure, result = None, None
        try:
            await self.check_live_for_write()
            async with self.factory() as db:
                target = await _target_for_record(db, self.record)
            result = await settled_io(
                _using_storage, self.record, call, self.storage, target=target
            )
            await self.check_live_for_write()
            if publish is not None:
                await publish(result)
        except BaseException as exc:
            failure = exc
        finally:
            if not getattr(failure, "storage_operation_unsettled", False):
                try:
                    await _settle(permit)
                except BaseException as closing:
                    if failure is None:
                        failure = closing
                    else:
                        _uncertainty(failure, closing)
        if failure is not None:
            self._unsettled |= getattr(failure, "storage_operation_unsettled", False)
            raise failure
        return result

    async def check_live_for_write(self):
        async def check():
            async with self.factory() as db:
                await self._fresh(db)

        await _async(check())

    def _url(self, storage, relative, verified):
        _entry(relative, verified, contract_version=self.record.contract_version)
        seconds = min(
            URL_SECONDS, int((self.record.expires_at - now()).total_seconds())
        )
        if seconds <= 0:
            reject("expired")
        return storage.generate_download_url(
            self.prefix + relative,
            expires_in=seconds,
            bucket=self.record.bucket,
            align=False,
            version_id=_immutable_version({"VersionId": verified["version_id"]}),
            audience="ml"
            if _location(self.record) == LEGACY_LOCATION_ID
            else "internal",
        )

    async def _publish(self, relative, expected, verified):
        async def publish():
            async with self.factory() as db:
                row = await self._fresh(db, lock=True)
                current = await db.scalar(
                    select(InputRecord.inventory["objects"][relative]).where(
                        InputRecord.id == row.id
                    )
                )
                if current != expected:
                    reject()
                await db.execute(
                    update(InputRecord)
                    .where(InputRecord.id == row.id)
                    .values(
                        inventory=func.jsonb_set(
                            InputRecord.inventory,
                            array(["objects", relative]),
                            cast(verified, JSONB),
                            True,
                        )
                    )
                    .execution_options(synchronize_session=False)
                )
                try:
                    await db.commit()
                except BaseException as exc:
                    # An acknowledgement failure cannot prove which receipt persisted.
                    raise storage_access.mark_unsettled(exc)

        await _async(publish())

    async def _upload(self, relative, content, *, sha256, size, media_type, video=None):
        from app.services import import_storage

        import_storage.resolve_ref(
            self.record.storage_ref, expected_prefix=input_prefix(self.record.id)
        ).key(relative)
        expected = await self._reserve(
            relative,
            dict(
                sha256=sha256,
                size_bytes=size,
                media_type=media_type,
                state="planned",
                etag=None,
                version_id=None,
                write_nonce=str(uuid.uuid4()),
                **(video or {}),
            ),
        )

        handle = (
            await _async(
                accounting.begin(
                    self.factory,
                    self.record,
                    relative,
                    "create",
                    nonce=expected["write_nonce"],
                    new_request=True,
                )
            )
            if expected["state"] == "planned"
            else None
        )
        evidence = {}

        def physical(storage):
            body = io.BytesIO(content) if type(content) is bytes else content
            position = body.tell()
            try:
                deadline = time.monotonic() + IO_SECONDS
                if expected["state"] == "planned":
                    _versioned_bucket(storage.client, self.record.bucket)
                    body.seek(0)
                    digest, count = hashlib.sha256(), 0
                    while True:
                        _deadline(deadline)
                        block = body.read(min(CHUNK_BYTES, size - count + 1))
                        if type(block) is not bytes:
                            reject("snapshot_invalid")
                        if not block:
                            break
                        count += len(block)
                        if count > size:
                            reject("snapshot_invalid")
                        digest.update(block)
                    if count != size or digest.hexdigest() != sha256:
                        reject("snapshot_invalid")
                    body.seek(0)
                    try:
                        written = storage.client.put_object(
                            Bucket=self.record.bucket,
                            Key=self.prefix + relative,
                            Body=body,
                            ContentLength=size,
                            ContentType=media_type,
                            CacheControl="private, max-age=3600, immutable",
                            IfNoneMatch="*",
                            Metadata={
                                "aap-prediction-lease": str(self.record.id),
                                "aap-prediction-write": expected["write_nonce"],
                            },
                        )
                        _http(written)
                        evidence.update(
                            physical_outcome="written",
                            returned_version=_version(written),
                        )
                    except Exception as exc:
                        code = str(
                            getattr(exc, "response", {})
                            .get("Error", {})
                            .get("Code", "")
                        )
                        if code not in {"412", "PreconditionFailed"}:
                            raise
                        written = None
                        evidence.update(
                            physical_outcome="not_written", current_delta_bytes=0
                        )
                else:
                    written = None
                verified = _read(
                    storage.client,
                    self.record.bucket,
                    self.prefix + relative,
                    expected,
                    deadline,
                    lease_id=self.record.id,
                    written=written,
                )
                if written is not None:
                    matched = accounting.current_matches(
                        storage.client,
                        self.record.bucket,
                        self.prefix + relative,
                        verified["version_id"],
                        verified["size_bytes"],
                    )
                    evidence.update(
                        physical_size_bytes=verified["size_bytes"],
                        body_verified=True,
                        current_version_matches=matched,
                        current_delta_bytes=verified["size_bytes"] if matched else None,
                    )
                return verified, self._url(storage, relative, verified)
            finally:
                if type(content) is bytes:
                    storage_access.close_registered(body)
                else:
                    try:
                        body.seek(position)
                    except BaseException as exc:
                        raise storage_access.mark_unsettled(exc)

        try:

            async def publish(result):
                verified, _ = result
                _entry(
                    relative, verified, contract_version=self.record.contract_version
                )
                if expected != verified:
                    await self._publish(relative, expected, verified)

            verified, url = await self._io(physical, publish=publish)
            self._uploaded.add(relative)
            if self.record.contract_version == 3:
                self._video_receipt = {"relative": relative, **deepcopy(verified)}
            return url
        except BaseException as exc:
            self._uncertain_write = True
            self._unsettled |= getattr(exc, "storage_operation_unsettled", False)
            if isinstance(exc, Exception) and not isinstance(exc, ContentError):
                failure = ContentError("prediction_input_upload_failed", 503)
                _uncertainty(failure, exc)
                raise failure from None
            raise
        finally:
            await _async(
                accounting.publish(
                    self.factory, handle, evidence, settled=not self._unsettled
                )
            )

    async def upload_video(self, stage: BinaryStage, *, probe):
        """Borrow one verified descriptor; the source owner retains its real close."""
        if (
            self.record.contract_version != 3
            or self.record.owner_kind not in VIDEO_KINDS
        ):
            reject("video_contract_required")
        probe = _probe(probe)
        if (
            type(stage) is not BinaryStage
            or stage._closed
            or type(stage.size) is not int
            or not 0 < stage.size <= MAX_VIDEO_BYTES
            or not isinstance(stage.sha256, str)
            or _SHA.fullmatch(stage.sha256) is None
        ):
            reject("video_stage_invalid")
        async with self._lock:
            # The caller retains the source permit during this borrowed FD check.
            # Hashing and transport also drain before this method releases it.
            def inspect_stage():
                from app.workers.media_process import _demuxer, decoder_input

                try:
                    _, descriptors = decoder_input(stage)
                    facts = os.fstat(descriptors[0])
                    if not stat.S_ISREG(facts.st_mode) or facts.st_size != stage.size:
                        reject("video_stage_invalid")
                    return _demuxer(descriptors[0])
                except (ValueError, OSError, AttributeError):
                    reject("video_stage_invalid")

            container = await settled_io(inspect_stage)
            return await self._upload(
                f"video/{stage.sha256}.{_VIDEO_EXTENSIONS[container]}",
                stage.file,
                sha256=stage.sha256,
                size=stage.size,
                media_type="application/octet-stream",
                video={"container": container, "probe": probe},
            )

    async def upload_crop(self, task, box_index, content):
        self._image_only()
        if (
            uuid.UUID(str(task.id)) != self.record.task_id
            or type(box_index) is not int
            or not 0 <= box_index <= 9_999_999_999
            or type(content) is not bytes
        ):
            reject("scope_invalid")
        if not 0 < len(content) <= MAX_BYTES:
            reject("too_large", 413)
        digest = hashlib.sha256(content).hexdigest()
        async with self._lock:
            return await self._upload(
                f"crops/{box_index}-{digest}.jpg",
                content,
                sha256=digest,
                size=len(content),
                media_type="image/jpeg",
            )

    async def upload_frame(self, task, content):
        """Use verified frame bytes as an image input without transcoding or crop indexes."""
        self._image_only()
        if uuid.UUID(str(task.id)) != self.record.task_id or type(content) is not bytes:
            reject("scope_invalid")
        if not 0 < len(content) <= MAX_BYTES:
            reject("too_large", 413)
        media_type = (
            "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        )
        digest = hashlib.sha256(content).hexdigest()
        async with self._lock:
            return await self._upload(
                f"original/{digest}.{_TYPES[media_type]}",
                content,
                sha256=digest,
                size=len(content),
                media_type=media_type,
            )

    def _image_only(self):
        if self.record.contract_version != 2:
            reject("image_contract_required")

    def _adopt_snapshot(self, snapshot):
        self._image_only()
        if (
            self._closed
            or self._unsettled
            or self._snapshot is not None
            or not isinstance(snapshot, Snapshot)
            or snapshot._closed
            or snapshot.inspection_error
            or snapshot.media_type not in _TYPES
            or not isinstance(snapshot.sha256, str)
            or _SHA.fullmatch(snapshot.sha256) is None
            or type(snapshot.size) is not int
            or not 0 < snapshot.size <= MAX_BYTES
            or type(snapshot.width) is not int
            or type(snapshot.height) is not int
            or snapshot.width <= 0
            or snapshot.height <= 0
            or snapshot.width * snapshot.height > MAX_PIXELS
        ):
            reject("snapshot_invalid")
        self._snapshot = snapshot

    async def adopt_snapshot(self, snapshot):
        """Own a verified Snapshot for crop-only delivery, without uploading it."""
        async with self._lock:
            self._adopt_snapshot(snapshot)

    async def upload_snapshot(self, snapshot):
        """Transfer ownership of one strictly verified Snapshot, never its source."""
        async with self._lock:
            self._adopt_snapshot(snapshot)
            relative = f"original/{snapshot.sha256}.{_TYPES[snapshot.media_type]}"
            return await self._upload(
                relative,
                snapshot.file,
                sha256=snapshot.sha256,
                size=snapshot.size,
                media_type=snapshot.media_type,
            )

    def load_image(self, task):
        self._image_only()
        if (
            self._closed
            or self._unsettled
            or now() >= self.record.expires_at
            or uuid.UUID(str(task.id)) != self.record.task_id
            or not self._snapshot
            or self._snapshot._closed
        ):
            reject("snapshot_invalid")
        self._snapshot.file.seek(0)
        with Image.open(self._snapshot.file) as image:
            oriented = ImageOps.exif_transpose(image)
            try:
                result = oriented.convert("RGB")
            finally:
                oriented.close()
        if result.size != (self._snapshot.width, self._snapshot.height):
            result.close()
            reject("image_size_mismatch")
        self._images.append(result)
        return result

    async def _seal(self):
        async with self.factory() as db:
            row = await _row(db, self.record.id, lock=True)
            if row.state == "deleted":
                return
            if not _same(row, self.record):
                reject()
            row.state = "cleanup_pending"
            row.uncertain_write |= self._uncertain_write or self._unsettled
            row.next_cleanup_at = now()
            await db.commit()

    async def close(self):
        # Waiting for another upload's lock is also part of physical ownership.
        await _async(self._close())

    async def _close(self):
        async with self._lock:
            if self._closed:
                if self._unsettled:
                    raise storage_access.mark_unsettled(
                        ContentError("prediction_input_close_unknown", 503)
                    )
                return
            self._closed = True
            failure = None
            for resource in [
                *self._images,
                *([self._snapshot] if self._snapshot else []),
            ]:
                try:
                    await settled_io(storage_access.close_registered, resource)
                except BaseException as closing:
                    self._unsettled |= getattr(
                        closing, "storage_operation_unsettled", False
                    )
                    if failure is None:
                        failure = closing
                    else:
                        _uncertainty(failure, closing)
            try:
                await _async(self._seal())
                if not self._unsettled:
                    await purge_input(self.factory, self.record.id)
            except BaseException as exc:
                if failure is None:
                    failure = exc
                else:
                    _uncertainty(failure, exc)
            if failure is not None:
                if self._unsettled:
                    storage_access.mark_unsettled(failure)
                if not isinstance(failure, Exception) or getattr(
                    failure, "storage_operation_unsettled", False
                ):
                    raise failure
                log.warning(
                    "Prediction input cleanup deferred input=%s", self.record.id
                )
            elif self._unsettled:
                raise storage_access.mark_unsettled(
                    ContentError("prediction_input_close_unknown", 503)
                )


@asynccontextmanager
async def prediction_inputs(factory, **owner):
    inputs = await PredictionInputs.create(factory, **owner)
    try:
        yield inputs
    finally:
        await inputs.close()


def _operations(identifier, *, ignore=None):
    query = select(StorageOperation.id).where(
        StorageOperation.purpose == purpose(identifier)
    )
    if ignore is not None:
        query = query.where(StorageOperation.id != ignore)
    return query.exists()


def _inventory(record, value):
    value = _document(value, MAX_INVENTORY_BYTES)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "objects"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or not isinstance(value["objects"], dict)
        or len(value["objects"]) > _limits(record)[0]
    ):
        reject()
    objects = {
        relative: _entry(relative, entry, contract_version=record.contract_version)
        for relative, entry in value["objects"].items()
    }
    if (
        record.object_count != len(objects)
        or record.size_bytes != sum(entry["size_bytes"] for entry in objects.values())
        or record.size_bytes > _limits(record)[1]
    ):
        reject()
    return objects


def _listed(client, record, expected, deadline, *, versions=False):
    prefix = _resolve(record)["key_prefix"]
    rows, markers, seen = {}, {}, set()
    while True:
        _deadline(deadline)
        params = {"Bucket": record.bucket, "Prefix": prefix, "MaxKeys": 1000, **markers}
        response = (
            client.list_object_versions(**params)
            if versions
            else client.list_objects_v2(**params)
        )
        _http(response)
        if type(response.get("IsTruncated")) is not bool:
            reject("integrity_mismatch")
        if versions and response.get("DeleteMarkers"):
            reject("unknown_object")
        values = response.get("Versions" if versions else "Contents", [])
        if not isinstance(values, list):
            reject("integrity_mismatch")
        for item in values:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("Key"), str)
                or not item["Key"].startswith(prefix)
            ):
                reject("unknown_object")
            relative = item["Key"][len(prefix) :]
            entry = expected.get(relative)
            if (
                entry is None
                or relative in rows
                or type(item.get("Size")) is not int
                or item["Size"] != entry["size_bytes"]
                or not _header(item.get("ETag"))
                or (entry["state"] == "verified" and item.get("ETag") != entry["etag"])
            ):
                reject("unknown_object")
            if versions:
                version = _immutable_version(item)
                if entry["state"] == "verified" and version != entry["version_id"]:
                    reject("unknown_object")
            rows[relative] = item
            if len(rows) > MAX_OBJECTS:
                reject("budget_exceeded", 413)
        if not response["IsTruncated"]:
            return rows
        if versions:
            key, version = (
                response.get("NextKeyMarker"),
                response.get("NextVersionIdMarker"),
            )
            if not _header(key) or not _header(version):
                reject("integrity_mismatch")
            next_markers = {"KeyMarker": key, "VersionIdMarker": version}
        else:
            token = response.get("NextContinuationToken")
            if not _header(token, 4096):
                reject("integrity_mismatch")
            next_markers = {"ContinuationToken": token}
        marker = tuple(next_markers.items())
        if marker in seen:
            reject("integrity_mismatch")
        seen.add(marker)
        if len(seen) > MAX_OBJECTS + 1:
            reject("budget_exceeded", 413)
        markers = next_markers


def _prepare_delete_known(storage, record, inventory):
    # Some S3 servers ignore conditional DELETE headers. A saved immutable
    # version is required independently from ETag and content equality.
    if any(
        entry["state"] == "verified" and entry["version_id"] in {None, "null"}
        for entry in inventory.values()
    ):
        reject("immutable_version_required")
    deadline = time.monotonic() + IO_SECONDS
    visible = _listed(storage.client, record, inventory, deadline)
    versions = _listed(storage.client, record, inventory, deadline, versions=True)
    if not set(visible) <= set(versions):
        reject("integrity_mismatch")
    observations = {}
    for relative, item in versions.items():
        observed = _read(
            storage.client,
            record.bucket,
            _resolve(record)["key_prefix"] + relative,
            inventory[relative],
            deadline,
            lease_id=record.id,
            written=item if inventory[relative]["state"] == "planned" else None,
        )
        if (
            observed["version_id"] in {None, "null"}
            or observed["etag"] != item["ETag"]
            or (observed["version_id"] or "null") != item["VersionId"]
        ):
            reject("unknown_object")
        observations[relative] = observed
    return {
        relative: (item, observations[relative]) for relative, item in versions.items()
    }


def _delete_known(storage, record, inventory, prepared, evidence):
    deadline = time.monotonic() + IO_SECONDS
    for relative, (item, observed) in prepared.items():
        _deadline(deadline)
        fact = evidence[relative]
        current_before = accounting.current_matches(
            storage.client,
            record.bucket,
            item["Key"],
            item["VersionId"],
            observed["size_bytes"],
        )
        fact.update(
            physical_size_bytes=observed["size_bytes"], current_before=current_before
        )
        result = storage.client.delete_object(
            Bucket=record.bucket,
            Key=item["Key"],
            VersionId=item["VersionId"],
            IfMatch=observed["etag"],
        )
        _http(result, (200, 204))
        removed = accounting._missing(
            storage.client, record.bucket, item["Key"], version=item["VersionId"]
        )
        current_gone = removed and accounting.deleted_current(
            storage.client, record.bucket, item["Key"], item["VersionId"]
        )
        fact.update(
            physical_outcome="deleted" if removed else "unknown",
            version_removed=removed,
            current_absent=current_gone,
            current_delta_bytes=-observed["size_bytes"]
            if current_before and current_gone
            else None,
        )
    if _listed(storage.client, record, inventory, deadline) or _listed(
        storage.client, record, inventory, deadline, versions=True
    ):
        reject("integrity_mismatch")


async def purge_input(factory, input_id, *, storage=None):
    """Only exact saved inventory may be removed; unknown operations never age out."""
    input_id = uuid.UUID(str(input_id))
    record, inventory, permit = None, None, None
    accounting_handles, accounting_evidence, physical_holder = {}, {}, {}

    async def close_physical():
        target = physical_holder.pop("storage", None)
        if target is not None:
            context = physical_holder.pop("context", None)
            if context is not None:
                await settled_io(context.__exit__, None, None, None)
            else:
                await settled_io(storage_access.close_registered, target.client)

    async def select_owner():
        async with factory() as db:
            row = await db.scalar(
                select(InputRecord)
                .options(load_only(*_COLUMNS))
                .where(InputRecord.id == input_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            checked = now()
            if (
                row is None
                or row.state == "deleted"
                or row.next_cleanup_at > checked
                or (row.state == "active" and row.expires_at > checked)
            ):
                return None
            row.next_cleanup_at = checked + timedelta(seconds=RETRY_SECONDS)
            row.last_cleanup_at = checked
            if row.contract_version not in {2, 3} or row.storage_ref is None:
                row.error_code = "legacy_unpinned"
                await db.commit()
                return "deferred"
            if await db.scalar(select(_operations(input_id))):
                row.error_code = "storage_operation_unsettled"
                await db.commit()
                return "deferred"
            if row.state == "active":
                row.state, row.uncertain_write = "cleanup_pending", True
            size = await db.scalar(
                select(func.octet_length(cast(InputRecord.inventory, Text))).where(
                    InputRecord.id == input_id
                )
            )
            if size is None or size > MAX_INVENTORY_BYTES:
                reject("budget_exceeded", 413)
            document = await db.scalar(
                select(InputRecord.inventory).where(InputRecord.id == input_id)
            )
            objects = _inventory(row, document)
            saved = _snapshot(row)
            await db.commit()
            return saved, objects

    async def fresh(*, lock=False):
        async with factory() as db:
            row = await _row(db, input_id, lock=lock)
            if (
                row.state != "cleanup_pending"
                or not _same(row, record)
                or await db.scalar(select(_operations(input_id, ignore=permit.id)))
            ):
                reject("cleanup_busy")
            await _target_for_record(db, record)
            await storage_access.check(
                db, _location(record), expected_revision=permit.revision
            )
            return row

    async def error(exc):
        async with factory() as db:
            row = await _row(db, input_id, lock=True)
            if row.state != "deleted":
                row.error_code = getattr(
                    exc, "code", "prediction_input_storage_cleanup_failed"
                )
                if getattr(exc, "storage_operation_unsettled", False):
                    row.uncertain_write = True
                await db.commit()

    selected = await _async(select_owner())
    if selected is None or isinstance(selected, str):
        return selected or "skipped"
    record, inventory = selected
    failure, outcome = None, "deferred"
    try:

        async def acquire():
            async with factory() as db:
                return await storage_access.acquire(
                    db, _location(record), purpose(record)
                )

        permit = await _async(acquire(), late=_settle)
        await _async(fresh(lock=True))

        async with factory() as db:
            current_target = await _target_for_record(db, record)

        def prepare():
            if storage is not None or current_target is None:
                target = storage if storage is not None else make_storage()
            else:
                context = current_target.client()
                client = context.__enter__()
                target = object.__new__(StorageService)
                target.client = client
                physical_holder["context"] = context
            physical_holder["storage"] = target
            return _using_storage(
                record,
                lambda client: _prepare_delete_known(client, record, inventory),
                target,
                close=False,
            )

        prepared = await settled_io(prepare)
        for relative, (item, _) in prepared.items():
            accounting_handles[relative] = await _async(
                accounting.begin(
                    factory, record, relative, "delete", version=item["VersionId"]
                )
            )
            accounting_evidence[relative] = {}
        await _async(fresh(lock=True))
        await settled_io(
            _using_storage,
            record,
            lambda client: _delete_known(
                client, record, inventory, prepared, accounting_evidence
            ),
            physical_holder["storage"],
            close=False,
        )
        # Preserve the original lifecycle contract: publishing deleted is only
        # allowed after the native client really closed, while its permit lives.
        await close_physical()
        await _async(fresh(lock=True))

        async def finish():
            async with factory() as db:
                row = await _row(db, input_id, lock=True)
                if (
                    row.state != "cleanup_pending"
                    or not _same(row, record)
                    or await db.scalar(select(_operations(input_id, ignore=permit.id)))
                ):
                    reject("cleanup_busy")
                await _target_for_record(db, record)
                await storage_access.check(
                    db, _location(record), expected_revision=permit.revision, lock=True
                )
                if row.uncertain_write and row.retain_until > now():
                    result = "retained"
                else:
                    row.state, row.deleted_at = "deleted", now()
                    result = "removed"
                row.error_code = None
                await db.commit()
                return result

        outcome = await _async(finish())
    except BaseException as exc:
        failure = exc
        try:
            await _async(error(exc))
        except BaseException as later:
            _uncertainty(failure, later)
    finally:
        try:
            await close_physical()
        except BaseException as closing:
            if failure is None:
                failure = closing
            else:
                _uncertainty(failure, closing)
            try:
                await _async(error(failure))
            except BaseException as later:
                _uncertainty(failure, later)
        if permit is not None and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            try:
                await _settle(permit)
            except BaseException as closing:
                if failure is None:
                    failure = closing
                else:
                    _uncertainty(failure, closing)
    for relative, handle in accounting_handles.items():
        await _async(
            accounting.publish(
                factory,
                handle,
                accounting_evidence[relative],
                settled=not getattr(failure, "storage_operation_unsettled", False),
            )
        )
    if failure is not None:
        if not isinstance(failure, Exception) or getattr(
            failure, "storage_operation_unsettled", False
        ):
            raise failure
        log.warning("Prediction input deletion deferred input=%s", input_id)
    return outcome


async def maintain(factory, *, limit=50):
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(InputRecord.id)
                    .where(
                        InputRecord.state != "deleted",
                        InputRecord.next_cleanup_at <= now(),
                        or_(
                            InputRecord.state == "cleanup_pending",
                            InputRecord.expires_at <= now(),
                        ),
                    )
                    .order_by(InputRecord.next_cleanup_at, InputRecord.id)
                    .limit(limit)
                )
            ).all()
        )
    counts = {"removed": 0, "retained": 0, "deferred": 0, "skipped": 0}
    for identifier in ids:
        try:
            counts[await purge_input(factory, identifier)] += 1
        except Exception:
            counts["deferred"] += 1
            log.warning("Prediction input recovery deferred input=%s", identifier)
    return counts
