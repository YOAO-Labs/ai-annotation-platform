"""Durable avatar publication and retirement through the Identity owner.

Physical ownership is committed before I/O. The only writers of
``users.avatar_ref`` are the Identity owner commands: the accepted-key proof,
the upload receipt and the expected previous reference travel in the
``set_avatar_ref`` command, and the owner transaction atomically publishes the
new upload, retires the previous owned upload, and moves the account reference.
This module never reads or writes the private ``users`` table.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.avatar_upload import AvatarUpload
from app.services import avatar, avatar_storage, storage_access
from app.services import conditional_write_accounting as accounting
from app.services import object_removal_accounting as removal
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.identity_read_relations import account_profiles

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


def http_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, avatar_storage.AvatarStorageError):
        return HTTPException(exc.status_code, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "avatar_storage_unavailable"})


@dataclass(frozen=True)
class AcceptedKeyProof:
    """Non-secret authority of the exact accepted API key.

    ``revision`` is the Identity owner's digest of the stored verifier, so a
    rotation changes it without this module ever reading or carrying the
    verifier itself. ``scopes`` is the full ordered snapshot, not a required
    subset: a rewritten key must not inherit the original credential's
    publication authority.
    """

    scopes: tuple[str, ...]
    expires_at: datetime | None
    revision: str


@dataclass(frozen=True)
class Authority:
    """Account and credential facts frozen before normalization can outlive them."""

    actor_id: uuid.UUID
    previous_ref: str | None
    key_id: uuid.UUID | None
    key_proof: AcceptedKeyProof | None


def _rejected():
    return http_error(ContentError("avatar_authority_revoked", 401))


def _principal_key(principal):
    if principal.credential_type == "api_key":
        return principal.credential_id, tuple(principal.scopes or ())
    return None, None


def proof_payload(proof):
    """Encode the frozen proof for the owner's atomic recheck."""
    if proof is None:
        return None
    return {
        "scopes": list(proof.scopes),
        "expires_at": proof.expires_at,
        "revision": proof.revision,
    }


async def capture_key_proof(db, actor_id, principal, *, expected=None):
    """Freeze the accepted key's current facts without exposing its verifier.

    The exact scope snapshot, expiry and verifier revision are read through the
    Identity-owned capability. The owner rechecks the same proof inside its own
    publication transaction, so a credential edit between admission and this
    read cannot extend authority.
    """
    key_id, admitted_scopes = _principal_key(principal)
    if expected is not None and expected.key_id != key_id:
        raise _rejected()
    if key_id is None:
        if expected is not None and expected.key_proof is not None:
            raise _rejected()
        return None
    from app.services.identity_account_lock_data import IdentityAccountLockData

    facts = await IdentityAccountLockData(db).read_accepted_api_key_facts(
        actor_id, key_id
    )
    if facts is None:
        raise _rejected()
    proof = AcceptedKeyProof(tuple(facts.scopes), facts.expires_at, facts.revision)
    if expected is not None:
        if expected.key_proof != proof:
            raise _rejected()
    elif proof.scopes != admitted_scopes:
        raise _rejected()
    return proof


async def capture_authority(db, execute, principal, *, expected=None):
    """Read the current account through the owner before any staging work.

    ``execute`` is the endpoint's command runner bound to the original bearer
    credential, so the owner re-authenticates the caller and rejects an
    inactive or revoked account before any immutable target is allocated. The
    same read also freezes the exact accepted-key proof that the final owner
    transaction verifies atomically.
    """
    data = await execute("read_self_account", {})
    account = data.get("account")
    if not isinstance(account, dict):
        raise _rejected()
    try:
        actor_id = uuid.UUID(str(account["id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise _rejected() from None
    if expected is not None and expected.actor_id != actor_id:
        raise _rejected()
    if actor_id != principal.actor_id:
        raise _rejected()
    proof = await capture_key_proof(db, actor_id, principal, expected=expected)
    authority = Authority(
        actor_id, account.get("avatar_ref"), _principal_key(principal)[0], proof
    )
    # Image normalization is CPU work and must not hold the key's SHARE fence.
    if proof is not None:
        await db.commit()
    return authority


def _snapshot(row):
    """Pass immutable receipt values to executor threads, never a live ORM row."""
    avatar_storage.resolve(row)
    return SimpleNamespace(
        token=row.token,
        storage_ref=deepcopy(row.storage_ref),
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        object_version_id=getattr(row, "object_version_id", None),
    )


def receipt(row, maintenance_revision):
    """The provider-confirmed facts the owner must verify before publication.

    ``maintenance_revision`` is the storage admission generation observed when
    the object write was admitted. The owner reacquires the gate at this exact
    revision before it publishes the reference.
    """
    avatar_storage.resolve(row)
    if type(maintenance_revision) is not int or maintenance_revision < 1:
        raise http_error(ContentError("avatar_storage_unavailable", 503))
    return {
        "token": row.token,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "object_version_id": row.object_version_id,
        "maintenance_revision": maintenance_revision,
        "storage_ref": deepcopy(row.storage_ref),
    }


async def _save_write_version(db, receipt, version):
    """Keep a provider receipt even when later user publication is refused."""
    if version is None:
        return receipt
    from app.services.avatar_storage_target import version_id

    version_id({"VersionId": version})
    row = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == receipt.token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or row.state != "pending"
        or _snapshot(row).__dict__ != receipt.__dict__
    ):
        raise http_error(ContentError("avatar_upload_changed", 409))
    row.object_version_id = version
    await db.flush()
    result = _snapshot(row)
    await db.commit()
    return result


def _published_account(result):
    """Require the owner's verified reply shape before trusting any ref."""
    if not isinstance(result, dict):
        raise http_error(ContentError("avatar_authority_revoked", 503))
    account = result.get("account")
    if (
        not isinstance(account, dict)
        or "avatar_ref" not in account
        or not (account["avatar_ref"] is None or isinstance(account["avatar_ref"], str))
    ):
        raise http_error(ContentError("avatar_authority_revoked", 503))
    return account["avatar_ref"]


async def _retire_published(db, result):
    """Retire only the exact previous ref the owner reported as changed.

    The owner transaction already moved the previous upload row to ``retired``.
    This removes its saved object through the durable cleanup path; a failed
    physical delete stays retired for the scheduled retry instead of guessing
    a token or bucket from the request. A no-op publication has no retired row.
    """
    _published_account(result)
    if result.get("changed") is True:
        parsed = avatar.parse_ref(result.get("previous_avatar_ref"))
        if parsed is not None and parsed[0] == "upload":
            await cleanup(db, token=parsed[1])
    return result


async def apply_ref(db, execute, new_ref, principal):
    """Select a preset or clear, then retire only the verified previous upload."""
    try:
        new_ref = avatar.validate_client_ref(new_ref)
    except avatar.InvalidAvatarRef as exc:
        raise HTTPException(400, str(exc)) from None
    proof = await capture_key_proof(db, principal.actor_id, principal)
    if proof is not None:
        # The owner command re-reads the facts in its own transaction; the
        # API-side SHARE fence must not span that independent binding.
        await db.commit()
    result = await execute(
        "set_avatar_ref",
        {
            "avatar_ref": new_ref,
            "kind": "preset_or_clear",
            "bytes": None,
            "accepted_api_key_proof": proof_payload(proof),
        },
    )
    return await _retire_published(db, result)


async def upload(db, execute, data, *, principal, authority=None):
    """Commit ownership before I/O and publish only through the owner command."""
    import hashlib

    authority = authority or await capture_authority(db, execute, principal)
    # Re-authenticate and re-read the expected reference after normalization but
    # before the immutable target is allocated. A credential rotation, scope
    # rewrite or account change during image work must not create pending
    # ownership.
    current = await capture_authority(db, execute, principal, expected=authority)
    if current.previous_ref != authority.previous_ref:
        raise http_error(ContentError("avatar_reference_changed", 409))
    actor_id, previous_ref = authority.actor_id, authority.previous_ref
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
    receipt_before_io = _snapshot(row)
    try:
        target = await avatar_storage.io_target(db, receipt_before_io)
    except Exception as exc:
        raise http_error(exc) from None
    await db.commit()
    recorder = accounting.recorder(
        db,
        receipt_before_io,
        producer="avatar_uploads",
        purpose="other_platform",
        owner_id=uuid.UUID(hex=receipt_before_io.token),
    )
    revision, owned_permit, failure, result = None, None, None, None

    async def acquired(permit):
        nonlocal revision, owned_permit
        owned_permit = permit
        revision = permit.revision

    try:
        try:
            version = await storage_access.run(
                db,
                uuid.UUID(ref["location_id"]),
                "avatar_upload",
                avatar.write_avatar_object,
                receipt_before_io,
                data,
                on_acquired=acquired,
                accounting=recorder,
                **({"target": target} if target is not None else {}),
            )
            confirmed = await _save_write_version(db, receipt_before_io, version)
        except Exception as exc:
            raise http_error(exc) from None
        current = await db.scalar(
            select(AvatarUpload)
            .where(AvatarUpload.token == token)
            .execution_options(populate_existing=True)
        )
        if (
            current is None
            or current.state != "pending"
            or current.owner_id != actor_id
            or _snapshot(current).__dict__ != confirmed.__dict__
        ):
            raise http_error(ContentError("avatar_upload_changed", 409))
        try:
            await storage_access.check(
                db, uuid.UUID(ref["location_id"]), expected_revision=revision, lock=True
            )
        except ContentError as exc:
            raise http_error(exc) from None
        # Release the maintenance gate and row reads before the owner's own
        # transaction: the owner re-verifies this pending receipt atomically, so
        # no API-held lock may span a separately configured Identity connection.
        await db.commit()
        # The owner re-authenticates the original credential, verifies that the
        # frozen previous reference is still current, checks this pending
        # receipt byte-for-byte, rechecks the exact accepted-key proof, then
        # atomically sets users.avatar_ref, retires the old owned upload and
        # marks this one ready in one transaction.
        result = await execute(
            "set_avatar_ref",
            {
                "avatar_ref": avatar.build_upload_ref(token),
                "kind": "upload",
                "bytes": current.size_bytes,
                "expected_previous_avatar_ref": previous_ref,
                "receipt": receipt(current, revision),
                "accepted_api_key_proof": proof_payload(authority.key_proof),
            },
        )
        published, previous = (
            _published_account(result),
            result.get("previous_avatar_ref"),
        )
        if (
            result.get("changed") is not True
            or published != avatar.build_upload_ref(token)
            or previous != previous_ref
        ):
            raise http_error(ContentError("avatar_receipt_invalid", 409))
        result = await _retire_published(db, result)

    except BaseException as exc:
        failure = exc
    failure = await accounting.finish(
        recorder,
        failure,
        settled=owned_permit is not None
        and getattr(owned_permit, "_settled", False) is True,
    )
    if failure is not None:
        raise failure
    return result


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
        receipt_row = _snapshot(row)
        owner = avatar_storage.resolve(receipt_row)
        route = await migrated_reads.resolve(
            db,
            owner.ref,
            owner.object_key,
            receipt_row.sha256,
            receipt_row.size_bytes,
            source_version_id=(
                receipt_row.object_version_id
                if receipt_row.object_version_id is not None
                else publications.ANY_VERSION
            ),
        )
        if route is not None:
            return await migrated_reads.run(
                db,
                route,
                "avatar_read",
                lambda client: avatar.read_from_client(
                    client, receipt_row, response_version_id=route.target_version_id
                ),
            )
        target = await avatar_storage.io_target(db, receipt_row)
        return await storage_access.run(
            db,
            uuid.UUID(receipt_row.storage_ref["location_id"]),
            "avatar_read",
            avatar.read_avatar_object,
            receipt_row,
            **({"target": target} if target is not None else {}),
        )
    except Exception as exc:
        raise http_error(exc) from None


async def _has_reference(db, token):
    if db.autoflush:
        await db.flush()
    return (
        await db.scalar(
            select(account_profiles.c.id)
            .where(account_profiles.c.avatar_ref == avatar.build_upload_ref(token))
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
    receipt_row = _snapshot(row)
    row.last_attempted_at = now()
    # Retired state cannot be reactivated, and user routes cannot select a token.
    # Release business locks before an independently registered physical delete.
    await db.commit()
    target = await avatar_storage.io_target(db, receipt_row)
    recorder = removal.recorder(
        db,
        receipt_row,
        producer="avatar_removals",
        purpose="other_platform",
        owner_id=uuid.UUID(hex=receipt_row.token),
    )
    owned_permit, failure = None, None

    async def acquired(permit):
        nonlocal owned_permit
        owned_permit = permit

    try:
        await storage_access.run(
            db,
            uuid.UUID(receipt_row.storage_ref["location_id"]),
            "avatar_retirement",
            avatar.delete_avatar_object,
            receipt_row,
            on_acquired=acquired,
            accounting=recorder,
            **({"target": target} if target is not None else {}),
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
    current = await db.scalar(
        select(AvatarUpload)
        .where(AvatarUpload.token == token)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        current is not None
        and current.state == "retired"
        and _snapshot(current).__dict__ == receipt_row.__dict__
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
