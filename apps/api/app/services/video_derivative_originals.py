"""Pinned video inputs and explicit authority through physically drained work.

Derived dimensions are output facts, not source identity. Every other saved
locator, content and Task/Item relationship remains part of the input receipt.
No helper commits the caller's business transaction or invents system access.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import comment_attachment_authority as credentials
from app.services import managed_original_reader as reader
from app.services import original_content_access as access
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.directory_policy import owns_media
from app.services.reference_keys import is_reference

MAX_SOURCE_BYTES = 512 * 1024**2
COPY_SECONDS = 300
JOB_SECONDS = 1800
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
_BASE_FIELDS = {
    "schema",
    "mode",
    "policy",
    "owner_kind",
    "owner_id",
    "original",
    "task_binding",
    "scope_binding",
    "lifecycle_revision",
}
_HTTP_FIELDS = {"actor_id", "api_key_id", "api_key_fingerprint"}


def reject(code="video_derivative_authority_unrecorded", status=403):
    raise ContentError(code, status)


def _json_default(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported video original identity fact")


def encoded(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
        return parsed if str(parsed) == value else None
    except (ValueError, TypeError, AttributeError):
        return None


def key_fingerprint(key):
    return digest([key[0], list(key[1]), key[2]] if key is not None else None)


def receipt(source):
    if not isinstance(source, original_sources.OriginalSource):
        reject("video_derivative_input_unsupported", 409)
    if is_reference(source.object_key):
        reject("reference_operation_not_supported", 409)
    if (
        source.owner_kind != "dataset_asset"
        or source.media_type != "video"
        or not isinstance(source.item_id, uuid.UUID)
        or not isinstance(source.content_id, uuid.UUID)
        or not isinstance(source.location_id, uuid.UUID)
        or source.bucket is None
        or source.endpoint_url is None
        or type(source.size_bytes) is not int
        or not 0 < source.size_bytes <= MAX_SOURCE_BYTES
        or not isinstance(source.sha256, str)
        or not _SHA.fullmatch(source.sha256)
        or type(source.location_revision) is not int
        or source.location_revision < 1
        or type(source.maintenance_revision) is not int
        or source.maintenance_revision < 1
        or not isinstance(source.item_facts, original_sources.ItemFacts)
    ):
        reject("video_derivative_input_unsupported", 409)
    reader.ObjectSpec(
        source.endpoint_url,
        source.bucket,
        source.object_key,
        source.sha256,
        source.size_bytes,
    )
    # A metadata publication writes these dimensions itself. They cannot also
    # serve as immutable input facts without invalidating its own publication.
    pinned = replace(
        source,
        relation=None,
        item_facts=replace(source.item_facts, width=None, height=None),
    )
    try:
        binding = digest(asdict(pinned))
    except (TypeError, ValueError):
        reject("video_derivative_input_unsupported", 409)
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
    if not (
        isinstance(value, dict)
        and set(value) == _RECEIPT_FIELDS
        and type(value["schema"]) is int
        and value["schema"] == 1
        and value["owner_kind"] == "dataset_asset"
        and _uuid(value["content_id"]) is not None
        and _uuid(value["location_id"]) is not None
        and type(value["size_bytes"]) is int
        and 0 < value["size_bytes"] <= MAX_SOURCE_BYTES
        and all(
            type(value[k]) is int and value[k] >= 1
            for k in ("location_revision", "maintenance_revision")
        )
        and all(
            isinstance(value[k], str) and _SHA.fullmatch(value[k])
            for k in ("sha256", "binding")
        )
    ):
        reject("video_derivative_input_unrecorded", 409)
    return dict(value)


def _task_binding(source):
    return digest(asdict(source.relation)) if source.relation is not None else None


async def pin(db, kind, identifier, *, lock=False):
    if (
        not isinstance(kind, str)
        or kind not in {"dataset_item", "task"}
        or not isinstance(identifier, uuid.UUID)
    ):
        reject("video_derivative_input_unrecorded", 409)
    if kind == "task":
        relation = await original_sources.resolve_task_relation(
            db, identifier, lock=lock
        )
        if relation.direct_item_id is None or relation.file_type != "video":
            reject("video_derivative_requires_item", 409)
        source = await original_sources.resolve_task(db, identifier, lock=lock)
    else:
        source = await original_sources.resolve_item(db, identifier, lock=lock)
    if await owns_media(db, source.item_id):
        reject("directory_cache_not_supported", 409)
    receipt(source)
    return source


@dataclass(frozen=True)
class Authority:
    mode: str
    policy: str
    owner_kind: str
    owner_id: uuid.UUID
    expected: str = field(repr=False)
    task_binding: str | None
    scope_binding: str | None = None
    lifecycle_revision: int | None = None
    actor_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    key: tuple | None = field(default=None, repr=False)
    proof: object = field(default=None, repr=False)

    @property
    def original(self):
        return json.loads(self.expected)


def clock(authority):
    if authority.mode == "http":
        access.check_clock(authority.proof, authority.key)


async def _actor_and_key(db, actor_id, key_id, proof=None):
    # Video read endpoints intentionally have no additional read scope. Initial
    # route dependencies remain authoritative; exact key facts still bind work.
    try:
        actor = (
            await credentials.reauthorize(db, proof)
            if proof is not None
            else await credentials._actor(db, actor_id, frozenset(PlatformRole))
        )
        key = await credentials._key(db, actor_id, key_id)
    except HTTPException as exc:
        raise access.credential_error(exc) from None
    return actor, key


async def _policy(db, policy, kind, identifier, actor, *, lock=False):
    if policy == "task" and kind == "task":
        await access.visible_task(db, identifier, actor, lock=lock)
        return None, None
    if kind != "dataset_item" or policy not in {"dataset", "storage"}:
        reject()
    source = await pin(db, kind, identifier, lock=lock)
    if policy == "dataset":
        from app.services.dataset_source_access import (
            assert_dataset_manage,
            has_directory_source,
            require_manager,
        )
        from app.services.dataset_lifecycle import require_writable

        dataset_id = source.item_facts.dataset_id
        await require_manager(db, actor.id)
        if await has_directory_source(db, dataset_id):
            await assert_dataset_manage(db, dataset_id, actor)
        dataset = await require_writable(db, dataset_id, lock=lock)
        return digest(
            [str(dataset.id), str(dataset.created_by)]
        ), dataset.lifecycle_revision
    if actor.role not in {
        PlatformRole.SUPER_ADMIN.value,
        PlatformRole.PROJECT_ADMIN.value,
    }:
        reject("access_revoked")
    from app.api.v1.storage import _assert_media_asset_manager
    from app.db.models.dataset import DatasetItem

    item = await db.get(DatasetItem, identifier, populate_existing=True)
    await _assert_media_asset_manager(db, item, actor)
    stmt = (
        select(Task.id, Task.project_id, Project.owner_id)
        .join(Project, Project.id == Task.project_id)
        .where(Task.dataset_item_id == identifier)
        .order_by(Task.id)
    )
    if lock:
        stmt = stmt.with_for_update(read=True, nowait=True)
    rows = (await db.execute(stmt)).all()
    return digest([list(row) for row in rows]), None


async def check(db, authority, *, lock=False):
    if (
        not isinstance(authority, Authority)
        or not isinstance(authority.mode, str)
        or authority.mode not in {"http", "system"}
    ):
        reject()
    async with original_sources._reads(db, True):
        if authority.mode == "http":
            if authority.actor_id is None:
                reject()
            actor, key = await _actor_and_key(
                db, authority.actor_id, authority.key_id, authority.proof
            )
            if key != authority.key:
                raise HTTPException(403, {"code": "api_key_authority_revoked"})
            scope, lifecycle = await _policy(
                db,
                authority.policy,
                authority.owner_kind,
                authority.owner_id,
                actor,
                lock=lock,
            )
            if (
                scope != authority.scope_binding
                or lifecycle != authority.lifecycle_revision
            ):
                reject("video_derivative_authority_changed", 409)
        elif authority.policy != "system":
            reject()
        source = await pin(db, authority.owner_kind, authority.owner_id, lock=lock)
        if (
            receipt(source) != validate_receipt(authority.original)
            or _task_binding(source) != authority.task_binding
        ):
            reject("content_binding_changed", 409)
        clock(authority)
        return source


async def authorize(db, task_id, actor, request):
    return await authorize_policy(db, "task", task_id, actor, request)


async def authorize_policy(db, policy, identifier, actor, request):
    if (
        not isinstance(request, Request)
        or not isinstance(identifier, uuid.UUID)
        or policy not in {"task", "dataset", "storage"}
    ):
        reject()
    kind = "task" if policy == "task" else "dataset_item"
    async with original_sources._reads(db, True):
        proof = await access.capture_credentials(db, actor, request)
        if proof is None or proof.actor_id != actor.id:
            reject()
        fresh_actor, key = await _actor_and_key(db, actor.id, proof.key_id, proof)
        scope, lifecycle = await _policy(
            db, policy, kind, identifier, fresh_actor, lock=True
        )
        source = await pin(db, kind, identifier, lock=True)
    authority = Authority(
        "http",
        policy,
        kind,
        identifier,
        encoded(receipt(source)),
        _task_binding(source),
        scope,
        lifecycle,
        actor.id,
        proof.key_id,
        key,
        proof,
    )
    clock(authority)
    return authority


def authorization(authority):
    if not isinstance(authority, Authority):
        reject()
    value = {
        "schema": 1,
        "mode": authority.mode,
        "policy": authority.policy,
        "owner_kind": authority.owner_kind,
        "owner_id": str(authority.owner_id),
        "original": validate_receipt(authority.original),
        "task_binding": authority.task_binding,
        "scope_binding": authority.scope_binding,
        "lifecycle_revision": authority.lifecycle_revision,
    }
    if authority.mode == "http":
        value.update(
            actor_id=str(authority.actor_id),
            api_key_id=str(authority.key_id) if authority.key_id else None,
            api_key_fingerprint=key_fingerprint(authority.key),
        )
    elif authority.mode != "system" or authority.policy != "system":
        reject()
    return value


def system_authorization(kind, identifier, source):
    """Explicit internal delegation, never inferred from missing HTTP input."""
    if (
        not isinstance(kind, str)
        or kind not in {"dataset_item", "task"}
        or not isinstance(identifier, uuid.UUID)
        or not isinstance(source, original_sources.OriginalSource)
        or (
            kind == "dataset_item"
            and (source.item_id != identifier or source.relation is not None)
        )
        or (
            kind == "task"
            and (source.relation is None or source.relation.task_id != identifier)
        )
    ):
        reject()
    return authorization(
        Authority(
            "system",
            "system",
            kind,
            identifier,
            encoded(receipt(source)),
            _task_binding(source),
        )
    )


async def delegated(db, value):
    if not isinstance(value, dict):
        reject()
    mode, kind, policy = value.get("mode"), value.get("owner_kind"), value.get("policy")
    if not (
        isinstance(mode, str)
        and isinstance(kind, str)
        and isinstance(policy, str)
        and mode in {"http", "system"}
        and kind in {"task", "dataset_item"}
        and set(value)
        == (_BASE_FIELDS | _HTTP_FIELDS if mode == "http" else _BASE_FIELDS)
        and type(value["schema"]) is int
        and value["schema"] == 1
        and _uuid(value["owner_id"]) is not None
        and (
            (
                kind == "task"
                and isinstance(value["task_binding"], str)
                and _SHA.fullmatch(value["task_binding"])
            )
            or (kind == "dataset_item" and value["task_binding"] is None)
        )
        and (
            (
                mode == "system"
                and policy == "system"
                and value["scope_binding"] is None
                and value["lifecycle_revision"] is None
            )
            or (
                mode == "http"
                and policy == "task"
                and kind == "task"
                and value["scope_binding"] is None
                and value["lifecycle_revision"] is None
            )
            or (
                mode == "http"
                and policy in {"dataset", "storage"}
                and kind == "dataset_item"
                and isinstance(value["scope_binding"], str)
                and _SHA.fullmatch(value["scope_binding"])
                and (
                    (policy == "storage" and value["lifecycle_revision"] is None)
                    or (
                        policy == "dataset"
                        and type(value["lifecycle_revision"]) is int
                        and value["lifecycle_revision"] >= 1
                    )
                )
            )
        )
    ):
        reject()
    actor_id, key_id, key = None, None, None
    if mode == "http":
        if (
            not _uuid(value["actor_id"])
            or (value["api_key_id"] is not None and not _uuid(value["api_key_id"]))
            or not isinstance(value["api_key_fingerprint"], str)
            or not _SHA.fullmatch(value["api_key_fingerprint"])
        ):
            reject()
        actor_id, key_id = _uuid(value["actor_id"]), _uuid(value["api_key_id"])
        async with original_sources._reads(db, True):
            _, key = await _actor_and_key(db, actor_id, key_id)
        if key_fingerprint(key) != value["api_key_fingerprint"]:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
    authority = Authority(
        mode,
        policy,
        kind,
        _uuid(value["owner_id"]),
        encoded(validate_receipt(value["original"])),
        value["task_binding"],
        value["scope_binding"],
        value["lifecycle_revision"],
        actor_id,
        key_id,
        key,
    )
    await check(db, authority)
    return authority


async def drain(pending):
    """Repeated soft limits must not abandon an executor or owned cleanup."""
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except BaseException:
            # A second interruption can arrive while the first is draining.
            # Reading result() before done would erase physical uncertainty.
            continue
    return pending.result()


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
            await drain(pending)
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
            reject("content_binding_changed", 409)
        if guard is not None:
            await guard()
        clock(authority)
    # The owned read-only transaction closes before any physical operation.


async def _capture(source, admission):
    timeout = COPY_SECONDS
    pending = asyncio.create_task(
        reader.capture(
            reader.ObjectSpec(
                source.endpoint_url,
                source.bucket,
                source.object_key,
                source.sha256,
                source.size_bytes,
            ),
            max_bytes=MAX_SOURCE_BYTES,
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
            late_stage = await drain(pending)
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
            "video_derivative_original",
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
