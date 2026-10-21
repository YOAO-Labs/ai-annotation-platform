"""SQL-backed workspace access resolution and transaction locks."""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_workspace_contract.access import (
    ProjectAccess as _ProjectAccess,
    ProjectAccessPolicyFailure,
    project_manager_access,
    project_member_access,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
    ProjectWritePolicyResolver,
    membership_role_compatible as _membership_role_compatible,
    platform_role_is_manager as _platform_role_is_manager,
)
from app.db.enums import (
    PlatformRole,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.task_creation_contract import (
    ProjectManagerFacts,
    decode_creation_reply,
)
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.identity_account_lock_data import IdentityAccountLockData


async def lock_creation_project_manager(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> ProjectManagerFacts:
    """Resolve manager facts through the Workspace-owned data capability."""
    with db.no_autoflush:
        payload = await db.scalar(
            text(
                "SELECT aap_workspace_v1.lock_project_manager("
                "CAST(:actor_id AS uuid), CAST(:project_id AS uuid))"
            ),
            {"actor_id": actor_id, "project_id": project_id},
        )
    result = decode_creation_reply(
        payload, "aap-project-manager/v1", ProjectManagerFacts
    )
    if result.actor_id != actor_id or result.project_id != project_id:
        raise ValueError("Creation authority does not match its request")
    return result


def assert_membership_role_compatible(platform_role: str, project_role: str) -> None:
    if _membership_role_compatible(platform_role, project_role):
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，"
            "平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
        ),
    )


def _invalid_account() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="账号已停用或不存在",
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_privileged_for_project(
    user: IdentityActorFacts | IdentityActorStatus, project: Project
) -> bool:
    """An active super administrator or a project owner with an admin role."""

    if not user.is_active:
        return False
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return True
    return _platform_role_is_manager(user.role) and project.owner_id == user.id


async def resolve_project_access(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project: Project,
    lock_membership: bool = False,
) -> _ProjectAccess:
    """Resolve the immutable access context for ``actor_id`` on ``project``.

    Always re-reads current account and project state so a revoked account or a
    transferred project cannot authorize from a stale identity map entry.

    ``lock_membership=True`` fences account, project and membership authority in
    that order, so disable, ownership transfer and role change cannot commit
    between authorization and the caller's write.

    Raises 401 for an inactive/missing account, 404 when the account cannot see
    the project at all (existence is hidden) and 403 when a visible membership
    is internally inconsistent.
    """
    if lock_membership:
        _, _, access = await lock_current_project_access(
            db, actor_id=actor_id, project_id=project.id
        )
        return access

    actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
    if actor is None or not actor.is_active:
        raise _invalid_account()
    fresh_project = await db.get(Project, project.id, populate_existing=True)
    if fresh_project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    return await _resolve_current_project_access(
        db, user=actor, project=fresh_project, lock_membership=lock_membership
    )


async def _resolve_current_project_access(
    db: AsyncSession,
    *,
    user: IdentityActorFacts | IdentityActorStatus,
    project: Project,
    lock_membership: bool,
    nowait: bool = False,
) -> _ProjectAccess:
    try:
        manager = project_manager_access(
            actor_id=user.id,
            platform_role=user.role,
            project_id=project.id,
            owner_id=project.owner_id,
        )
        if manager is not None:
            return manager

        # Retain the caller's refreshed row and optional lock before pure policy.
        stmt = (
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == user.id,
            )
            .execution_options(populate_existing=True)
        )
        if lock_membership:
            stmt = stmt.with_for_update(read=True, nowait=nowait)
        if nowait:
            member = await _locked_access_row(db, stmt, busy_reason="membership_busy")
        else:
            member = (await db.execute(stmt)).scalar_one_or_none()
        return project_member_access(
            actor_id=user.id,
            platform_role=user.role,
            project_id=project.id,
            membership_id=member.id if member is not None else None,
            project_role=member.role if member is not None else None,
            membership_version=member.version if member is not None else None,
        )
    except ProjectAccessPolicyFailure as failure:
        raise HTTPException(
            status_code=failure.status_code, detail=failure.detail
        ) from failure


async def _locked_access_row(db: AsyncSession, statement, *, busy_reason: str):
    try:
        return (await db.execute(statement)).scalar_one_or_none()
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": busy_reason}
            ) from exc
        raise


async def lock_current_project_rows(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[IdentityActorFacts, Project]:
    """Refresh and fence account/project rows without granting project visibility."""
    with db.no_autoflush:
        try:
            accounts = await IdentityAccountLockData(db).lock_accounts(
                [actor_id], mode="share", nowait=True
            )
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
                exc.orig, "pgcode", None
            )
            if sqlstate in {"55P03", "40001", "40P01"}:
                raise HTTPException(
                    status_code=409, detail={"reason": "account_busy"}
                ) from exc
            raise
        user = accounts.get(actor_id)
        if user is None or not user.is_active:
            raise _invalid_account()
        project = await _locked_access_row(
            db,
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(read=True, key_share=True, nowait=True)
            .execution_options(populate_existing=True),
            busy_reason="project_busy",
        )
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
    return user, project


async def lock_current_project_access(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[IdentityActorFacts, Project, _ProjectAccess]:
    """Resolve authority under account -> project -> membership share locks.

    Locks are bounded and retained until the caller commits or rolls back.
    Project KEY SHARE fences explicit ownership/deletion FOR UPDATE locks while
    allowing ordinary counter deltas without a SHARE-to-UPDATE upgrade cycle.
    This is a local database admission boundary, not a portable authority token.
    """
    user, project = await lock_current_project_rows(
        db, actor_id=actor_id, project_id=project_id
    )
    with db.no_autoflush:
        access = await _resolve_current_project_access(
            db, user=user, project=project, lock_membership=True, nowait=True
        )
    return user, project, access


class SqlProjectWritePolicyResolver:
    """Bind write-policy resolution to the caller's transaction session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def resolve(self, request: ProjectWritePolicyRequest) -> _ProjectAccess:
        try:
            _, _, access = await lock_current_project_access(
                self._db,
                actor_id=request.actor_id,
                project_id=request.project_id,
            )
        except HTTPException as exc:
            raise ProjectWritePolicyFailure(
                status_code=exc.status_code,
                detail=exc.detail,
                headers=exc.headers,
            ) from exc
        return access


def bind_project_write_policy_resolver(
    db: AsyncSession,
) -> ProjectWritePolicyResolver:
    """Bind the session-free policy port to the caller-owned SQL transaction."""
    return SqlProjectWritePolicyResolver(db)


def restore_project_write_policy_failure(
    failure: ProjectWritePolicyFailure,
) -> NoReturn:
    """Restore a resolver failure as the existing HTTP response at its boundary."""
    raise HTTPException(
        status_code=failure.status_code,
        detail=failure.detail,
        headers=failure.headers,
    ) from failure


async def resolve_project_access_by_id(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    lock_membership: bool = False,
) -> tuple[Project, _ProjectAccess]:
    project = await db.get(Project, project_id, populate_existing=True)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    access = await resolve_project_access(
        db, actor_id=actor_id, project=project, lock_membership=lock_membership
    )
    return project, access


def assert_context_matches_project(
    access: _ProjectAccess, project_id: uuid.UUID
) -> None:
    """Reject a context/resource project mismatch before any read or write."""

    if access.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "access_context_mismatch",
                "detail": "访问上下文与资源项目不一致",
            },
        )


def assert_capability(access: _ProjectAccess, *capabilities: str) -> None:
    missing = sorted(cap for cap in capabilities if cap not in access.capabilities)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"缺少项目权限: {', '.join(missing)}",
        )
