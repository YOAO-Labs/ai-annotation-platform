"""Durable avatar publication and retirement through the Identity owner.

Physical ownership is committed before I/O. The only writers of
``users.avatar_ref`` are the Identity owner commands: the accepted-key proof,
the upload receipt and the expected previous reference travel in the
``set_avatar_ref`` command, and the owner transaction atomically publishes the
new upload, retires the previous owned upload, and moves the account reference.
This module never reads or writes the private ``users`` table.
"""

from copy import copy, deepcopy
from dataclasses import dataclass
import logging
from types import SimpleNamespace
import uuid

from fastapi import HTTPException

from app.services import avatar, avatar_storage, avatar_upload_data, storage_access
from app.services import conditional_write_accounting as accounting
from app.services import object_removal_accounting as removal
from app.services import saved_output_io
from app.services import storage_migration_artifact_reads as migrated_reads
from app.services import storage_migration_read_publications as publications
from app.services.content_snapshot import ContentError
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import AcceptedApiKeyFacts

logger = logging.getLogger(__name__)


def http_error(exc):
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, avatar_storage.AvatarStorageError):
        return HTTPException(exc.status_code, {"code": exc.code})
    if isinstance(exc, ContentError):
        return HTTPException(exc.status, {"code": exc.code})
    return HTTPException(503, {"code": "avatar_storage_unavailable"})


@dataclass(frozen=True)
class Authority:
    """Account and credential facts frozen before normalization can outlive them."""

    actor_id: uuid.UUID
    previous_ref: str | None
    key_id: uuid.UUID | None
    key_proof: AcceptedApiKeyFacts | None


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
    proof = await IdentityAccountLockData(db).read_accepted_api_key_facts(
        actor_id, key_id
    )
    if proof is None:
        raise _rejected()
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


async def _save_write_version(db, receipt, version, *, actor_id):
    """Keep a provider receipt even when later user publication is refused."""
    if version is None:
        return receipt
    from app.services.avatar_storage_target import version_id

    version_id({"VersionId": version})
    if not await avatar_upload_data.confirm_version(db, actor_id, receipt, version):
        raise http_error(ContentError("avatar_upload_changed", 409))
    result = copy(receipt)
    result.object_version_id = version
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
    current = await capture_authority(db, execute, principal, expected=authority)
    if current.previous_ref != authority.previous_ref:
        raise http_error(ContentError("avatar_reference_changed", 409))
    actor_id, previous_ref = authority.actor_id, authority.previous_ref
    token = avatar.new_upload_token()
    try:
        ref = await avatar_storage.allocate(db, token)
    except (avatar_storage.AvatarStorageError, ContentError) as exc:
        raise http_error(exc) from None
    receipt_before_io = _snapshot(
        SimpleNamespace(
            token=token,
            storage_ref=ref,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
    )
    status = await avatar_upload_data.reserve(db, actor_id, receipt_before_io)
    if status == "avatar_authority_revoked":
        raise _rejected()
    if status == "avatar_storage_binding_unproven":
        raise http_error(avatar_storage.AvatarStorageError(status))
    if status != "reserved":
        raise http_error(ContentError("avatar_storage_unavailable", 503))
    try:
        target = await avatar_storage.io_target(db, receipt_before_io)
    except Exception as exc:
        raise http_error(exc) from None
    await db.commit()
    source = saved_output_io.capture(target, "avatar", receipt_before_io)
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
        if source is not None:
            source.attach(permit)

    try:
        try:
            version = await storage_access.run(
                db,
                uuid.UUID(ref["location_id"]),
                "avatar_upload_" + token if source is not None else "avatar_upload",
                source.write if source is not None else avatar.write_avatar_object,
                *((data,) if source is not None else (receipt_before_io, data)),
                on_acquired=acquired,
                accounting=recorder,
                **({"target": target} if target is not None and source is None else {}),
            )
            confirmed = await _save_write_version(
                db, receipt_before_io, version, actor_id=actor_id
            )
        except Exception as exc:
            raise http_error(exc) from None
        if not await avatar_upload_data.pending_matches(db, actor_id, confirmed):
            raise http_error(ContentError("avatar_upload_changed", 409))
        try:
            await storage_access.check(
                db, uuid.UUID(ref["location_id"]), expected_revision=revision, lock=True
            )
        except ContentError as exc:
            raise http_error(exc) from None
        await db.commit()
        result = await execute(
            "set_avatar_ref",
            {
                "avatar_ref": avatar.build_upload_ref(token),
                "kind": "upload",
                "bytes": confirmed.size_bytes,
                "expected_previous_avatar_ref": previous_ref,
                "receipt": receipt(confirmed, revision),
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
    try:
        row = await avatar_upload_data.read_ready(db, token)
        if row is None:
            return None
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
        source = saved_output_io.capture(target, "avatar", receipt_row)

        async def acquired(permit):
            source.attach(permit)

        return await storage_access.run(
            db,
            uuid.UUID(receipt_row.storage_ref["location_id"]),
            "avatar_read_" + token if source is not None else "avatar_read",
            source.read_avatar if source is not None else avatar.read_avatar_object,
            *(() if source is not None else (receipt_row,)),
            **({"on_acquired": acquired} if source is not None else {}),
            **({"target": target} if target is not None and source is None else {}),
        )
    except Exception as exc:
        raise http_error(exc) from None


async def _cleanup_one(db, token):
    row = await avatar_upload_data.begin_retirement(db, token)
    if row is None:
        await db.commit()
        return False
    receipt_row = _snapshot(row)
    await db.commit()
    target = await avatar_storage.io_target(db, receipt_row)
    source = saved_output_io.capture(target, "avatar", receipt_row)
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
        if source is not None:
            source.attach(permit)

    try:
        await storage_access.run(
            db,
            uuid.UUID(receipt_row.storage_ref["location_id"]),
            "avatar_retirement_" + token if source is not None else "avatar_retirement",
            source.delete if source is not None else avatar.delete_avatar_object,
            *(() if source is not None else (receipt_row,)),
            on_acquired=acquired,
            accounting=recorder,
            **(
                {"version": receipt_row.object_version_id} if source is not None else {}
            ),
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
    deleted = await avatar_upload_data.finish_retirement(db, receipt_row)
    await db.commit()
    return deleted


async def cleanup(db, *, token=None, limit=100):
    """Retry bounded committed retirements; never reap pending uploads by age."""
    if token is not None:
        tokens = [token] if avatar.is_upload_token(token) else []
    else:
        tokens = await avatar_upload_data.list_retired(db, limit)
        await db.commit()
    result = {"deleted": 0, "failed": 0, "retained": 0}
    for candidate in tokens:
        try:
            deleted = await _cleanup_one(db, candidate)
            result["deleted" if deleted else "retained"] += 1
        except Exception:
            await db.rollback()
            result["failed"] += 1
            logger.warning(
                "Avatar retirement retained after cleanup failure: %s", candidate
            )
    return result
