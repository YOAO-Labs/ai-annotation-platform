"""Authenticated, complete reads of independently owned audit archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from fastapi import HTTPException

from app.core import token_blacklist
from app.db.enums import UserRole
from app.services import audit_archive_storage as storage, bug_attachment_uploads
from app.services.audit_archive_output_data import list_ready_outputs, lock_ready_output
from app.services import saved_output_io
from app.services import storage_access
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as publications
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
    principal = getattr(request.state, "identity_principal", None)
    if principal is None or principal.actor_id != actor_id:
        reject("audit_archive_authority_revoked", 403)
    if principal.credential_type == "api_key":
        return None
    return JwtProof(
        principal.expires_at, principal.generation, str(principal.credential_id)
    )


def _check_clock(proof):
    expires = proof.original.key_proof[2] if proof.original.key_proof else None
    if (expires is not None and expires <= now()) or (
        proof.jwt is not None and proof.jwt.expires_at <= now()
    ):
        reject("audit_archive_authority_revoked", 403)


async def _jwt_current(db, proof):
    _check_clock(proof)
    jwt = proof.jwt
    if jwt is not None and jwt.jti is not None:
        if await token_blacklist.is_blacklisted(jwt.jti, db=db):
            reject("audit_archive_authority_revoked", 403)
        generation = await token_blacklist.get_user_generation(
            str(proof.original.actor_id), db=db
        )
        if generation > jwt.generation:
            reject("audit_archive_authority_revoked", 403)
    _check_clock(proof)


def _admin(actor):
    if actor.role != UserRole.SUPER_ADMIN:
        reject("audit_archive_authority_revoked", 403)
    return actor


def _authority_error(exc):
    if (
        isinstance(exc.detail, dict)
        and exc.detail.get("code") in {"bug_attachment_authority_revoked", "api_key_authority_revoked"}
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
    await _jwt_current(db, proof)
    return actor, proof


async def reauthorize(db, proof):
    """Recheck the same actor, original Key proof and current super-admin role."""
    try:
        actor = await bug_attachment_uploads.reauthorize(db, proof.original)
    except HTTPException as exc:
        raise _authority_error(exc) from None
    _admin(actor)
    await _jwt_current(db, proof)
    return actor


async def capture_authority(db, actor, request):
    _, proof = await lock_authority(db, actor, request)
    await db.commit()
    _check_clock(proof)
    return proof


def read_purpose(identifier):
    return "audit_archive_read_" + uuid.UUID(str(identifier)).hex


async def _ready_month(db, year, month):
    row = await lock_ready_output(db, year, month)
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
        rows = await list_ready_outputs(db)
        result = []
        for row in rows:
            key = storage.key_for(row.year, row.month, row.id)
            if row.published_at is None or not (
                row.object_key == key
                or (
                    isinstance(row.object_key, str)
                    and row.object_key.endswith("_aap_managed/audit-archive/" + key)
                )
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
        owner = storage.resolve(receipt)
        route = await migrated_reads.resolve(
            db, owner.ref, owner.object_key, receipt.sha256, receipt.size_bytes
        )
        target = await storage.io_target(db, receipt) if route is None else None
        source = saved_output_io.capture(target, "archive", receipt)
        await db.commit()
        _check_clock(proof)
        revision = None

        async def acquired(permit):
            nonlocal revision
            await reauthorize(db, proof)
            await _same_ready(db, receipt)
            if source is not None:
                source.attach(permit)
                await source.admit_read(db, proof.original.actor_id)
            await db.commit()
            _check_clock(proof)
            revision = permit.revision

        if route is not None:
            page = await migrated_reads.run(
                db,
                route,
                read_purpose(receipt.id),
                lambda client: storage.read_from_client(
                    client, receipt, limit=limit, offset=offset
                ),
                on_acquired=acquired,
            )
        else:
            page = await storage_access.run(
                db,
                uuid.UUID(receipt.storage_ref["location_id"]),
                read_purpose(receipt.id),
                storage.read_from_source if source is not None else storage.read,
                receipt,
                limit=limit,
                offset=offset,
                **({"source": source} if source is not None else {}),
                **({"target": target} if target is not None and source is None else {}),
                on_acquired=acquired,
            )
        await reauthorize(db, proof)
        await _same_ready(db, receipt)
        if route is not None:
            await publications.recheck(db, route, lock=True)
        else:
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
