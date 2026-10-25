"""Canonical image facts, original credentials and physically owned snapshots.

Business transactions are never committed here. Independent read sessions end
before native I/O; source admission survives until the transferred image closes.
"""

import asyncio
from contextvars import copy_context
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import re
import threading
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PlatformRole
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services import comment_attachment_authority as credentials
from app.services import content_snapshot as images
from app.services import managed_original_reader as reader
from app.services import original_sources, storage_access
from app.services.content_snapshot import ContentError
from app.services.reference_bindings import task_binding
from app.services.reference_keys import is_reference

MAX_TASKS = 1000
MAX_PROOF_BYTES = 1024 * 1024
MAX_LINKS = 1000
COPY_SECONDS = 60
_SHA = re.compile(r"[0-9a-f]{64}")
_slots = threading.BoundedSemaphore(4)
_ISSUER = object()


def _json_default(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported original input fact")


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


def bounded_proof(value):
    try:
        data = encoded(value).encode()
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ContentError("prediction_scope_changed") from None
    if len(data) > MAX_PROOF_BYTES:
        raise ContentError("prediction_proof_budget_exceeded", 413)
    return json.loads(data)


def source_receipt(source):
    if (
        not isinstance(source, original_sources.OriginalSource)
        or source.owner_kind not in {"dataset_asset", "task_original"}
        or source.media_type != "image"
        or source.relation is None
        or source.location_id is None
        or type(source.size_bytes) is not int
        or not 0 < source.size_bytes <= images.MAX_BYTES
        or is_reference(source.object_key)
    ):
        raise ContentError("prediction_original_unsupported")
    reader.ObjectSpec(
        source.endpoint_url,
        source.bucket,
        source.object_key,
        source.sha256,
        source.size_bytes,
    )
    facts = json.loads(encoded(asdict(source)))
    return bounded_proof(
        {
            "schema": 1,
            "owner_kind": source.owner_kind,
            "content_id": str(source.content_id),
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "location_id": str(source.location_id),
            "location_revision": source.location_revision,
            "maintenance_revision": source.maintenance_revision,
            "endpoint_url": source.endpoint_url,
            "bucket": source.bucket,
            "object_key": source.object_key,
            "facts_digest": digest(facts),
            "facts": facts,
        }
    )


async def resolve(db, task_id, *, lock=False):
    source = await original_sources.CachedResolver(
        db, lock=lock, max_links=MAX_LINKS
    ).task(task_id)
    source_receipt(source)
    return source


async def managed_binding(db, task, *, lock=False):
    """Version-free original facts; the outer task binding retains its version."""
    return source_receipt(await resolve(db, task.id, lock=lock))


async def locked_task(db, task_id):
    """Refresh the version only after its Batch and Task parent fences hold."""
    async with original_sources._reads(db, True):
        located = (
            await db.execute(select(Task.batch_id).where(Task.id == task_id))
        ).one_or_none()
        if located is None:
            raise ContentError("prediction_scope_changed")
        batch_id = located[0]
        if batch_id is not None:
            await db.execute(
                select(TaskBatch.id)
                .where(TaskBatch.id == batch_id)
                .with_for_update(read=True, nowait=True)
            )
        task = await db.scalar(
            select(Task)
            .where(Task.id == task_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True)
        )
        if task is None or task.batch_id != batch_id:
            raise ContentError("prediction_scope_changed")
        return task


def credentials_value(proof):
    # Store a fingerprint of the verifier, never the request token or key hash.
    key = proof.key_proof
    return {
        "schema": 1,
        "actor_id": str(proof.actor_id),
        "api_key_id": str(proof.key_id) if proof.key_id else None,
        "key_fingerprint": digest(key) if key is not None else None,
        "key_expires_at": key[2].isoformat() if key is not None and key[2] else None,
        "jwt": None
        if proof.jwt is None
        else {
            "expires_at": proof.jwt.expires_at.isoformat(),
            "generation": proof.jwt.generation,
            "jti": proof.jwt.jti,
        },
    }


async def capture_credentials(db, actor, request):
    try:
        _, proof = await credentials.lock_authority(
            db, actor, request, allowed_roles=frozenset(PlatformRole)
        )
    except HTTPException as exc:
        raise credential_error(exc) from None
    return credentials_value(proof)


def credential_error(exc):
    code = exc.detail.get("code") if isinstance(exc.detail, dict) else None
    if not isinstance(code, str) or code.startswith("comment_attachment_"):
        code = "prediction_authority_revoked"
    return ContentError(code, exc.status_code)


def _jwt(value):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"expires_at", "generation", "jti"}
        or type(value["generation"]) is not int
        or value["generation"] < 0
        or (
            value["jti"] is not None
            and (not isinstance(value["jti"], str) or not value["jti"])
        )
    ):
        raise ValueError
    expires = datetime.fromisoformat(value["expires_at"])
    if expires.utcoffset() is None:
        raise ValueError
    return credentials.JwtProof(expires, value["generation"], value["jti"])


def validate_credentials(value, actor_id, key_id):
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "actor_id",
                "api_key_id",
                "key_fingerprint",
                "key_expires_at",
                "jwt",
            }
            or type(value["schema"]) is not int
            or value["schema"] != 1
            or value["actor_id"] != str(actor_id)
            or value["api_key_id"] != (str(key_id) if key_id else None)
            or (key_id is None) != (value["key_fingerprint"] is None)
            or (key_id is None) != (value["jwt"] is not None)
            or (key_id is None and value["key_expires_at"] is not None)
            or (
                key_id is not None
                and (
                    not isinstance(value["key_fingerprint"], str)
                    or _SHA.fullmatch(value["key_fingerprint"]) is None
                )
            )
        ):
            raise ValueError
        _jwt(value["jwt"])
        if value["key_expires_at"] is not None:
            if datetime.fromisoformat(value["key_expires_at"]).utcoffset() is None:
                raise ValueError
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ContentError("prediction_credentials_invalid", 403) from None


def check_clock(value):
    try:
        jwt = _jwt(value["jwt"])
        if jwt is not None and jwt.expires_at <= datetime.now(timezone.utc):
            raise ContentError("prediction_authority_revoked", 403)
        if value["key_expires_at"] is not None and datetime.fromisoformat(
            value["key_expires_at"]
        ) <= datetime.now(timezone.utc):
            raise ContentError("api_key_authority_revoked", 403)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ContentError("prediction_credentials_invalid", 403) from None


async def reauthorize(db, value, actor_id, key_id):
    validate_credentials(value, actor_id, key_id)
    try:
        actor = await credentials._actor(db, actor_id, frozenset(PlatformRole))
        current_key = await credentials._key(db, actor_id, key_id)
        if (digest(current_key) if current_key is not None else None) != value[
            "key_fingerprint"
        ]:
            raise ContentError("api_key_authority_revoked", 403)
        if (
            current_key[2].isoformat() if current_key and current_key[2] else None
        ) != value["key_expires_at"]:
            raise ContentError("api_key_authority_revoked", 403)
        proof = credentials.Authority(
            actor_id,
            key_id,
            current_key,
            _jwt(value["jwt"]),
            frozenset(PlatformRole),
        )
        await credentials._jwt_current(proof)
        return actor
    except HTTPException as exc:
        raise credential_error(exc) from None


async def drain(pending):
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except BaseException:
            continue
    return pending.result()


def _uncertain(error, later):
    if getattr(later, "storage_operation_unsettled", False):
        storage_access.mark_unsettled(error)


async def physical(call, *args, **kwargs):
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
            await drain(pending)
        except BaseException as later:
            _uncertain(interrupted, later)
        raise


async def physical_close(call):
    """Only an actually failed close is uncertain; an interrupted wait is not."""
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, call
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await drain(pending)
        except BaseException:
            storage_access.mark_unsettled(interrupted)
        raise


async def settled(call, *, late=None):
    pending = asyncio.create_task(call)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            result = await drain(pending)
        except BaseException as later:
            _uncertain(interrupted, later)
        else:
            if late is not None:
                try:
                    await late(result)
                except BaseException as later:
                    _uncertain(interrupted, later)
        raise


async def close_permit(permit, failure=None):
    if getattr(failure, "storage_operation_unsettled", False):
        return
    await settled(permit.__aexit__(None, None, None))


@dataclass
class SourceInput:
    snapshot: images.Snapshot
    permit: object

    async def close(self, failure=None):
        if getattr(failure, "storage_operation_unsettled", False):
            return
        closing = None
        try:
            await physical_close(self.snapshot.close)
        except BaseException as exc:
            closing = exc
            raise
        finally:
            await close_permit(self.permit, closing)


async def open_snapshot(db, task_id, expected, *, guard, purpose, node_id):
    """Return an image plus a source permit; caller transfers image ownership."""

    async def fresh():
        async with AsyncSession(
            bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
        ) as owned:
            await guard(owned)
            source = await resolve(owned, task_id, lock=True)
            if source_receipt(source) != expected:
                raise ContentError("content_binding_changed")
            return source

    source = await settled(fresh())

    async def acquire():
        async with AsyncSession(
            bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
        ) as owned:
            return await storage_access.acquire(
                owned,
                source.location_id,
                purpose,
                expected_revision=source.maintenance_revision,
                node_id=node_id,
            )

    permit = stage = snapshot = failure = None
    try:
        permit = await settled(acquire(), late=close_permit)
        await settled(fresh())
        read_options = await settled(
            original_sources.read_options(db, source, read_timeout=15)
        )
        # The reader owns/relinquishes the slot only after real file close.
        if not _slots.acquire(blocking=False):
            raise ContentError("snapshot_capacity_exceeded", 429)
        stop = threading.Event()
        try:
            pending = asyncio.get_running_loop().run_in_executor(
                None,
                copy_context().run,
                partial(
                    reader._capture,
                    reader.ObjectSpec(
                        source.endpoint_url,
                        source.bucket,
                        source.object_key,
                        source.sha256,
                        source.size_bytes,
                    ),
                    images.MAX_BYTES,
                    COPY_SECONDS,
                    time.monotonic() + COPY_SECONDS,
                    stop,
                    _slots.release,
                    transport_timeout_seconds=15,
                    retain_on_close_error=True,
                    **read_options,
                ),
            )
        except BaseException:
            _slots.release()
            raise
        try:
            stage = await asyncio.shield(pending)
        except BaseException as interrupted:
            stop.set()
            try:
                late_stage = await drain(pending)
            except BaseException as later:
                _uncertain(interrupted, later)
            else:
                try:
                    await physical_close(late_stage.close)
                except BaseException as later:
                    _uncertain(interrupted, later)
            raise
        if stage.sha256 != source.sha256 or stage.size != source.size_bytes:
            raise ContentError("source_changed")
        # adopt_stage owns the file from invocation, including interrupted waits.
        consumed, stage = stage, None
        snapshot = await settled(
            images.adopt_stage(consumed), late=lambda value: physical_close(value.close)
        )
        await settled(fresh())
        return SourceInput(snapshot, permit)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if failure is not None:
            try:
                if snapshot is not None:
                    await physical_close(snapshot.close)
                elif stage is not None:
                    await physical_close(stage.close)
            except BaseException as closing:
                _uncertain(failure, closing)
                _uncertain(closing, failure)
                raise
            finally:
                if permit is not None:
                    await close_permit(permit, failure)


@dataclass(frozen=True, slots=True)
class _Receipt:
    issuer: object
    session: object
    transaction: object
    task_id: uuid.UUID
    binding: str
    evidence: str


def public_provenance(evidence):
    """Project a private source receipt onto explicitly public identity facts."""
    original = evidence["original"]
    return {
        **{
            key: evidence[key]
            for key in (
                "schema",
                "task_id",
                "batch_id",
                "item_id",
                "version",
                "job_id",
                "actor_id",
                "node_id",
                "execution_snapshot_id",
                "execution_digest",
            )
            if key in evidence
        },
        "original": {
            key: original[key]
            for key in (
                "schema",
                "owner_kind",
                "content_id",
                "sha256",
                "size_bytes",
                "location_id",
                "location_revision",
                "maintenance_revision",
                "facts_digest",
            )
            if key in original
        },
    }


def issue_receipt(db, task_id, binding, evidence):
    if db.sync_session.in_nested_transaction():
        raise ContentError("prediction_original_receipt_invalid")
    return _Receipt(
        _ISSUER,
        db.sync_session,
        db.sync_session.get_transaction(),
        task_id,
        encoded(binding),
        encoded(public_provenance(evidence)),
    )


async def validate_receipt(db, task_id, project_id, receipt):
    if (
        type(receipt) is not _Receipt
        or receipt.issuer is not _ISSUER
        or receipt.session is not db.sync_session
        or receipt.transaction is None
        or receipt.transaction is not db.sync_session.get_transaction()
        or db.sync_session.in_nested_transaction()
        or receipt.task_id != task_id
    ):
        raise ContentError("prediction_original_receipt_invalid")
    task = await db.get(Task, task_id, populate_existing=True)
    if task is None or task.project_id != project_id or is_reference(task.file_path):
        raise ContentError("prediction_original_receipt_invalid")
    binding = {
        **task_binding(task),
        "original": await managed_binding(db, task, lock=True),
    }
    if binding != json.loads(receipt.binding):
        raise ContentError("content_binding_changed")
    return public_provenance(json.loads(receipt.evidence))
