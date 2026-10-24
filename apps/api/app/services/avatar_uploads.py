"""Durable avatar publication and retirement, independent of the user's next ref."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import verify_password
from app.db.models.api_key import ApiKey
from app.db.models.avatar_upload import AvatarUpload
from app.db.models.user import User
from app.services import avatar, avatar_storage, storage_access
from app.services.audit import AuditAction, AuditService
from app.services.content_snapshot import ContentError

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def http_error(exc):
    if isinstance(exc, avatar_storage.AvatarStorageError):
        return HTTPException(exc.status_code, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "avatar_storage_unavailable"})


def _key_id(request):
    value = getattr(request.state, "api_key_id", None)
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(403, {"code": "api_key_authority_revoked"}) from None


async def _key_proof(db, actor_id, key_id, *, lock=False):
    """Preserve this unscoped endpoint's authority without adding a new scope."""
    if key_id is None:
        return None
    stmt = select(ApiKey).where(ApiKey.id == key_id)
    if lock:
        stmt = stmt.with_for_update(read=True)
    key = await db.scalar(stmt.execution_options(populate_existing=True))
    if (
        key is None
        or key.user_id != actor_id
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now())
    ):
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    return key.key_hash, tuple(key.scopes or ()), key.expires_at


async def _authenticated_proof(db, actor_id, request):
    key_id = _key_id(request)
    proof = await _key_proof(db, actor_id, key_id, lock=True)
    if proof is not None:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        scopes = tuple(getattr(request.state, "api_key_scopes", None) or ())
        try:
            valid = scheme.lower() == "bearer" and verify_password(presented, proof[0])
        except (ValueError, TypeError):
            valid = False
        if not valid or scopes != proof[1]:
            raise HTTPException(403, {"code": "api_key_authority_revoked"})
    return key_id, proof


@dataclass(frozen=True)
class Authority:
    actor_id: uuid.UUID
    previous_ref: str | None
    key_id: uuid.UUID | None
    key_proof: tuple | None


async def capture_authority(db, actor, request):
    """Freeze authentication before normalization can outlive its owner."""
    actor = await _actor(db, actor.id)
    key_id, proof = await _authenticated_proof(db, actor.id, request)
    result = Authority(actor.id, actor.avatar_ref, key_id, proof)
    # Image normalization is CPU work and must not hold account/key row locks.
    await db.commit()
    return result


async def _actor(db, identifier):
    actor = await db.scalar(
        select(User)
        .where(User.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor is None or not actor.is_active:
        raise HTTPException(403, {"code": "avatar_authority_revoked"})
    return actor


def _snapshot(row):
    """Pass immutable receipt values to executor threads, never a live ORM row."""
    avatar_storage.resolve(row)
    return SimpleNamespace(
        token=row.token,
        storage_ref=deepcopy(row.storage_ref),
        sha256=row.sha256,
        size_bytes=row.size_bytes,
    )


async def _retire_previous(db, actor, previous_ref):
    parsed = avatar.parse_ref(previous_ref)
    if parsed is None or parsed[0] != "upload":
        return None
    row = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == parsed[1])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Unknown historical refs and inconsistent ownership never become delete
    # authority. Clearing a user's ref does not require guessing their bytes.
    if row is None or row.owner_id != actor.id or row.state != "ready":
        return None
    row.state, row.retired_at = "retired", now()
    return row.token


async def _publish_ref(db, actor, new_ref, previous_ref, request, detail):
    retired = await _retire_previous(db, actor, previous_ref)
    actor.avatar_ref = new_ref
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_PROFILE_UPDATE,
        target_type="user",
        target_id=str(actor.id),
        request=request,
        status_code=200,
        detail={"field": "avatar", "avatar_ref": new_ref, **detail},
    )
    # Do not clean a candidate after a failed/uncertain commit. The immutable
    # pending or ready receipt remains available for explicit reconciliation.
    await db.commit()
    if retired is not None:
        await cleanup(db, token=retired)
    # A failed cleanup rolls this session back and expires ORM attributes.
    # Refresh afterward so the already committed UserOut remains serializable.
    await db.refresh(actor)
    return actor


async def apply_ref(db, actor, new_ref, request, *, detail):
    """Select a preset or clear, atomically retiring only a known old object."""
    try:
        new_ref = avatar.validate_client_ref(new_ref)
    except avatar.InvalidAvatarRef as exc:
        raise HTTPException(400, str(exc)) from None
    actor = await _actor(db, actor.id)
    await _authenticated_proof(db, actor.id, request)
    previous_ref = actor.avatar_ref
    if previous_ref == new_ref:
        await db.commit()
        return actor
    return await _publish_ref(db, actor, new_ref, previous_ref, request, detail)


async def upload(db, actor, data, request, *, authority=None):
    """Commit ownership before I/O and publish only after all fences hold."""
    import hashlib

    authority = authority or await capture_authority(db, actor, request)
    actor = await _actor(db, actor.id)
    actor_id, previous_ref = authority.actor_id, authority.previous_ref
    key_id, key_proof = authority.key_id, authority.key_proof
    if actor.id != actor_id or _key_id(request) != key_id:
        raise HTTPException(403, {"code": "avatar_authority_revoked"})
    if await _key_proof(db, actor_id, key_id, lock=True) != key_proof:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    if actor.avatar_ref != previous_ref:
        raise HTTPException(409, {"code": "avatar_reference_changed"})
    token = avatar.new_upload_token()
    try:
        ref = await avatar_storage.allocate(db, token)
    except (avatar_storage.AvatarStorageError, ContentError) as exc:
        raise http_error(exc) from None
    row = AvatarUpload(
        token=token,
        owner_id=actor_id,
        storage_ref=ref,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        state="pending",
    )
    db.add(row)
    await db.flush()
    receipt = _snapshot(row)
    await db.commit()
    revision = None

    async def acquired(permit):
        nonlocal revision
        revision = permit.revision

    try:
        await storage_access.run(
            db,
            uuid.UUID(ref["location_id"]),
            "avatar_upload",
            avatar.write_avatar_object,
            receipt,
            data,
            on_acquired=acquired,
        )
    except Exception as exc:
        # Failed publication never authorizes physical deletion. An unknown
        # storage outcome retains both the pending row and any unsettled permit.
        raise http_error(exc) from None

    actor = await _actor(db, actor_id)
    if await _key_proof(db, actor_id, key_id, lock=True) != key_proof:
        raise HTTPException(403, {"code": "api_key_authority_revoked"})
    if actor.avatar_ref != previous_ref:
        raise HTTPException(409, {"code": "avatar_reference_changed"})
    current = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        current is None
        or current.state != "pending"
        or current.owner_id != actor_id
        or _snapshot(current).__dict__ != receipt.__dict__
    ):
        raise HTTPException(409, {"code": "avatar_upload_changed"})
    try:
        await storage_access.check(
            db, uuid.UUID(ref["location_id"]), expected_revision=revision, lock=True
        )
    except ContentError as exc:
        raise http_error(exc) from None
    current.state = "ready"
    return await _publish_ref(
        db,
        actor,
        avatar.build_upload_ref(token),
        previous_ref,
        request,
        {"kind": "upload", "bytes": len(data)},
    )


async def read(db, token):
    """Anonymous token reads resolve only a ready, independently saved receipt."""
    row = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == token, AvatarUpload.state == "ready")
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    try:
        receipt = _snapshot(row)
        return await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            "avatar_read",
            avatar.read_avatar_object,
            receipt,
        )
    except Exception as exc:
        raise http_error(exc) from None


async def _has_reference(db, token):
    return (
        await db.scalar(
            select(User.id)
            .where(User.avatar_ref == avatar.build_upload_ref(token))
            .limit(1)
        )
        is not None
    )


async def _cleanup_one(db, token):
    row = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == token, AvatarUpload.state == "retired")
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if row is None or await _has_reference(db, token):
        await db.commit()
        return False
    receipt = _snapshot(row)
    row.last_attempted_at = now()
    # Retired state cannot be reactivated, and user routes cannot select a token.
    # Release business locks before an independently registered physical delete.
    await db.commit()
    await storage_access.run(
        db,
        uuid.UUID(receipt.storage_ref["location_id"]),
        "avatar_retirement",
        avatar.delete_avatar_object,
        receipt,
    )
    current = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        current is not None
        and current.state == "retired"
        and _snapshot(current).__dict__ == receipt.__dict__
        and not await _has_reference(db, token)
    ):
        await db.delete(current)
        await db.commit()
        return True
    await db.commit()
    return False


async def cleanup(db, *, token=None, limit=100):
    """Retry bounded committed retirements; never reap pending uploads by age."""
    if token is not None:
        tokens = [token] if avatar.is_upload_token(token) else []
    else:
        tokens = list(
            await db.scalars(
                select(AvatarUpload.token)
                .where(AvatarUpload.state == "retired")
                .order_by(
                    AvatarUpload.last_attempted_at.asc().nulls_first(),
                    AvatarUpload.created_at,
                    AvatarUpload.token,
                )
                .limit(max(1, min(limit, 100)))
            )
        )
        await db.commit()
    result = {"deleted": 0, "failed": 0, "retained": 0}
    for candidate in tokens:
        try:
            deleted = await _cleanup_one(db, candidate)
            result["deleted" if deleted else "retained"] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            # Storage exceptions can include credentials. Keep only the public
            # random token, never driver messages or signed request details.
            logger.warning(
                "Avatar retirement retained after cleanup failure: %s", candidate
            )
    return result
