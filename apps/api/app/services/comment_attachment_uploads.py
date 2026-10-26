"""Durable comment-file receipts, original-comment binding and retirement."""

from datetime import timedelta
import hashlib
import logging
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import exists, or_, select

from app.db.models.annotation_comment import AnnotationComment
from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.storage_maintenance import StorageOperation
from app.schemas._jsonb_types import Attachment
from app.services import conditional_write_accounting as accounting
from app.services import comment_attachment_authority as authority
from app.services import comment_attachment_storage as storage, storage_access
from app.services import storage_migration_artifact_reads as migration_reads
from app.services import storage_migration_read_publications as read_publications
from app.services.content_snapshot import ContentError
from app.services.storage import storage_service

log = logging.getLogger(__name__)
_KEY = re.compile(r"comment-attachments/([0-9a-f-]{36})/([0-9a-f-]{36})")
GRACE = timedelta(days=7)
now = authority.now
reject = authority.reject
lock_authority = authority.lock_authority
reauthorize = authority.reauthorize
context = authority.context


def http_error(exc):
    if isinstance(exc, storage.CommentAttachmentStorageError):
        status = (
            404 if exc.code == "comment_attachment_storage_missing" else exc.status_code
        )
        return HTTPException(status, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "comment_attachment_storage_unavailable"})


def attachment(row):
    return Attachment(
        storageKey=object_key(row),
        fileName=row.file_name,
        mimeType=row.mime_type,
        size=row.size_bytes,
    )


def object_key(row):
    return storage.key_for(row.annotation_id, row.id)


def read_purpose(identifier):
    return "comment_attachment_read_" + uuid.UUID(str(identifier)).hex


async def _locked(db, identifier, *, skip_locked=False):
    return await authority.locked(
        db, CommentAttachmentUpload, identifier, read=False, skip_locked=skip_locked
    )


def _check_expiries(expiries):
    current = now()
    if any(expires_at <= current for expires_at in expiries):
        reject("comment_attachment_expired", 410)


def check_deadlines(proof, expiries):
    """Recheck immutable deadlines after waits without reopening business locks."""
    authority.check_clock(proof)
    _check_expiries(expiries)


def _ready(row):
    _check_expiries((row.expires_at,))
    if row.state != "ready":
        reject("comment_attachment_not_ready")


def _matches_context(row, ctx):
    if (row.annotation_id, row.task_id, row.project_id) != (
        ctx.annotation_id,
        ctx.task_id,
        ctx.project_id,
    ):
        reject("comment_attachment_context_changed")


def _identifier(key):
    if not isinstance(key, str):
        reject("comment_attachment_storage_unpinned")
    match = _KEY.fullmatch(key)
    if match is None:
        reject("comment_attachment_storage_unpinned")
    try:
        annotation_id, identifier = uuid.UUID(match[1]), uuid.UUID(match[2])
        if storage.key_for(annotation_id, identifier) != key:
            raise ValueError
        return annotation_id, identifier
    except ValueError:
        reject("comment_attachment_storage_unpinned")


async def _for_key(db, key):
    annotation_id, identifier = _identifier(key)
    row = await _locked(db, identifier)
    if row is None or row.annotation_id != annotation_id or object_key(row) != key:
        reject("comment_attachment_storage_unpinned")
    return row


async def capture_upload(db, actor, request, annotation_id):
    actor, proof = await lock_authority(db, actor, request)
    ctx = await context(db, actor, annotation_id, active=True)
    await reauthorize(db, proof)
    await db.commit()
    authority.check_clock(proof)
    return proof, ctx


async def upload(db, proof, original, data, mime_type, file_name):
    """Persist pending before external writes; never compensate uncertain writes."""
    recorder, owned_permit, failure, result = None, None, None, None
    try:
        if type(data) is not bytes or len(data) > storage.MAX_BYTES:
            reject("comment_attachment_too_large", 413)
        actor = await reauthorize(db, proof)
        ctx = await context(
            db, actor, original.annotation_id, active=True, expected=original
        )
        started = now()
        row = CommentAttachmentUpload(
            id=uuid.uuid4(),
            owner_id=proof.actor_id,
            annotation_id=ctx.annotation_id,
            task_id=ctx.task_id,
            project_id=ctx.project_id,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            mime_type=storage.normalize_mime_type(mime_type),
            file_name=storage.safe_file_name(file_name),
            created_at=started,
            expires_at=started + timedelta(seconds=storage.RETENTION_SECONDS),
            state="pending",
            bound_comment_id=None,
        )
        row.storage_ref = await storage.allocate(db, row)
        db.add(row)
        await db.flush()
        receipt = storage.snapshot(row)
        expiries = (receipt.expires_at,)
        await reauthorize(db, proof)
        await db.commit()
        check_deadlines(proof, expiries)
        recorder = accounting.recorder(
            db, receipt, producer="comment_attachment_uploads", purpose="other_platform"
        )
        revision = None
        target = None

        async def acquired(permit):
            nonlocal revision, target, owned_permit
            owned_permit = permit
            actor = await reauthorize(db, proof)
            await context(
                db, actor, original.annotation_id, active=True, expected=original
            )
            current = await _locked(db, receipt.id)
            if (
                current is None
                or current.state != "pending"
                or not storage.same(current, receipt)
            ):
                reject()
            if current.expires_at <= now():
                reject("comment_attachment_expired", 410)
            await reauthorize(db, proof)
            target = await storage.io_target(db, receipt)
            await db.commit()
            check_deadlines(proof, expiries)
            revision = permit.revision

        def write_saved():
            # Executor queueing can outlive admission; only immutable facts are used.
            check_deadlines(proof, expiries)
            storage.run_io(
                target,
                storage_service.client,
                "write",
                receipt,
                data,
                accounting=recorder,
            )

        await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            "comment_attachment_write_" + receipt.id.hex,
            write_saved,
            on_acquired=acquired,
        )
        actor = await reauthorize(db, proof)
        await context(db, actor, original.annotation_id, active=True, expected=original)
        current = await _locked(db, receipt.id)
        if (
            current is None
            or current.state != "pending"
            or not storage.same(current, receipt)
        ):
            reject()
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, proof)
        if current.expires_at <= now():
            reject("comment_attachment_expired", 410)
        current.state = "ready"
        await db.commit()
        check_deadlines(proof, expiries)
        result = attachment(receipt)
    except BaseException as exc:
        failure = exc
    if recorder is not None:
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
    return result


async def normalize_for_comment(db, actor_id, ctx, supplied):
    """Lock by UUID, accepting only exact server receipts for this author/context."""
    keys = [value.storage_key for value in supplied]
    if len(set(keys)) != len(keys):
        reject("comment_attachment_duplicate_key")
    indexed = [(_identifier(key)[1], key) for key in keys]
    rows = {}
    for identifier, key in sorted(indexed):
        row = await _locked(db, identifier)
        if row is None or object_key(row) != key:
            reject("comment_attachment_storage_unpinned")
        if row.owner_id != actor_id:
            reject("comment_attachment_foreign_owner", 403)
        _matches_context(row, ctx)
        _ready(row)
        if row.bound_comment_id is not None:
            reject("comment_attachment_already_bound")
        try:
            storage.snapshot(row)
        except storage.CommentAttachmentStorageError as exc:
            raise http_error(exc) from None
        rows[key] = row
    actual = []
    for supplied_value in supplied:
        row = rows[supplied_value.storage_key]
        server_value = attachment(row)
        if supplied_value != server_value:
            reject("comment_attachment_receipt_mismatch")
        actual.append(server_value.model_dump(by_alias=True))
    return [rows[key] for key in keys], actual


async def bind(db, rows, comment_id):
    # All owners already follow the context locks. The gate is always last.
    for location_id in sorted(
        {uuid.UUID(row.storage_ref["location_id"]) for row in rows}
    ):
        try:
            await storage_access.check(db, location_id, lock=True)
        except ContentError as exc:
            raise http_error(exc) from None
    for row in rows:
        _ready(row)
        if row.bound_comment_id is not None:
            reject("comment_attachment_already_bound")
        row.bound_comment_id = comment_id


def validate_bound(rows, comment_id):
    for row in rows:
        _ready(row)
        if row.bound_comment_id != comment_id:
            reject("comment_attachment_binding_mismatch")


async def locked_comment(db, identifier):
    return await authority.locked(db, AnnotationComment, identifier, read=False)


def _registered(comment, row, key, ctx):
    if not comment.is_active:
        reject("comment_attachment_comment_unavailable", 404)
    _matches_context(row, ctx)
    if (
        comment.annotation_id != ctx.annotation_id
        or comment.project_id != ctx.project_id
        or row.bound_comment_id != comment.id
        or row.owner_id != comment.author_id
    ):
        reject("comment_attachment_binding_mismatch")
    matches = []
    for value in comment.attachments or []:
        if not isinstance(value, dict):
            continue
        if value.get("storageKey", value.get("storage_key")) == key:
            try:
                matches.append(Attachment.model_validate(value) == attachment(row))
            except ValueError:
                matches.append(False)
    if matches != [True]:
        reject("comment_attachment_receipt_mismatch")


async def _read_binding(db, actor, annotation_id, key, *, expected=None, receipt=None):
    # Pre-read locates the bound original Comment; no Feedback metadata is authority.
    ctx = await context(db, actor, annotation_id, expected=expected)
    key_annotation_id, identifier = _identifier(key)
    if key_annotation_id != annotation_id:
        reject("comment_attachment_unregistered_key", 400)
    with db.no_autoflush:
        comment_id = await db.scalar(
            select(CommentAttachmentUpload.bound_comment_id).where(
                CommentAttachmentUpload.id == identifier
            )
        )
    if comment_id is None:
        reject("comment_attachment_storage_unpinned")
    comment = await locked_comment(db, comment_id)
    if comment is None:
        reject("comment_attachment_comment_unavailable", 404)
    current = await _locked(db, identifier)
    if current is None:
        reject("comment_attachment_storage_unpinned")
    if receipt is not None and not storage.same(current, receipt):
        reject()
    _ready(current)
    _registered(comment, current, key, ctx)
    return ctx, current


async def read(db, actor, request, annotation_id, key):
    try:
        actor, proof = await lock_authority(db, actor, request)
        original, row = await _read_binding(db, actor, annotation_id, key)
        receipt = storage.snapshot(row)
        expiries = (receipt.expires_at,)
        owner = storage.resolve(receipt)
        read_route = await migration_reads.resolve(
            db, owner.ref, owner.object_key, receipt.sha256, receipt.size_bytes
        )
        await reauthorize(db, proof)
        await db.commit()
        check_deadlines(proof, expiries)
        revision = None
        target = None

        async def acquired(permit):
            nonlocal revision, target
            actor = await reauthorize(db, proof)
            await _read_binding(
                db, actor, annotation_id, key, expected=original, receipt=receipt
            )
            await reauthorize(db, proof)
            if read_route is None:
                target = await storage.io_target(db, receipt)
            await db.commit()
            check_deadlines(proof, expiries)
            revision = permit.revision

        def read_saved():
            check_deadlines(proof, expiries)
            return storage.run_io(target, storage_service.client, "read", receipt)

        if read_route is None:
            data = await storage_access.run(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                read_purpose(receipt.id),
                read_saved,
                on_acquired=acquired,
            )
        else:

            def read_mapped(reader):
                check_deadlines(proof, expiries)
                return storage.read_routed(reader, receipt)

            data = await migration_reads.run(
                db,
                read_route,
                read_purpose(receipt.id),
                read_mapped,
                on_acquired=acquired,
            )
        actor = await reauthorize(db, proof)
        _, current = await _read_binding(
            db, actor, annotation_id, key, expected=original, receipt=receipt
        )
        if read_route is None:
            await storage_access.check(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                expected_revision=revision,
                lock=True,
            )
        else:
            await read_publications.recheck(db, read_route, lock=True)
        await reauthorize(db, proof)
        _ready(current)
        await db.commit()
        check_deadlines(proof, expiries)
        return receipt, data
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def retire_for_comments(db, comment_ids):
    """Retire known owners in the caller's deletion transaction, without commit."""
    with db.no_autoflush:
        try:
            rows = list(
                await db.scalars(
                    select(CommentAttachmentUpload)
                    .where(CommentAttachmentUpload.bound_comment_id.in_(comment_ids))
                    .order_by(CommentAttachmentUpload.id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            )
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            if getattr(getattr(exc, "orig", None), "sqlstate", None) in {
                "55P03",
                "40P01",
            }:
                reject("comment_attachment_busy")
            raise http_error(exc) from None
    identifiers = []
    for row in rows:
        if row.state == "ready":
            row.state, row.retired_at = "retired", now()
        if row.state == "retired":
            identifiers.append(row.id)
    return identifiers


async def _has_read(db, receipt):
    return (
        await db.scalar(
            select(StorageOperation.id)
            .where(
                StorageOperation.purpose == read_purpose(receipt.id),
            )
            .limit(1)
        )
        is not None
    )


async def _active_comment(db, row):
    # Plain MVCC reads deliberately take no Comment lock after an owner lock.
    if row.bound_comment_id is None or row.expires_at <= now():
        return False
    return (
        await db.scalar(
            select(AnnotationComment.id).where(
                AnnotationComment.id == row.bound_comment_id,
                AnnotationComment.is_active.is_(True),
                AnnotationComment.author_id == row.owner_id,
                AnnotationComment.annotation_id == row.annotation_id,
                AnnotationComment.project_id == row.project_id,
            )
        )
        is not None
    )


def _due(row):
    return row.expires_at <= now() or (
        row.retired_at is not None and row.retired_at + GRACE <= now()
    )


async def _cleanup_one(db, identifier):
    row = await _locked(db, identifier, skip_locked=True)
    if row is None or row.state == "pending":
        await db.commit()
        return None
    if await _active_comment(db, row):
        await db.commit()
        return None
    if row.state == "ready":
        if row.expires_at > now() and row.bound_comment_id is None:
            await db.commit()
            return None
        row.state, row.retired_at = "retired", now()
    if row.state != "retired":
        await db.commit()
        return None
    row.last_attempted_at = now()
    receipt = storage.snapshot(row)
    if not _due(row) or await _has_read(db, receipt):
        await db.commit()
        return None
    await db.commit()

    target = None

    async def acquired(permit):
        nonlocal target
        current = await _locked(db, identifier)
        if (
            current is None
            or current.state != "retired"
            or not storage.same(current, receipt)
            or not _due(current)
            or await _active_comment(db, current)
            or await _has_read(db, receipt)
        ):
            reject()
        target = await storage.io_target(db, receipt)
        await db.commit()

    await storage_access.run(
        db,
        uuid.UUID(receipt.storage_ref["location_id"]),
        "comment_attachment_delete_" + receipt.id.hex,
        lambda: storage.run_io(target, storage_service.client, "delete", receipt),
        on_acquired=acquired,
    )
    current = await _locked(db, identifier)
    if (
        current is not None
        and current.state == "retired"
        and storage.same(current, receipt)
        and _due(current)
        and not await _active_comment(db, current)
        and not await _has_read(db, receipt)
    ):
        await db.delete(current)
        await db.commit()
        return receipt
    await db.commit()
    return None


async def cleanup(db, *, identifiers=None, limit=500):
    """Retry only known owners; leave all pending and legacy JSON untouched."""
    limit = max(1, min(int(limit), 500))
    if identifiers is None:
        active = exists().where(
            AnnotationComment.id == CommentAttachmentUpload.bound_comment_id,
            AnnotationComment.is_active.is_(True),
            AnnotationComment.author_id == CommentAttachmentUpload.owner_id,
            AnnotationComment.annotation_id == CommentAttachmentUpload.annotation_id,
            AnnotationComment.project_id == CommentAttachmentUpload.project_id,
        )
        identifiers = list(
            await db.scalars(
                select(CommentAttachmentUpload.id)
                .where(
                    or_(
                        CommentAttachmentUpload.state == "retired",
                        (CommentAttachmentUpload.state == "ready")
                        & or_(
                            CommentAttachmentUpload.expires_at <= now(),
                            CommentAttachmentUpload.bound_comment_id.is_not(None)
                            & ~active,
                        ),
                    )
                )
                .order_by(
                    CommentAttachmentUpload.last_attempted_at.asc().nulls_first(),
                    CommentAttachmentUpload.created_at,
                    CommentAttachmentUpload.id,
                )
                .limit(limit)
            )
        )
        await db.commit()
    else:
        identifiers = list(dict.fromkeys(identifiers))[:limit]
    result, comments = {"comments": 0, "objects": 0, "retained": 0, "failed": 0}, set()
    for identifier in identifiers:
        try:
            receipt = await _cleanup_one(db, identifier)
            if receipt is None:
                result["retained"] += 1
            else:
                result["objects"] += 1
                if receipt.bound_comment_id is not None:
                    comments.add(receipt.bound_comment_id)
        except Exception:
            await db.rollback()
            result["failed"] += 1
            log.warning("Comment attachment cleanup retained: %s", identifier)
    result["comments"] = len(comments)
    return result
