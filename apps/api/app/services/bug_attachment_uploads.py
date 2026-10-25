"""Durable bug attachment receipts, one-time report binding and bounded cleanup."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.core.security import verify_password
from app.db.enums import UserRole
from app.db.models.api_key import ApiKey
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.bug_report import BugReport
from app.db.models.storage_maintenance import StorageOperation
from app.db.models.user import User
from app.schemas.bug_report import BugAttachment
from app.services import bug_attachment_accounting as accounting
from app.services import bug_attachment_storage as storage, storage_access
from app.services.content_snapshot import ContentError

log = logging.getLogger(__name__)
_KEY = re.compile(
    r"bug-report-attachments/([0-9a-f-]{36})/([0-9a-f-]{36})\.(png|jpg|webp)"
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


def same(row, receipt):
    current = snapshot(row)
    return all(getattr(current, name) == getattr(receipt, name) for name in _FACTS)


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


async def _locked(db, identifier, *, skip_locked=False):
    return await db.scalar(
        select(BugAttachmentUpload)
        .where(BugAttachmentUpload.id == identifier)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )


async def _actor(db, identifier):
    actor = await db.scalar(
        select(User)
        .where(User.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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
    key = await db.scalar(
        select(ApiKey)
        .where(ApiKey.id == key_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        key is None
        or key.user_id != actor_id
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now())
    ):
        reject("api_key_authority_revoked", 403)
    return key.key_hash, tuple(key.scopes or ()), key.expires_at


@dataclass(frozen=True)
class Authority:
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key_proof: tuple | None


async def lock_authority(db, actor, request):
    """Validate the presented original key without introducing new scopes."""
    actor_id = actor.id
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    key_id = _key_id(request)
    raw_scopes = getattr(request.state, "api_key_scopes", None)
    if key_id is None and raw_scopes is not None:
        reject("api_key_authority_revoked", 403)
    authenticated_scopes = tuple(raw_scopes or ())
    actor = await _actor(db, actor_id)
    proof = await _key_proof(db, actor_id, key_id)
    if proof is not None:
        try:
            valid = scheme.lower() == "bearer" and verify_password(presented, proof[0])
        except (ValueError, TypeError):
            valid = False
        if not valid or authenticated_scopes != proof[1]:
            reject("api_key_authority_revoked", 403)
    return actor, Authority(actor_id, key_id, proof)


async def capture_authority(db, actor, request):
    _, authority = await lock_authority(db, actor, request)
    # Request streaming and image validation must not hold business row locks.
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
        # Repeated cancellation cannot abandon image parsing in its thread.
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
    row = BugAttachmentUpload(
        id=uuid.uuid4(),
        owner_id=authority.actor_id,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mime_type=mime_type,
        file_name=storage.safe_file_name(file_name),
        created_at=started,
        expires_at=started + timedelta(seconds=storage.RETENTION_SECONDS),
        state="pending",
        bound_report_id=None,
    )
    try:
        row.storage_ref = await storage.allocate(db, row)
        db.add(row)
        await db.flush()
        receipt = snapshot(row)
        target = await storage.io_target(db, receipt)
    except (storage.BugAttachmentStorageError, ContentError) as exc:
        raise http_error(exc) from None
    await reauthorize(db, authority)
    await db.commit()
    revision = None
    owned_permit = None

    async def acquired(permit):
        nonlocal revision, owned_permit
        owned_permit = permit
        revision = permit.revision
        current = await _locked(db, receipt.id)
        if current is None or current.state != "pending" or not same(current, receipt):
            reject()
        if current.expires_at <= now():
            reject("bug_attachment_expired", 410)
        await db.commit()

    recorder = accounting.recorder(db, receipt)
    failure = None
    try:
        version = await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            "bug_attachment_write_" + receipt.id.hex,
            storage.write,
            receipt,
            data,
            accounting=recorder,
            on_acquired=acquired,
            **({"target": target} if target is not None else {}),
        )
        if version is not None:
            current = await _locked(db, receipt.id)
            if (
                current is None
                or current.state != "pending"
                or not same(current, receipt)
            ):
                reject()
            current.object_version_id = version
            await db.flush()
            receipt = snapshot(current)
            await db.commit()
        await reauthorize(db, authority)
        current = await _locked(db, receipt.id)
        if current is None or current.state != "pending" or not same(current, receipt):
            reject()
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        # Key and attachment deadlines may elapse during any preceding lock wait.
        await reauthorize(db, authority)
        if current.expires_at <= now():
            reject("bug_attachment_expired", 410)
        current.state = "ready"
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


async def _for_key(db, key):
    matched = _KEY.fullmatch(key)
    if matched is None:
        reject("bug_attachment_unknown_owner")
    try:
        owner_id, identifier = uuid.UUID(matched[1]), uuid.UUID(matched[2])
    except ValueError:
        reject("bug_attachment_unknown_owner")
    row = await _locked(db, identifier)
    if row is None or row.owner_id != owner_id or object_key(row) != key:
        reject("bug_attachment_unknown_owner")
    return row


def _ready(row):
    if row.expires_at <= now():
        reject("bug_attachment_expired", 410)
    if row.state != "ready":
        reject("bug_attachment_not_ready")


async def normalize_for_report(db, actor_id, fields):
    """Lock receipts in stable order and replace claims with actual server facts."""
    supplied = fields.get("attachments") or []
    alias = fields.get("screenshot_url") or None
    keys = [item.get("storage_key", item.get("storageKey")) for item in supplied]
    if len(keys) > 5 or len(set(keys)) != len(keys):
        reject("bug_attachment_duplicate_or_limit")
    if alias and keys and alias != keys[0]:
        reject("bug_attachment_screenshot_mismatch")
    if alias and not keys:
        keys = [alias]
    rows = {}
    for key in sorted(keys):
        row = await _for_key(db, key)
        if row.owner_id != actor_id:
            reject("bug_attachment_foreign_owner", 403)
        _ready(row)
        if row.bound_report_id is not None:
            reject("bug_attachment_already_bound")
        rows[key] = row
    actual = []
    for index, key in enumerate(keys):
        value = attachment(rows[key])
        if supplied and BugAttachment.model_validate(supplied[index]) != value:
            reject("bug_attachment_receipt_mismatch")
        actual.append(value.model_dump(by_alias=True))
    fields["attachments"] = actual
    fields["screenshot_url"] = keys[0] if keys else None
    return [rows[key] for key in keys]


def bind(rows, report_id):
    for row in rows:
        _ready(row)
        if row.bound_report_id is not None:
            reject("bug_attachment_already_bound")
        row.bound_report_id = report_id


def visible(report, actor):
    return bool(
        actor.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
        or report.reporter_id == actor.id
    )


async def locked_report(db, identifier, *, read=False):
    return await db.scalar(
        select(BugReport)
        .where(BugReport.id == identifier)
        .with_for_update(read=read)
        .execution_options(populate_existing=True)
    )


def registered_keys(report):
    keys = {
        str(item.get("storageKey") or item.get("storage_key"))
        for item in (report.attachments or [])
        if item.get("storageKey") or item.get("storage_key")
    }
    if report.screenshot_url:
        keys.add(report.screenshot_url)
    return keys


def _registered_receipt(report, row, key):
    if key not in registered_keys(report):
        reject("bug_attachment_unregistered_key", 400)
    if row.bound_report_id != report.id or row.owner_id != report.reporter_id:
        reject("bug_attachment_binding_mismatch")
    expected = attachment(row)
    for item in report.attachments or []:
        if (item.get("storageKey") or item.get("storage_key")) == key:
            try:
                matches = BugAttachment.model_validate(item) == expected
            except ValueError:
                matches = False
            if not matches:
                reject("bug_attachment_receipt_mismatch")


async def _readable_report(db, report_id, actor, key):
    report = await locked_report(db, report_id, read=True)
    if report is None:
        reject("bug_report_missing", 404)
    if not visible(report, actor):
        reject("bug_attachment_access_denied", 403)
    if key not in registered_keys(report):
        reject("bug_attachment_unregistered_key", 400)
    return report


async def read(db, actor, request, report_id, key):
    """Verify bytes at the saved location, then recheck original authority."""
    authority = await capture_authority(db, actor, request)
    actor = await reauthorize(db, authority)
    report = await _readable_report(db, report_id, actor, key)
    row = await _for_key(db, key)
    _ready(row)
    _registered_receipt(report, row, key)
    try:
        receipt = snapshot(row)
        target = await storage.io_target(db, receipt)
        await db.commit()
        revision = None

        async def acquired(permit):
            nonlocal revision
            revision = permit.revision
            actor = await reauthorize(db, authority)
            report = await _readable_report(db, report_id, actor, key)
            current = await _locked(db, receipt.id)
            if current is None or not same(current, receipt):
                reject()
            _ready(current)
            _registered_receipt(report, current, key)
            await reauthorize(db, authority)
            _ready(current)
            await db.commit()

        data = await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            read_purpose(receipt.id),
            storage.read,
            receipt,
            on_acquired=acquired,
            **({"target": target} if target is not None else {}),
        )
        actor = await reauthorize(db, authority)
        report = await _readable_report(db, report_id, actor, key)
        current = await _locked(db, receipt.id)
        if current is None or not same(current, receipt):
            reject()
        _ready(current)
        _registered_receipt(report, current, key)
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, authority)
        _ready(current)
        await db.commit()
        return receipt, data
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def retire_report(db, report):
    """Retire only this report's known owners inside the deletion transaction."""
    rows = list(
        await db.scalars(
            select(BugAttachmentUpload)
            .where(BugAttachmentUpload.bound_report_id == report.id)
            .order_by(BugAttachmentUpload.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    identifiers = []
    for row in rows:
        if row.owner_id != report.reporter_id or row.state == "pending":
            continue
        if row.state == "ready":
            row.state, row.retired_at = "retired", now()
        identifiers.append(row.id)
    return identifiers


async def _has_read(db, receipt):
    return (
        await db.scalar(
            select(StorageOperation.id)
            .where(
                StorageOperation.location_id
                == uuid.UUID(receipt.storage_ref["location_id"]),
                StorageOperation.purpose == read_purpose(receipt.id),
            )
            .limit(1)
        )
        is not None
    )


async def _protected_binding(db, row):
    if row.bound_report_id is None or row.expires_at <= now():
        return False
    return (
        await db.scalar(select(BugReport.id).where(BugReport.id == row.bound_report_id))
        is not None
    )


async def _cleanup_one(db, identifier):
    row = await _locked(db, identifier, skip_locked=True)
    if row is None or row.state == "pending":
        await db.commit()
        return False
    if row.state == "ready" and row.expires_at > now():
        await db.commit()
        return False
    receipt = snapshot(row)
    row.last_attempted_at = now()
    if await _has_read(db, receipt) or await _protected_binding(db, row):
        await db.commit()
        return False
    if row.state == "ready":
        row.state, row.retired_at = "retired", now()
    elif row.state != "retired":
        await db.commit()
        return False
    await db.commit()
    target = await storage.io_target(db, receipt)

    async def acquired(permit):
        current = await _locked(db, identifier)
        if (
            current is None
            or current.state != "retired"
            or not same(current, receipt)
            or await _has_read(db, receipt)
            or await _protected_binding(db, current)
        ):
            reject()
        await db.commit()

    await storage_access.run(
        db,
        uuid.UUID(receipt.storage_ref["location_id"]),
        "bug_attachment_delete_" + receipt.id.hex,
        storage.delete,
        receipt,
        on_acquired=acquired,
        **({"target": target} if target is not None else {}),
    )
    current = await _locked(db, identifier)
    if (
        current is not None
        and current.state == "retired"
        and same(current, receipt)
        and not await _has_read(db, receipt)
        and not await _protected_binding(db, current)
    ):
        await db.delete(current)
        await db.commit()
        return True
    await db.commit()
    return False


async def cleanup(db, *, identifiers=None, limit=100):
    """Retry bounded expired/retired owners; pending is never age-reaped."""
    limit = max(1, min(limit, 100))
    if identifiers is None:
        identifiers = list(
            await db.scalars(
                select(BugAttachmentUpload.id)
                .where(
                    or_(
                        (BugAttachmentUpload.state == "ready")
                        & (BugAttachmentUpload.expires_at <= now()),
                        BugAttachmentUpload.state == "retired",
                    )
                )
                .order_by(
                    BugAttachmentUpload.last_attempted_at.asc().nulls_first(),
                    BugAttachmentUpload.created_at,
                    BugAttachmentUpload.id,
                )
                .limit(limit)
            )
        )
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
