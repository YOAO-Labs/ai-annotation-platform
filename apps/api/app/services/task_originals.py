"""Publish a durable standalone original before attaching its business pointer.

This service performs no physical I/O. Source ETag/version facts describe the
creation receipt; readers after an offline restore must observe the target's
current identity and verify its bytes instead of replaying those conditions.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, make_dataclass
from datetime import datetime
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_original import TaskOriginal
from app.services import managed_original_creation as creation
from app.services import annotations_storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

FACTS = (
    "id",
    "creation_id",
    "task_id",
    "project_id",
    "location_id",
    "storage_identity",
    "bucket",
    "object_key",
    "sha256",
    "size_bytes",
    "etag",
    "version_id",
    "created_at",
)
_SHA = re.compile(r"[0-9a-f]{64}")


def _reject(code="invalid_receipt", status=409):
    raise ContentError("task_original_" + code, status)


def _get(row, name):
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _safe(value, maximum, *, spaces=True):
    return (
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value.isascii()
        and not any(
            ord(char) < (32 if spaces else 33) or ord(char) == 127 for char in value
        )
    )


def _endpoint(value):
    if not _safe(value, 2048, spaces=False) or "\\" in value or value.endswith("/"):
        _reject("invalid_location")
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
            or parsed.port == 0
        ):
            _reject("invalid_location")
    except ValueError:
        _reject("invalid_location")
    return value


def key_for(task_id, original_id):
    if not isinstance(task_id, uuid.UUID) or not isinstance(original_id, uuid.UUID):
        _reject()
    return f"__aap_task_originals__/{task_id}/{original_id}/payload"


@dataclass(frozen=True)
class Locator:
    location_id: uuid.UUID
    storage_identity: str
    bucket: str
    object_key: str
    endpoint_url: str


def resolve(row, *, endpoint_url=None, storage_ref=None):
    """Validate saved facts without accessing business rows, gates or objects."""
    if row is None:
        _reject("unpinned")
    identifiers = [
        _get(row, field)
        for field in ("id", "creation_id", "task_id", "project_id", "location_id")
    ]
    if (
        any(not isinstance(value, uuid.UUID) for value in identifiers)
        or identifiers[0] != identifiers[1]
        or not isinstance(_get(row, "sha256"), str)
        or _SHA.fullmatch(_get(row, "sha256")) is None
        or type(_get(row, "size_bytes")) is not int
        or not 0 <= _get(row, "size_bytes") <= 2**63 - 1
        or not _safe(_get(row, "etag"), 1024)
        or (
            _get(row, "version_id") is not None
            and (
                not _safe(_get(row, "version_id"), 1024)
                or _get(row, "version_id") == "null"
            )
        )
    ):
        _reject()
    timestamp = _get(row, "created_at")
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        _reject()
    bucket, identity = _get(row, "bucket"), _get(row, "storage_identity")
    if (
        not _safe(bucket, 255, spaces=False)
        or any(char in bucket for char in "/\\?#@")
        or not isinstance(identity, str)
        or _SHA.fullmatch(identity) is None
    ):
        _reject("invalid_location")
    reference = storage_ref if storage_ref is not None else _get(row, "storage_ref")
    logical = key_for(identifiers[2], identifiers[0])
    if reference is None:
        if identifiers[4] != LEGACY_LOCATION_ID or _get(row, "object_key") != logical:
            _reject()
        endpoint = _endpoint(
            endpoint_url
            if endpoint_url is not None
            else annotations_storage._runtime_endpoint()
        )
        if identity != managed_storage_identity(endpoint, bucket):
            _reject("invalid_location")
    else:
        try:
            saved = annotations_storage.resolve_ref(reference, logical)
        except annotations_storage.AnnotationsStorageError:
            _reject("invalid_location")
        if (
            saved.location_id != identifiers[4]
            or saved.bucket != bucket
            or saved.object_key != _get(row, "object_key")
            or saved.storage_identity != identity
            or (endpoint_url is not None and endpoint_url != saved.endpoint_url)
        ):
            _reject("invalid_location")
        endpoint = saved.endpoint_url
    return Locator(identifiers[4], identity, bucket, _get(row, "object_key"), endpoint)


_Snapshot = make_dataclass(
    "TaskOriginalSnapshot", [(name, object) for name in FACTS], frozen=True
)


def snapshot(row, *, endpoint_url=None, storage_ref=None):
    resolve(row, endpoint_url=endpoint_url, storage_ref=storage_ref)
    return _Snapshot(*(_get(row, name) for name in FACTS))


def same(row, receipt):
    return all(_get(row, name) == _get(receipt, name) for name in FACTS)


async def _persist(db, values):
    """Never acquire the caller's Task, Project, User or maintenance locks."""
    async with storage_access.engine(db).begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
        table = TaskOriginal.__table__
        existing = (
            (await connection.execute(select(table).where(table.c.id == values["id"])))
            .mappings()
            .one_or_none()
        )
        if existing is None:
            await connection.execute(
                insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[table.c.id])
            )
            existing = (
                (
                    await connection.execute(
                        select(table).where(table.c.id == values["id"])
                    )
                )
                .mappings()
                .one()
            )
        if not same(existing, values) or existing["storage_ref"] != values.get(
            "storage_ref"
        ):
            _reject("creation_conflict")
        return dict(existing)


async def _durable_owner(db, values):
    operation = asyncio.create_task(_persist(db, values))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        # Drain the commit/rollback even under repeated cancellation. An owner
        # may have committed despite losing its acknowledgement; never delete it.
        try:
            await storage_access.finish(operation)
        except BaseException:
            pass
        raise
    except DBAPIError as exc:
        if storage_access.unavailable(exc):
            _reject("publish_busy")
        _reject("publish_unknown", 503)
    except ContentError:
        raise
    except Exception:
        _reject("publish_unknown", 503)


async def publish(db, task, allocation, created):
    """Caller owns final authorization and the exact Task UPDATE lock.

    The independent owner commits first. The pointer is then flushed only in
    the caller's transaction, together with its eventual business status update.
    """
    row = await creation._loaded(db, allocation)
    if (
        row.get("owner_kind") != "task"
        or row.get("dataset_id") is not None
        or row.get("origin") != "direct"
        or row.get("status") != "verified"
        or row.get("task_id") != task.id
        or row.get("project_id") != task.project_id
        or row.get("object_key") != task.file_path
        or task.dataset_item_id is not None
        or task.original_id not in (None, row["id"])
        or any(
            getattr(created, name, None) != row[field]
            for name, field in (
                ("creation_id", "id"),
                ("sha256", "sha256"),
                ("size_bytes", "size_bytes"),
                ("md5", "md5"),
                ("etag", "etag"),
                ("version_id", "version_id"),
            )
        )
        or await db.scalar(
            select(TaskDatasetItemLink.id)
            .where(TaskDatasetItemLink.task_id == task.id)
            .limit(1)
        )
        is not None
    ):
        _reject("creation_conflict")
    values = {
        field: row[field]
        for field in FACTS
        if field not in {"creation_id", "created_at"}
    }
    values.update(
        creation_id=row["id"],
        created_at=row["completed_at"],
        storage_ref=creation.original_ref(row),
    )
    resolve(values)
    # These are the caller's short final configuration/Gate locks. The separate
    # owner transaction only references already committed immutable evidence.
    await creation._validate(db, row, lock=True)
    saved = await _durable_owner(db, values)
    owner = TaskOriginal(**saved)
    task.original_id = owner.id
    await db.flush()
    return owner
