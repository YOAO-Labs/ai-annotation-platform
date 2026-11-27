"""Durable bug attachment receipts, one-time report binding and bounded cleanup."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import text

from app.schemas.bug_report import BugAttachment
from app.services import bug_attachment_accounting as accounting
from app.services import bug_attachment_storage as storage, storage_access
from app.services import object_removal_accounting as removal
from app.services import saved_output_io
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services.bug_report import attachment_error
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import (
    IdentityCredentialRejected,
    IdentityGatewayUnavailable,
)
from app.services.identity_request_authority import capture_request_key_proof

log = logging.getLogger(__name__)
_RESERVE = text(
    "SELECT yoao_diagnostics_v1.reserve_bug_attachment("
    "CAST(:owner AS uuid), CAST(:id AS uuid), CAST(:sha256 AS text), CAST(:size AS integer), "
    "CAST(:mime AS text), CAST(:file_name AS text), CAST(:created_at AS timestamptz), "
    "CAST(:expires_at AS timestamptz), CAST(:ref AS jsonb))"
)
_LOCK_PENDING = text(
    "SELECT yoao_diagnostics_v1.lock_pending_bug_attachment(CAST(:owner AS uuid), CAST(:id AS uuid))"
)
_RECORD_VERSION = text(
    "SELECT yoao_diagnostics_v1.record_bug_attachment_version("
    "CAST(:owner AS uuid), CAST(:id AS uuid), CAST(:ref AS jsonb), CAST(:version AS text))"
)
_PUBLISH = text(
    "SELECT yoao_diagnostics_v1.publish_bug_attachment("
    "CAST(:owner AS uuid), CAST(:id AS uuid), CAST(:ref AS jsonb), CAST(:version AS text))"
)
_LOCK_REGISTERED = text(
    "SELECT yoao_diagnostics_v1.lock_report_attachment("
    "CAST(:actor AS uuid), CAST(:report AS uuid), CAST(:key AS text))"
)
_CLEANUP_CANDIDATES = text(
    "SELECT yoao_diagnostics_v1.bug_attachment_cleanup_candidates(CAST(:limit AS integer))"
)
_PREPARE_CLEANUP = text(
    "SELECT yoao_diagnostics_v1.prepare_bug_attachment_cleanup(CAST(:id AS uuid))"
)
_LOCK_RETIREMENT = text(
    "SELECT yoao_diagnostics_v1.lock_retired_bug_attachment(CAST(:id AS uuid))"
)
_SETTLE_CLEANUP = text(
    "SELECT yoao_diagnostics_v1.complete_bug_attachment_cleanup("
    "CAST(:id AS uuid), CAST(:ref AS jsonb), CAST(:version AS text))"
)
_FACTS = (
    "id",
    "owner_id",
    "storage_ref",
    "object_version_id",
    "sha256",
    "size_bytes",
    "mime_type",
    "file_name",
    "created_at",
    "expires_at",
    "bound_report_id",
)


def now():
    return datetime.now(timezone.utc)


def reject(code="bug_attachment_changed", status=409):
    raise HTTPException(status, {"code": code})


def http_error(exc):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if isinstance(sub, (storage.BugAttachmentStorageError, ContentError)):
                return http_error(sub)
    if isinstance(exc, storage.BugAttachmentStorageError):
        status = (
            404 if exc.code == "bug_attachment_storage_missing" else exc.status_code
        )
        return HTTPException(status, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "bug_attachment_storage_unavailable"})


@dataclass(frozen=True)
class AttachmentSnapshot:
    id: uuid.UUID
    owner_id: uuid.UUID
    sha256: str
    size_bytes: int
    mime_type: str
    file_name: str
    created_at: datetime
    expires_at: datetime
    bound_report_id: uuid.UUID | None
    object_version_id: str | None
    _storage_ref: dict = field(repr=False)

    @property
    def storage_ref(self):
        return deepcopy(self._storage_ref)


def snapshot(row):
    storage.resolve(row)
    facts = {name: deepcopy(getattr(row, name, None)) for name in _FACTS}
    facts["_storage_ref"] = facts.pop("storage_ref")
    return AttachmentSnapshot(**facts)


def _decode_receipt(raw: dict | None) -> AttachmentSnapshot | None:
    if raw is None:
        return None
    receipt = AttachmentSnapshot(
        id=uuid.UUID(raw["id"]),
        owner_id=uuid.UUID(raw["owner_id"]),
        sha256=raw["sha256"],
        size_bytes=raw["size_bytes"],
        mime_type=raw["mime_type"],
        file_name=raw["file_name"],
        created_at=datetime.fromisoformat(raw["created_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
        bound_report_id=uuid.UUID(raw["bound_report_id"])
        if raw["bound_report_id"]
        else None,
        object_version_id=raw["object_version_id"],
        _storage_ref=raw["storage_ref"],
    )
    return receipt


def object_key(row):
    return storage.key_for(row.owner_id, row.id, row.mime_type)


def attachment(row):
    return BugAttachment(
        storage_key=object_key(row),
        file_name=row.file_name,
        mime_type=row.mime_type,
        size=row.size_bytes,
    )


def read_purpose(identifier):
    return "bug_attachment_read_" + uuid.UUID(str(identifier)).hex


async def _native(db, statement, params):
    if db.autoflush:
        await db.flush()
    return await db.scalar(statement, params)


async def _locked_pending(db, receipt):
    current = _decode_receipt(
        await _native(
            db,
            _LOCK_PENDING,
            {"owner": receipt.owner_id, "id": receipt.id},
        )
    )
    if current != receipt:
        reject()
    return current


async def _actor(db, identifier):
    accounts = await IdentityAccountLockData(db).lock_accounts(
        [identifier], mode="share", nowait=False
    )
    actor = accounts.get(identifier)
    if actor is None or not actor.is_active:
        reject("bug_attachment_authority_revoked", 403)
    return actor


def _key_id(request):
    value = getattr(request.state, "api_key_id", None)
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        reject("api_key_authority_revoked", 403)


async def _key_proof(db, actor_id, key_id):
    if key_id is None:
        return None
    facts = await IdentityAccountLockData(db).read_accepted_api_key_facts(
        actor_id, key_id
    )
    if facts is None or (facts.expires_at is not None and facts.expires_at <= now()):
        reject("api_key_authority_revoked", 403)
    return facts.revision, facts.scopes, facts.expires_at


@dataclass(frozen=True)
class Authority:
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key_proof: tuple | None


async def lock_authority(db, actor, request):
    """Validate the presented original key without introducing new scopes."""
    actor_id = actor.id
    key_id = _key_id(request)
    principal = getattr(request.state, "identity_principal", None)
    if key_id is not None and (
        principal is None
        or principal.credential_type != "api_key"
        or principal.actor_id != actor_id
        or principal.credential_id != key_id
    ):
        reject("api_key_authority_revoked", 403)
    if principal is not None and (principal.credential_type == "api_key") != (
        key_id is not None
    ):
        reject("api_key_authority_revoked", 403)
    try:
        facts = await capture_request_key_proof(db, actor_id=actor_id, request=request)
    except IdentityCredentialRejected:
        reject("api_key_authority_revoked", 403)
    except IdentityGatewayUnavailable:
        raise HTTPException(503, "身份认证服务暂不可用") from None
    actor = await _actor(db, actor_id)
    proof = (facts.revision, facts.scopes, facts.expires_at) if facts else None
    if proof is not None and proof[2] is not None and proof[2] <= now():
        reject("api_key_authority_revoked", 403)
    return actor, Authority(actor_id, key_id, proof)


async def capture_authority(db, actor, request):
    _, authority = await lock_authority(db, actor, request)
    await db.commit()
    return authority


async def reauthorize(db, authority):
    actor = await _actor(db, authority.actor_id)
    if (
        await _key_proof(db, authority.actor_id, authority.key_id)
        != authority.key_proof
    ):
        reject("api_key_authority_revoked", 403)
    return actor


async def validated_image(data, mime_type):
    task = asyncio.create_task(
        asyncio.to_thread(storage.validate_image, data, mime_type)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await storage_access.finish(task)
        except BaseException:
            pass
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def upload(db, authority, data, mime_type, file_name):
    """Commit pending responsibility before PUT; never compensate unknown writes."""
    await reauthorize(db, authority)
    started = now()
    identifier = uuid.uuid4()
    try:
        ref = await storage.allocate(
            db,
            SimpleNamespace(
                id=identifier, owner_id=authority.actor_id, mime_type=mime_type
            ),
        )
        receipt = AttachmentSnapshot(
            id=identifier,
            owner_id=authority.actor_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            mime_type=mime_type,
            file_name=storage.safe_file_name(file_name),
            created_at=started,
            expires_at=started + timedelta(seconds=storage.RETENTION_SECONDS),
            bound_report_id=None,
            object_version_id=None,
            _storage_ref=ref,
        )
        storage.resolve(receipt)
        reserved = await _native(
            db,
            _RESERVE,
            {
                "id": receipt.id,
                "owner": receipt.owner_id,
                "sha256": receipt.sha256,
                "size": receipt.size_bytes,
                "mime": receipt.mime_type,
                "file_name": receipt.file_name,
                "created_at": receipt.created_at,
                "expires_at": receipt.expires_at,
                "ref": json.dumps(ref, allow_nan=False),
            },
        )
        if not reserved:
            reject("bug_attachment_authority_revoked", 403)
        target = await storage.io_target(db, receipt)
    except (storage.BugAttachmentStorageError, ContentError) as exc:
        raise http_error(exc) from None
    await reauthorize(db, authority)
    await db.commit()
    source = saved_output_io.capture(
        target, "bug", receipt, actor_id=authority.actor_id
    )
    revision = None
    owned_permit = None

    async def acquired(permit):
        nonlocal revision, owned_permit
        owned_permit = permit
        revision = permit.revision
        if source is not None:
            source.attach(permit)
        current = await _locked_pending(db, receipt)
        _check_expiry(current)
        await db.commit()

    recorder = accounting.recorder(db, receipt)
    failure = None
    try:
        version = await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            "bug_attachment_write_" + receipt.id.hex,
            source.write if source is not None else storage.write,
            *((data,) if source is not None else (receipt, data)),
            accounting=recorder,
            on_acquired=acquired,
            **({"target": target} if target is not None and source is None else {}),
        )
        if version is not None:
            await _locked_pending(db, receipt)
            receipt = _decode_receipt(
                await _native(
                    db,
                    _RECORD_VERSION,
                    {
                        "owner": receipt.owner_id,
                        "id": receipt.id,
                        "ref": json.dumps(receipt.storage_ref, allow_nan=False),
                        "version": version,
                    },
                )
            )
            if receipt is None:
                reject()
            await db.commit()
        await reauthorize(db, authority)
        current = await _locked_pending(db, receipt)
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, authority)
        _check_expiry(current)
        published = await _native(
            db,
            _PUBLISH,
            {
                "owner": receipt.owner_id,
                "id": receipt.id,
                "ref": json.dumps(receipt.storage_ref, allow_nan=False),
                "version": receipt.object_version_id,
            },
        )
        if not published:
            _check_expiry(current)
            reject()
        await db.commit()
    except BaseException as exc:
        failure = exc
    failure = await accounting.finish(
        recorder,
        failure,
        settled=owned_permit is not None
        and getattr(owned_permit, "_settled", False) is True,
    )
    if isinstance(failure, HTTPException):
        raise failure
    if isinstance(failure, Exception):
        raise http_error(failure) from None
    if failure is not None:
        raise failure
    return attachment(receipt)


def _check_expiry(receipt: AttachmentSnapshot) -> None:
    if receipt.expires_at <= now():
        reject("bug_attachment_expired", 410)


async def _registered_receipt(db, actor, report_id, key):
    raw = await _native(
        db, _LOCK_REGISTERED, {"actor": actor.id, "report": report_id, "key": key}
    )
    if error := raw.get("error"):
        raise attachment_error(error)
    receipt = _decode_receipt(raw["receipt"])
    _check_expiry(receipt)
    return receipt


async def read(db, actor, request, report_id, key):
    """Verify bytes at the saved location, then recheck original authority."""
    authority = await capture_authority(db, actor, request)
    actor = await reauthorize(db, authority)
    receipt = await _registered_receipt(db, actor, report_id, key)
    try:
        owner = storage.resolve(receipt)
        route = await migrated_reads.resolve(
            db,
            owner.ref,
            owner.object_key,
            receipt.sha256,
            receipt.size_bytes,
            source_version_id=(
                receipt.object_version_id
                if receipt.object_version_id is not None
                else publications.ANY_VERSION
            ),
        )
        target = await storage.io_target(db, receipt) if route is None else None
        source = saved_output_io.capture(target, "bug", receipt)
        await db.commit()
        revision = None

        async def acquired(permit):
            nonlocal revision
            revision = permit.revision
            actor = await reauthorize(db, authority)
            current = await _registered_receipt(db, actor, report_id, key)
            if current != receipt:
                reject()
            await reauthorize(db, authority)
            _check_expiry(current)
            if source is not None:
                source.attach(permit)
                await source.admit_read(
                    db, authority.actor_id, report_id=report_id, key=key
                )
            await db.commit()

        if route is not None:
            data = await migrated_reads.run(
                db,
                route,
                read_purpose(receipt.id),
                lambda client: storage.read_from_client(
                    client, receipt, response_version_id=route.target_version_id
                ),
                on_acquired=acquired,
            )
        else:
            data = await storage_access.run(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                read_purpose(receipt.id),
                source.read_bytes if source is not None else storage.read,
                *(() if source is not None else (receipt,)),
                on_acquired=acquired,
                **({"target": target} if target is not None and source is None else {}),
            )
        actor = await reauthorize(db, authority)
        current = await _registered_receipt(db, actor, report_id, key)
        if current != receipt:
            reject()
        if route is not None:
            await publications.recheck(db, route, lock=True)
        else:
            await storage_access.check(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                expected_revision=revision,
                lock=True,
            )
        await reauthorize(db, authority)
        _check_expiry(current)
        await db.commit()
        return receipt, data
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def _sync_cached_upload(db, identifier, *, deleted=False):
    sync_session = getattr(db, "sync_session", None)
    if sync_session is None:
        return
    for (model, key, _), instance in list(sync_session.identity_map.items()):
        if getattr(
            model, "__tablename__", None
        ) == "bug_attachment_uploads" and key == (identifier,):
            if deleted:
                db.expunge(instance)
            else:
                await db.refresh(instance)


async def _cleanup_one(db, identifier):
    receipt = _decode_receipt(await _native(db, _PREPARE_CLEANUP, {"id": identifier}))
    if receipt is not None:
        storage.resolve(receipt)
    await db.commit()
    if receipt is None:
        return False
    await _sync_cached_upload(db, identifier, deleted=False)
    target = await storage.io_target(db, receipt)
    source = saved_output_io.capture(target, "bug", receipt)
    recorder = removal.recorder(
        db, receipt, producer="bug_attachment_removals", purpose="other_platform"
    )
    owned_permit, failure = None, None

    async def acquired(permit):
        nonlocal owned_permit
        owned_permit = permit
        if source is not None:
            source.attach(permit)
        current = _decode_receipt(
            await _native(db, _LOCK_RETIREMENT, {"id": identifier})
        )
        if current != receipt:
            reject()
        await db.commit()

    try:
        await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            "bug_attachment_delete_" + receipt.id.hex,
            source.delete if source is not None else storage.delete,
            *(() if source is not None else (receipt,)),
            on_acquired=acquired,
            accounting=recorder,
            **({"version": receipt.object_version_id} if source is not None else {}),
            **({"target": target} if target is not None and source is None else {}),
        )
    except BaseException as exc:
        failure = exc
    failure = await removal.finish(
        recorder,
        failure,
        settled=owned_permit is not None
        and getattr(owned_permit, "_settled", False) is True,
    )
    if failure is not None:
        raise failure
    current = _decode_receipt(await _native(db, _LOCK_RETIREMENT, {"id": identifier}))
    deleted = current == receipt and await _native(
        db,
        _SETTLE_CLEANUP,
        {
            "id": identifier,
            "ref": json.dumps(receipt.storage_ref, allow_nan=False),
            "version": receipt.object_version_id,
        },
    )
    await db.commit()
    await _sync_cached_upload(db, identifier, deleted=bool(deleted))
    return deleted


async def cleanup(db, *, identifiers=None, limit=100):
    """Retry bounded expired/retired owners; pending is never age-reaped."""
    limit = max(1, min(limit, 100))
    if identifiers is None:
        if db.autoflush:
            await db.flush()
        identifiers = list(await db.scalars(_CLEANUP_CANDIDATES, {"limit": limit}))
        await db.commit()
    else:
        identifiers = list(dict.fromkeys(identifiers))[:limit]
    result = {"deleted": 0, "retained": 0, "failed": 0}
    for identifier in identifiers:
        try:
            deleted = await _cleanup_one(db, identifier)
            result["deleted" if deleted else "retained"] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            log.warning("Bug attachment cleanup retained: %s", identifier)
    return result
