"""Pinned pyramid originals, explicit delegation and physically drained reads.

The derivative's owner may be a shared DatasetItem while HTTP authority remains
bound to the initiating Task. Neither a path nor a missing authorization grants
system authority. Metadata helpers never commit the caller's transaction.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import partial
import hashlib
import json
import re
import threading
import uuid

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import managed_original_reader as reader
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.directory_policy import owns_media
from app.services.reference_keys import is_reference

_SHA = re.compile(r"[0-9a-f]{64}")
_slots = threading.BoundedSemaphore(2)
_RECEIPT_FIELDS = {
    "schema",
    "owner_kind",
    "content_id",
    "sha256",
    "size_bytes",
    "location_id",
    "location_revision",
    "maintenance_revision",
    "binding",
}
_BASE_FIELDS = {"schema", "mode", "owner_kind", "owner_id", "original", "task_binding"}
_HTTP_FIELDS = {"task_id", "actor_id", "api_key_id", "api_key_fingerprint"}


def _reject(code="pyramid_authority_unrecorded", status=403):
    raise ContentError(code, status)


def _json_default(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported pyramid identity fact")


def _encoded(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _uuid(value):
    try:
        parsed = uuid.UUID(value)
        return parsed if str(parsed) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def key_fingerprint(key):
    """Bind hash rotation, exact scope ordering and expiration without disclosure."""
    return _digest([key[0], list(key[1]), key[2]] if key is not None else None)


def receipt(source):
    """Record canonical identity without publishing its saved physical locator."""
    if not isinstance(source, original_sources.OriginalSource):
        _reject("original_input_unsupported", 409)
    if is_reference(source.object_key):
        _reject("reference_operation_not_supported", 409)
    if (
        source.media_type != "image"
        or not isinstance(source.owner_kind, str)
        or source.owner_kind not in {"dataset_asset", "task_original"}
        or not isinstance(source.content_id, uuid.UUID)
        or not isinstance(source.location_id, uuid.UUID)
        or source.bucket is None
        or source.endpoint_url is None
        or type(source.size_bytes) is not int
        or not 0 < source.size_bytes <= settings.image_pyramid_max_source_bytes
        or not isinstance(source.sha256, str)
        or _SHA.fullmatch(source.sha256) is None
        or type(source.location_revision) is not int
        or source.location_revision < 1
        or type(source.maintenance_revision) is not int
        or source.maintenance_revision < 1
    ):
        _reject("original_input_unsupported", 409)
    # Validate the saved locator, never infer one from today's purpose buckets.
    reader.ObjectSpec(
        source.endpoint_url,
        source.bucket,
        source.object_key,
        source.sha256,
        source.size_bytes,
    )
    try:
        # HTTP Task relations are checked separately. This lets the same original
        # be reloaded by its canonical DatasetItem derivative owner in a worker.
        binding = _digest(asdict(replace(source, relation=None)))
    except (TypeError, ValueError):
        _reject("original_input_unsupported", 409)
    return {
        "schema": 1,
        "owner_kind": source.owner_kind,
        "content_id": str(source.content_id),
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "location_id": str(source.location_id),
        "location_revision": source.location_revision,
        "maintenance_revision": source.maintenance_revision,
        "binding": binding,
    }


def validate_receipt(value):
    valid = (
        isinstance(value, dict)
        and set(value) == _RECEIPT_FIELDS
        and type(value["schema"]) is int
        and value["schema"] == 1
        and isinstance(value["owner_kind"], str)
        and value["owner_kind"] in {"dataset_asset", "task_original"}
        and _uuid(value["content_id"]) is not None
        and _uuid(value["location_id"]) is not None
        and type(value["size_bytes"]) is int
        and 0 < value["size_bytes"] <= settings.image_pyramid_max_source_bytes
        and all(
            type(value[name]) is int and value[name] >= 1
            for name in ("location_revision", "maintenance_revision")
        )
        and all(
            isinstance(value[name], str) and _SHA.fullmatch(value[name]) is not None
            for name in ("sha256", "binding")
        )
    )
    if not valid:
        _reject("original_input_unrecorded", 409)
    return dict(value)


def _task_binding(source):
    return _digest(asdict(source.relation)) if source.relation is not None else None


async def pin(db, kind, identifier, *, lock=False):
    """Resolve authoritative managed images, including canonical primary links."""
    if (
        not isinstance(kind, str)
        or kind not in {"dataset_item", "task"}
        or not isinstance(identifier, uuid.UUID)
    ):
        _reject("original_input_unrecorded", 409)
    if kind == "task":
        relation = await original_sources.resolve_task_relation(
            db, identifier, lock=lock
        )
        if await owns_media(db, relation.item_id):
            _reject("directory_cache_not_supported", 409)
        source = await original_sources.resolve_task(db, identifier, lock=lock)
    else:
        if await owns_media(db, identifier):
            _reject("directory_cache_not_supported", 409)
        source = await original_sources.resolve_item(db, identifier, lock=lock)
    if await owns_media(db, source.item_id):
        _reject("directory_cache_not_supported", 409)
    receipt(source)
    return source


@dataclass(frozen=True)
class Authority:
    mode: str
    owner_kind: str
    owner_id: uuid.UUID
    expected: str = field(repr=False)
    task_binding: str | None
    actor_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    key: tuple | None = field(default=None, repr=False)
    proof: object = field(default=None, repr=False)

    @property
    def task_id(self):
        return self.owner_id if self.owner_kind == "task" else None

    @property
    def original(self):
        return json.loads(self.expected)


def clock(authority):
    if authority.mode == "http":
        access.check_clock(authority.proof, authority.key)


async def check(db, authority, *, lock=False):
    """Refresh exact credentials, visibility and original; keep caller ownership."""
    if not isinstance(authority, Authority):
        _reject()
    if not isinstance(authority.mode, str) or authority.mode not in {"http", "system"}:
        _reject()
    # Credential helpers lock User/Key even during read-only checks. A connection
    # savepoint keeps NOWAIT conflicts from poisoning or flushing caller work.
    async with original_sources._reads(db, True):
        if authority.mode == "http":
            if authority.owner_kind != "task" or authority.actor_id is None:
                _reject()
            actor, key = await access.actor_and_key(
                db,
                authority.actor_id,
                authority.key_id,
                "annotations:read",
                authority.proof,
            )
            if key != authority.key:
                raise HTTPException(403, {"code": "api_key_authority_revoked"})
            await access.visible_task(db, authority.owner_id, actor, lock=lock)
        source = await pin(db, authority.owner_kind, authority.owner_id, lock=lock)
        if (
            receipt(source) != validate_receipt(json.loads(authority.expected))
            or _task_binding(source) != authority.task_binding
        ):
            _reject("content_binding_changed", 409)
        clock(authority)
        return source


async def authorize(db, task_id, actor, request):
    if not isinstance(request, Request) or not isinstance(task_id, uuid.UUID):
        _reject()
    async with original_sources._reads(db, True):
        proof = await access.capture_credentials(db, actor, request)
        if proof is None or proof.actor_id != actor.id:
            _reject()
        # Pin only after the existing Task read rule succeeds. Never demand Dataset
        # management or writable lifecycle state for an authorized Task derivative.
        fresh_actor, key = await access.actor_and_key(
            db, actor.id, proof.key_id, "annotations:read", proof
        )
        await access.visible_task(db, task_id, fresh_actor, lock=True)
        source = await pin(db, "task", task_id, lock=True)
    authority = Authority(
        "http",
        "task",
        task_id,
        _encoded(receipt(source)),
        _task_binding(source),
        actor.id,
        proof.key_id,
        key,
        proof,
    )
    clock(authority)
    return authority


def authorization(authority):
    if not isinstance(authority, Authority):
        _reject()
    value = {
        "schema": 1,
        "mode": authority.mode,
        "owner_kind": authority.owner_kind,
        "owner_id": str(authority.owner_id),
        "original": validate_receipt(json.loads(authority.expected)),
        "task_binding": authority.task_binding,
    }
    if authority.mode == "http":
        value.update(
            {
                "task_id": str(authority.owner_id),
                "actor_id": str(authority.actor_id),
                "api_key_id": str(authority.key_id) if authority.key_id else None,
                "api_key_fingerprint": key_fingerprint(authority.key),
            }
        )
    elif authority.mode != "system":
        _reject()
    return value


def system_authorization(kind, identifier, source):
    """Only trusted internal call sites may construct a system delegation."""
    expected = receipt(source)
    if (
        not isinstance(kind, str)
        or kind not in {"dataset_item", "task"}
        or not isinstance(identifier, uuid.UUID)
        or (
            kind == "dataset_item"
            and (source.item_id != identifier or source.relation is not None)
        )
        or (
            kind == "task"
            and (source.relation is None or source.relation.task_id != identifier)
        )
    ):
        _reject()
    return authorization(
        Authority("system", kind, identifier, _encoded(expected), _task_binding(source))
    )


async def delegated(db, value):
    """Revalidate a mandatory, strictly parsed queue reference without JWT data."""
    if not isinstance(value, dict):
        _reject()
    mode, kind = value.get("mode"), value.get("owner_kind")
    if (
        not isinstance(mode, str)
        or mode not in {"http", "system"}
        or not isinstance(kind, str)
        or kind not in {"dataset_item", "task"}
        or set(value)
        != (_BASE_FIELDS | _HTTP_FIELDS if mode == "http" else _BASE_FIELDS)
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or _uuid(value["owner_id"]) is None
        or (
            kind == "task"
            and (
                not isinstance(value["task_binding"], str)
                or _SHA.fullmatch(value["task_binding"]) is None
            )
        )
        or (kind == "dataset_item" and value["task_binding"] is not None)
    ):
        _reject()
    actor_id, key_id, key = None, None, None
    if mode == "http":
        if (
            kind != "task"
            or value["task_id"] != value["owner_id"]
            or _uuid(value["actor_id"]) is None
            or (value["api_key_id"] is not None and _uuid(value["api_key_id"]) is None)
            or not isinstance(value["api_key_fingerprint"], str)
            or _SHA.fullmatch(value["api_key_fingerprint"]) is None
        ):
            _reject()
        actor_id = _uuid(value["actor_id"])
        key_id = _uuid(value["api_key_id"]) if value["api_key_id"] else None
        async with original_sources._reads(db, True):
            _actor, key = await access.actor_and_key(
                db, actor_id, key_id, "annotations:read"
            )
        if key_fingerprint(key) != value["api_key_fingerprint"]:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
    authority = Authority(
        mode,
        kind,
        _uuid(value["owner_id"]),
        _encoded(validate_receipt(value["original"])),
        value["task_binding"],
        actor_id,
        key_id,
        key,
    )
    await check(db, authority)
    return authority


async def physical(call, *args, timeout=None, **kwargs):
    """Do not release source files or permits before real native work settles."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args, **kwargs)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return (
            await asyncio.shield(pending)
            if timeout is None
            else await asyncio.wait_for(asyncio.shield(pending), timeout)
        )
    except BaseException as interrupted:
        try:
            await storage_access.finish(pending)
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        raise


class Admission(threading.Semaphore):
    def __init__(self):
        super().__init__(0)
        self.retained = False

    def acquire(self, *args, **kwargs):
        return _slots.acquire(*args, **kwargs)

    def release(self):
        if not self.retained:
            _slots.release()


@asynccontextmanager
async def _session(db):
    async with AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    ) as owned:
        yield owned


async def _fresh(db, authority, source, guard):
    async with _session(db) as owned:
        current = await check(owned, authority, lock=True)
        if receipt(current) != receipt(source):
            _reject("content_binding_changed", 409)
        if guard is not None:
            await guard()
        clock(authority)
    # The owned read-only transaction closes before any physical operation.


async def _capture(source, admission):
    timeout = settings.image_pyramid_job_timeout_seconds
    pending = asyncio.create_task(
        reader.capture(
            reader.ObjectSpec(
                source.endpoint_url,
                source.bucket,
                source.object_key,
                source.sha256,
                source.size_bytes,
            ),
            max_bytes=settings.image_pyramid_max_source_bytes,
            timeout_seconds=timeout,
            transport_timeout_seconds=min(30, timeout),
            admission=admission,
        )
    )
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        # A Celery soft time limit is an ordinary exception, not task cancellation.
        # Ask the reader to stop, then drain its physical handles just as for an
        # asyncio timeout. The outer task must never abandon an unobserved stage.
        pending.cancel()
        try:
            late_stage = await storage_access.finish(pending)
        except BaseException as late:
            if getattr(late, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        else:
            try:
                await physical(late_stage.close)
            except BaseException as closing:
                if getattr(closing, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(interrupted)
        raise


@asynccontextmanager
async def media(db, authority, source, *, guard=None):
    """Yield a verified file while retaining admission through native/close work."""
    receipt(source)
    await _fresh(db, authority, source, guard)
    async with _session(db) as owned:
        permit = await storage_access.acquire(
            owned,
            source.location_id,
            "image_pyramid_original",
            expected_revision=source.maintenance_revision,
        )
    async with permit:
        stage, failure = None, None
        admission = Admission()
        try:
            await _fresh(db, authority, source, guard)
            stage = await _capture(source, admission)
            yield stage
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if getattr(failure, "storage_operation_unsettled", False):
                admission.retained = True
            if stage is not None:
                try:
                    await physical(stage.close)
                except BaseException as closing:
                    if getattr(failure, "storage_operation_unsettled", False):
                        storage_access.mark_unsettled(closing)
                    raise
            if failure is None:
                await _fresh(db, authority, source, guard)
