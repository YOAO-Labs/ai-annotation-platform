"""Authenticated, complete reads of independently owned audit archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import uuid

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select

from app.core.security import decode_access_token
from app.core import token_blacklist
from app.db.enums import UserRole
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.services import audit_archive_storage as storage, bug_attachment_uploads
from app.services import storage_access
from app.services.content_snapshot import ContentError


def now():
    return datetime.now(timezone.utc)


def reject(code="audit_archive_changed", status=409):
    raise HTTPException(status, {"code": code})


def http_error(exc):
    if isinstance(exc, storage.AuditArchiveStorageError):
        status = 404 if exc.code == "audit_archive_storage_missing" else exc.status_code
        return HTTPException(status, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "audit_archive_storage_unavailable"})


@dataclass(frozen=True)
class JwtProof:
    expires_at: datetime
    generation: int
    jti: str | None


@dataclass(frozen=True)
class ArchiveAuthority:
    original: bug_attachment_uploads.Authority
    jwt: JwtProof | None


def _jwt_proof(actor_id, request):
    # Capture the presented credentials before the first row-lock await.
    if getattr(request.state, "api_key_id", None) is not None:
        return None
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    try:
        if scheme.lower() != "bearer" or not presented:
            raise ValueError
        claims = decode_access_token(presented)
        if str(uuid.UUID(claims.get("sub", ""))) != str(actor_id):
            raise ValueError
        expiration = claims.get("exp")
        generation = claims.get("gen", 0)
        jti = claims.get("jti")
        if (
            type(expiration) not in {int, float}
            or not math.isfinite(expiration)
            or type(generation) is not int
            or generation < 0
            or (jti is not None and (not isinstance(jti, str) or not jti))
        ):
            raise ValueError
        return JwtProof(
            datetime.fromtimestamp(expiration, timezone.utc), generation, jti
        )
    except (JWTError, ValueError, TypeError, OverflowError, AttributeError):
        reject("audit_archive_authority_revoked", 403)


def _check_clock(proof):
    expires = proof.original.key_proof[2] if proof.original.key_proof else None
    if (expires is not None and expires <= now()) or (
        proof.jwt is not None and proof.jwt.expires_at <= now()
    ):
        reject("audit_archive_authority_revoked", 403)


async def _jwt_current(proof):
    _check_clock(proof)
    jwt = proof.jwt
    if jwt is not None and jwt.jti is not None:
        # Match get_current_user's existing best-effort Redis policy. A cache
        # outage does not introduce a new authorization requirement here.
        try:
            if await token_blacklist.is_blacklisted(jwt.jti):
                reject("audit_archive_authority_revoked", 403)
        except HTTPException:
            raise
        except Exception:
            pass
        try:
            generation = await token_blacklist.get_user_generation(
                str(proof.original.actor_id)
            )
            if generation > jwt.generation:
                reject("audit_archive_authority_revoked", 403)
        except HTTPException:
            raise
        except Exception:
            pass
    _check_clock(proof)


def _admin(actor):
    if actor.role != UserRole.SUPER_ADMIN:
        reject("audit_archive_authority_revoked", 403)
    return actor


def _authority_error(exc):
    if (
        isinstance(exc.detail, dict)
        and exc.detail.get("code") == "bug_attachment_authority_revoked"
    ):
        return HTTPException(403, {"code": "audit_archive_authority_revoked"})
    return exc


async def lock_authority(db, actor, request):
    """Lock the original credentials and current role without committing."""
    actor_id = actor.id
    jwt = _jwt_proof(actor_id, request)
    try:
        actor, original = await bug_attachment_uploads.lock_authority(
            db, actor, request
        )
    except HTTPException as exc:
        raise _authority_error(exc) from None
    proof = ArchiveAuthority(original, jwt)
    _admin(actor)
    await _jwt_current(proof)
    return actor, proof


async def reauthorize(db, proof):
    """Recheck the same actor, original Key proof and current super-admin role."""
    try:
        actor = await bug_attachment_uploads.reauthorize(db, proof.original)
    except HTTPException as exc:
        raise _authority_error(exc) from None
    _admin(actor)
    await _jwt_current(proof)
    return actor


async def capture_authority(db, actor, request):
    _, proof = await lock_authority(db, actor, request)
    await db.commit()
    _check_clock(proof)
    return proof


def read_purpose(identifier):
    return "audit_archive_read_" + uuid.UUID(str(identifier)).hex


async def _ready_month(db, year, month):
    result = await db.execute(
        select(AuditArchiveOutput)
        .where(
            AuditArchiveOutput.year == year,
            AuditArchiveOutput.month == month,
            AuditArchiveOutput.state == "ready",
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is None:
        reject("audit_archive_storage_unpinned")
    return row


async def _same_ready(db, receipt):
    current = await _ready_month(db, receipt.year, receipt.month)
    if current.id != receipt.id or not storage.same(current, receipt):
        reject()
    return current


async def list_archives(db, actor, request):
    """List only published database owners; never probe the current bucket."""
    try:
        _, proof = await lock_authority(db, actor, request)
        rows = list(
            await db.scalars(
                select(AuditArchiveOutput)
                .where(AuditArchiveOutput.state == "ready")
                .order_by(
                    AuditArchiveOutput.year.desc(), AuditArchiveOutput.month.desc()
                )
            )
        )
        result = []
        for row in rows:
            key = storage.key_for(row.year, row.month, row.id)
            if (
                not isinstance(row.storage_ref, dict)
                or row.storage_ref.get("object_key") != key
                or row.published_at is None
            ):
                reject()
            result.append(
                {
                    "year": row.year,
                    "month": row.month,
                    "object_key": key,
                    "size_bytes": row.size_bytes,
                    "archived_at": row.published_at.isoformat(),
                }
            )
        await reauthorize(db, proof)
        await db.commit()
        _check_clock(proof)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None


async def read_archive_rows(db, actor, request, year, month, *, limit=100, offset=0):
    """Verify the complete saved archive, then release only the requested page."""
    try:
        proof = await capture_authority(db, actor, request)
        await reauthorize(db, proof)
        receipt = storage.snapshot(await _ready_month(db, year, month))
        await db.commit()
        _check_clock(proof)
        revision = None

        async def acquired(permit):
            nonlocal revision
            await reauthorize(db, proof)
            await _same_ready(db, receipt)
            await db.commit()
            _check_clock(proof)
            revision = permit.revision

        page = await storage_access.run(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            read_purpose(receipt.id),
            storage.read,
            receipt,
            limit=limit,
            offset=offset,
            on_acquired=acquired,
        )
        await reauthorize(db, proof)
        await _same_ready(db, receipt)
        await storage_access.check(
            db,
            uuid.UUID(receipt.storage_ref["location_id"]),
            expected_revision=revision,
            lock=True,
        )
        await reauthorize(db, proof)
        await db.commit()
        _check_clock(proof)
        return {
            "items": page["items"],
            "total": page["total"],
            "limit": limit,
            "offset": offset,
            "year": year,
            "month": month,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(exc) from None
