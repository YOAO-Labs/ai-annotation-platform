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
from app.core.security import decode_access_token, verify_password
from app.db.enums import PlatformRole
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import storage_access


ALLOWED_ROLES = frozenset(
    {
        PlatformRole.SUPER_ADMIN,
        PlatformRole.PROJECT_ADMIN,
        PlatformRole.EMPLOYEE,
    }
)
AUTHORITY_LOCK_TIMEOUT = "1s"


def now():
    return datetime.now(timezone.utc)


def reject(code="comment_attachment_changed", status=409):
    raise HTTPException(status, {"code": code})


async def locked(db, model, identifier, *, read=True, skip_locked=False, wait=False):
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
        if storage_access.unavailable(exc):
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


def _presented(actor_id, request):
    """Capture original request facts before any application lock await."""
    key_id = getattr(request.state, "api_key_id", None)
    scopes = getattr(request.state, "api_key_scopes", None)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        reject("comment_attachment_authority_revoked", 403)
    if key_id is not None:
        try:
            return uuid.UUID(str(key_id)), tuple(scopes or ()), token, None
        except (ValueError, TypeError, AttributeError):
            reject("api_key_authority_revoked", 403)
    if scopes is not None:
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
            token,
            JwtProof(datetime.fromtimestamp(expiration, timezone.utc), generation, jti),
        )
    except (JWTError, ValueError, TypeError, OverflowError, AttributeError):
        reject("comment_attachment_authority_revoked", 403)


def check_clock(proof):
    expiration = proof.key_proof[2] if proof.key_proof else None
    if (expiration is not None and expiration <= now()) or (
        proof.jwt is not None and proof.jwt.expires_at <= now()
    ):
        reject("comment_attachment_authority_revoked", 403)


async def _jwt_current(proof):
    check_clock(proof)
    if proof.jwt is not None and proof.jwt.jti is not None:
        # Preserve get_current_user's best-effort Redis policy, including legacy
        # tokens without jti, rather than adding a new cache dependency.
        try:
            if await token_blacklist.is_blacklisted(proof.jwt.jti):
                reject("comment_attachment_authority_revoked", 403)
        except HTTPException:
            raise
        except Exception:
            pass
        try:
            if (
                await token_blacklist.get_user_generation(str(proof.actor_id))
                > proof.jwt.generation
            ):
                reject("comment_attachment_authority_revoked", 403)
        except HTTPException:
            raise
        except Exception:
            pass
    check_clock(proof)


async def _actor(db, actor_id, allowed_roles=ALLOWED_ROLES):
    actor = await locked(db, User, actor_id, wait=True)
    if actor is None or not actor.is_active or actor.role not in allowed_roles:
        reject("comment_attachment_authority_revoked", 403)
    return actor


async def _key(db, actor_id, identifier):
    if identifier is None:
        return None
    key = await locked(db, ApiKey, identifier)
    if (
        key is None
        or key.user_id != actor_id
        or key.revoked_at is not None
        or (key.expires_at is not None and key.expires_at <= now())
    ):
        reject("api_key_authority_revoked", 403)
    return key.key_hash, tuple(key.scopes or ()), key.expires_at


async def lock_authority(db, actor, request, *, allowed_roles=ALLOWED_ROLES):
    actor_id = actor.id
    key_id, scopes, token, jwt = _presented(actor_id, request)
    allowed_roles = frozenset(allowed_roles)
    actor = await _actor(db, actor_id, allowed_roles)
    key = await _key(db, actor_id, key_id)
    if key is not None:
        try:
            valid = verify_password(token, key[0])
        except (ValueError, TypeError):
            valid = False
        if not valid or key[1] != scopes:
            reject("api_key_authority_revoked", 403)
    proof = Authority(actor_id, key_id, key, jwt, allowed_roles)
    await _jwt_current(proof)
    return actor, proof


async def reauthorize(db, proof):
    actor = await _actor(db, proof.actor_id, proof.allowed_roles)
    if await _key(db, proof.actor_id, proof.key_id) != proof.key_proof:
        reject("api_key_authority_revoked", 403)
    await _jwt_current(proof)
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
    from app.api.v1.tasks._shared import _assert_task_visible
    from app.services.project_access import resolve_project_access

    with db.no_autoflush:
        # Scalar locator values cannot be mutated by populate_existing later.
        located = (
            await db.execute(
                select(Annotation.task_id, Task.project_id, Task.batch_id)
                .join(Task, Task.id == Annotation.task_id)
                .where(Annotation.id == annotation_id)
            )
        ).one_or_none()
        if located is None:
            reject("comment_attachment_context_missing", 404)
        task_id, project_id, batch_id = located
        project = await locked(db, Project, project_id)
        if project is None:
            reject("comment_attachment_context_missing", 404)
        try:
            member = await db.scalar(
                select(ProjectMember)
                .where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == actor.id,
                )
                .with_for_update(read=True, nowait=True)
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            if storage_access.unavailable(exc):
                reject("comment_attachment_busy")
            reject("comment_attachment_unavailable", 503)
        batch = await locked(db, TaskBatch, batch_id) if batch_id is not None else None
        task = await locked(db, Task, task_id)
        annotation = await locked(db, Annotation, annotation_id)
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
        access = await resolve_project_access(db, user=actor, project=project)
        await _assert_task_visible(db, task, actor, access=access)
        facts = (
            project_id,
            project.owner_id,
            actor.role,
            json.dumps(project.video_collaboration or {}, sort_keys=True),
            (member.id, member.role, member.version) if member is not None else None,
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
