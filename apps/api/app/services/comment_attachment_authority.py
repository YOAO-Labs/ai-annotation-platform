"""Original credentials and short, ordered locks for private file flows."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import uuid

from fastapi import HTTPException
from jose import JWTError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core import token_blacklist
from app.core.security import decode_access_token
from app.db.enums import PlatformRole
from app.services import storage_access
from app.services.identity_account_lock_data import IdentityAccountLockData


ALLOWED_ROLES = frozenset(
    {
        PlatformRole.SUPER_ADMIN,
        PlatformRole.PROJECT_ADMIN,
        PlatformRole.EMPLOYEE,
    }
)
AUTHORITY_LOCK_TIMEOUT = "1s"
# Accepted-key facts no longer expose credential material. The Identity owner
# confirms the same key row under its own SHARE fence on every reauthorization.
KEY_ACCEPTED = (True, (), None)


def now():
    return datetime.now(timezone.utc)


def reject(code="comment_attachment_changed", status=409):
    raise HTTPException(status, {"code": code})


def _busy(exc):
    return (
        storage_access.unavailable(exc)
        or getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}
        or getattr(exc.orig, "pgcode", None) in {"55P03", "40001", "40P01"}
    )


async def locked(db, model, identifier, *, read=True, skip_locked=False, wait=False):
    """Lock one non-core private row in the caller transaction."""
    try:
        with db.no_autoflush:
            previous_timeout = None
            if wait:
                previous_timeout = await db.scalar(text("SHOW lock_timeout"))
                await db.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": AUTHORITY_LOCK_TIMEOUT},
                )
            row = await db.scalar(
                select(model)
                .where(model.id == identifier)
                .with_for_update(
                    read=read, nowait=not (wait or skip_locked), skip_locked=skip_locked
                )
                .execution_options(populate_existing=True)
            )
            if previous_timeout is not None:
                await db.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": previous_timeout},
                )
            return row
    except DBAPIError as exc:
        if _busy(exc):
            reject("comment_attachment_busy")
        reject("comment_attachment_unavailable", 503)


@dataclass(frozen=True)
class JwtProof:
    expires_at: datetime
    generation: int
    jti: str | None


@dataclass(frozen=True)
class Authority:
    actor_id: uuid.UUID
    key_id: uuid.UUID | None
    key_proof: tuple | None
    jwt: JwtProof | None
    allowed_roles: frozenset[PlatformRole] = ALLOWED_ROLES
    expires_at: datetime | None = None


def _presented(actor_id, request):
    """Capture admitted request facts before any application lock await.

    The bearer was already authenticated by the Identity admission boundary,
    which records the verified principal (including API-key scopes and expiry)
    on the request. No credential row is read here.
    """
    principal = getattr(request.state, "identity_principal", None)
    state_key_id = getattr(request.state, "api_key_id", None)
    state_scopes = getattr(request.state, "api_key_scopes", None)
    if principal is not None:
        if str(principal.actor_id) != str(actor_id):
            reject("comment_attachment_authority_revoked", 403)
        if principal.credential_type == "api_key":
            if str(principal.credential_id) != str(state_key_id):
                reject("api_key_authority_revoked", 403)
            scopes = tuple(principal.scopes or ())
            if state_scopes is not None and tuple(state_scopes) != scopes:
                reject("api_key_authority_revoked", 403)
            return principal.credential_id, scopes, principal.expires_at, None
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        reject("comment_attachment_authority_revoked", 403)
    if state_key_id is not None:
        try:
            key_id = uuid.UUID(str(state_key_id))
        except (ValueError, TypeError, AttributeError):
            reject("api_key_authority_revoked", 403)
        return key_id, tuple(state_scopes or ()), None, None
    if state_scopes is not None:
        reject("api_key_authority_revoked", 403)
    try:
        claims = decode_access_token(token)
        expiration, generation, jti = (
            claims.get("exp"),
            claims.get("gen", 0),
            claims.get("jti"),
        )
        if (
            str(uuid.UUID(claims.get("sub", ""))) != str(actor_id)
            or type(expiration) not in {int, float}
            or not math.isfinite(expiration)
            or type(generation) is not int
            or generation < 0
            or (jti is not None and (not isinstance(jti, str) or not jti))
        ):
            raise ValueError
        return (
            None,
            None,
            None,
            JwtProof(datetime.fromtimestamp(expiration, timezone.utc), generation, jti),
        )
    except (JWTError, ValueError, TypeError, OverflowError, AttributeError):
        reject("comment_attachment_authority_revoked", 403)


def check_clock(proof):
    expiration = proof.expires_at
    if expiration is None and proof.key_proof is not None:
        expiration = proof.key_proof[2]
    if (expiration is not None and expiration <= now()) or (
        proof.jwt is not None and proof.jwt.expires_at <= now()
    ):
        reject("comment_attachment_authority_revoked", 403)


async def _jwt_current(db, proof):
    check_clock(proof)
    if proof.jwt is not None and proof.jwt.jti is not None:
        # Recheck Identity in the transaction holding the account SHARE fence.
        if await token_blacklist.is_blacklisted(proof.jwt.jti, db=db):
            reject("comment_attachment_authority_revoked", 403)
        if (
            await token_blacklist.get_user_generation(str(proof.actor_id), db=db)
            > proof.jwt.generation
        ):
            reject("comment_attachment_authority_revoked", 403)
    check_clock(proof)


async def _actor(db, actor_id, allowed_roles=ALLOWED_ROLES):
    """Current account facts under Identity's own SHARE fence."""
    try:
        with db.no_autoflush:
            accounts = await IdentityAccountLockData(db).lock_accounts(
                [actor_id] if actor_id is not None else [], mode="share", nowait=True
            )
    except DBAPIError as exc:
        if _busy(exc):
            reject("comment_attachment_busy")
        reject("comment_attachment_unavailable", 503)
    actor = accounts.get(actor_id)
    allowed = {role.value for role in allowed_roles}
    if actor is None or not actor.is_active or actor.role not in allowed:
        reject("comment_attachment_authority_revoked", 403)
    return actor


async def _key(db, actor_id, identifier, *, required_scopes=()):
    """Read the accepted key's current non-secret facts under Identity's fence."""
    if identifier is None:
        return None
    try:
        with db.no_autoflush:
            facts = await IdentityAccountLockData(db).read_accepted_api_key_facts(
                actor_id, identifier
            )
    except DBAPIError as exc:
        if _busy(exc):
            reject("comment_attachment_busy")
        reject("comment_attachment_unavailable", 503)
    if facts is None:
        reject("api_key_authority_revoked", 403)
    if any(
        "*" not in facts.scopes and scope not in facts.scopes
        for scope in required_scopes
    ):
        reject("api_key_authority_revoked", 403)
    return (facts.revision, facts.scopes, facts.expires_at)


async def lock_authority(db, actor, request, *, allowed_roles=ALLOWED_ROLES):
    actor_id = actor.id
    key_id, scopes, _, jwt = _presented(actor_id, request)
    allowed_roles = frozenset(allowed_roles)
    actor = await _actor(db, actor_id, allowed_roles)
    key = await _key(db, actor_id, key_id)
    if key_id is not None:
        # Restore the exact stored-facts match the admission captured: a scope
        # edit between admission and this lock revokes the accepted credential.
        if key is None or tuple(key[1]) != tuple(scopes or ()):
            reject("api_key_authority_revoked", 403)
    proof = Authority(
        actor_id,
        key_id,
        key,
        jwt,
        allowed_roles,
        key[2] if key is not None else None,
    )
    await _jwt_current(db, proof)
    return actor, proof


async def reauthorize(db, proof):
    actor = await _actor(db, proof.actor_id, proof.allowed_roles)
    if await _key(db, proof.actor_id, proof.key_id) != proof.key_proof:
        reject("api_key_authority_revoked", 403)
    await _jwt_current(db, proof)
    return actor


@dataclass(frozen=True)
class DiscussionContext:
    annotation_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    facts: tuple
    task: object
    annotation: object
    access: object


async def context(db, actor, annotation_id, *, active=False, expected=None):
    """Locate first, then lock one unchanged actual relationship in global order."""
    from app.services import task_access
    from app.services.labeling_core_read_relations import (
        annotation_row,
        lock_annotation_rows_for_core_read,
        lock_batch_rows_for_core_read,
        lock_task_for_core_read,
        task_row,
    )
    from app.services.project_access import (
        lock_current_project_rows,
        resolve_project_access,
    )

    try:
        with db.no_autoflush:
            # Scalar locator values cannot be mutated by populate_existing later.
            located = (
                await db.execute(
                    select(
                        annotation_row.task_id,
                        task_row.project_id,
                        task_row.batch_id,
                    )
                    .join(task_row, task_row.id == annotation_row.task_id)
                    .where(annotation_row.id == annotation_id)
                )
            ).one_or_none()
            if located is None:
                reject("comment_attachment_context_missing", 404)
            task_id, project_id, batch_id = located
            try:
                _, project = await lock_current_project_rows(
                    db, actor_id=actor.id, project_id=project_id
                )
            except HTTPException as exc:
                if exc.status_code == 404:
                    reject("comment_attachment_context_missing", 404)
                if exc.status_code in {401, 403}:
                    reject("comment_attachment_authority_revoked", 403)
                if exc.status_code == 409:
                    reject("comment_attachment_busy")
                reject("comment_attachment_unavailable", 503)
            access = await resolve_project_access(
                db, actor_id=actor.id, project=project, lock_membership=True
            )
            batch = (
                (
                    await lock_batch_rows_for_core_read(
                        db,
                        [batch_id],
                        project_id=project_id,
                        lock_mode="share",
                        nowait=True,
                    )
                )[0]
                if batch_id is not None
                else None
            )
            task = await lock_task_for_core_read(
                db,
                task_id,
                project_id=project_id,
                lock_mode="share",
                nowait=True,
                populate_existing=True,
            )
            annotations = await lock_annotation_rows_for_core_read(
                db,
                [annotation_id],
                task_id=task_id,
                project_id=project_id,
                lock_mode="share",
                nowait=True,
                populate_existing=True,
            )
            annotation = annotations[0] if annotations else None
            if task is None or annotation is None:
                reject("comment_attachment_context_missing", 404)
            if (
                task.project_id != project_id
                or task.batch_id != batch_id
                or annotation.task_id != task_id
                or (
                    batch_id is not None
                    and (batch is None or batch.project_id != project_id)
                )
            ):
                reject("comment_attachment_context_changed")
            if active and (not annotation.is_active or bool(annotation.was_cancelled)):
                reject("comment_attachment_annotation_inactive", 404)
            await task_access.assert_task_visible(db, task, actor.id, access=access)
            facts = (
                project_id,
                project.owner_id,
                actor.role,
                json.dumps(project.video_collaboration or {}, sort_keys=True),
                (access.membership_id, access.project_role, access.membership_version)
                if access.membership_id is not None
                else None,
                task_id,
                batch_id,
                task.status,
                task.assignee_id,
                task.reviewer_id,
                task.file_type,
                (batch.id, batch.status, batch.annotator_id, batch.reviewer_id)
                if batch is not None
                else None,
                annotation_id,
                annotation.is_active,
                annotation.was_cancelled,
            )
            if expected is not None and facts != expected.facts:
                reject("comment_attachment_context_changed")
            return DiscussionContext(
                annotation_id, task_id, project_id, facts, task, annotation, access
            )
    except DBAPIError as exc:
        if _busy(exc):
            reject("comment_attachment_busy")
        reject("comment_attachment_unavailable", 503)
