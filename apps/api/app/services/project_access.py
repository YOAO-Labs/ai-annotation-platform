"""SQL-backed workspace access resolution and transaction locks."""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.workspace_contract import (
    ACCESS_KIND_MEMBER as _ACCESS_KIND_MEMBER,
    ACCESS_KIND_OWNER as _ACCESS_KIND_OWNER,
    ACCESS_KIND_SUPER_ADMIN as _ACCESS_KIND_SUPER_ADMIN,
    MANAGER_CAPABILITIES as _MANAGER_CAPABILITIES,
    ProjectAccess as _ProjectAccess,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
    ProjectWritePolicyResolver,
    membership_role_compatible as _membership_role_compatible,
    platform_role_is_manager as _platform_role_is_manager,
    project_role_capabilities as _project_role_capabilities,
)
from app.db.enums import (
    PLATFORM_ROLES,
    PROJECT_ROLES,
    PlatformRole,
    ProjectRole,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.task_creation_contract import (
    ProjectManagerFacts,
    decode_creation_reply,
)
from app.services.identity_contracts import IdentityActorFacts


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


def _manager_access(
    *,
    user: User,
    project: Project,
    access_kind: str,
    is_super_admin: bool,
    is_owner: bool,
) -> _ProjectAccess:
    return _ProjectAccess(
        user_id=user.id,
        project_id=project.id,
        platform_role=user.role,
        project_role=None,
        membership_id=None,
        membership_version=None,
        access_kind=access_kind,
        capabilities=_MANAGER_CAPABILITIES,
        is_super_admin=is_super_admin,
        is_owner=is_owner,
        is_manager=True,
    )


def _invalid_account() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="账号已停用或不存在",
        headers={"WWW-Authenticate": "Bearer"},
    )


def is_privileged_for_project(
    user: User | IdentityActorFacts, project: Project
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

    fresh_user = await db.get(User, actor_id, populate_existing=True)
    if fresh_user is None or not fresh_user.is_active:
        raise _invalid_account()
    fresh_project = await db.get(Project, project.id, populate_existing=True)
    if fresh_project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")
    user, project = fresh_user, fresh_project

    return await _resolve_current_project_access(
        db, user=user, project=project, lock_membership=lock_membership
    )


async def _resolve_current_project_access(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    lock_membership: bool,
    nowait: bool = False,
) -> _ProjectAccess:
    platform_role = user.role
    if platform_role not in PLATFORM_ROLES:
        # Unknown or legacy account roles fail closed; they are not a live
        # authorization source.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号角色无效，访问被拒绝"
        )

    if platform_role == PlatformRole.SUPER_ADMIN.value:
        return _manager_access(
            user=user,
            project=project,
            access_kind=_ACCESS_KIND_SUPER_ADMIN,
            is_super_admin=True,
            is_owner=project.owner_id == user.id,
        )

    if project.owner_id == user.id and _platform_role_is_manager(platform_role):
        return _manager_access(
            user=user,
            project=project,
            access_kind=_ACCESS_KIND_OWNER,
            is_super_admin=False,
            is_owner=True,
        )

    # Every other platform role, including an anomalous non-administrative
    # owner, needs a valid membership.  Ownership alone never grants visibility
    # or authority.
    # Always refresh the row: an identity-map entry cached before a role change
    # or membership removal must never remain authoritative.  Under FOR SHARE
    # the refreshed role/version is the locked, current value.
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
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="项目不存在")

    project_role = member.role
    if project_role not in PROJECT_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="项目职责无效，访问被拒绝"
        )
    if (
        platform_role == PlatformRole.VIEWER.value
        and project_role != ProjectRole.VIEWER.value
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号平台角色与项目职责不兼容"
        )

    return _ProjectAccess(
        user_id=user.id,
        project_id=project.id,
        platform_role=platform_role,
        project_role=project_role,
        membership_id=member.id,
        membership_version=member.version,
        access_kind=_ACCESS_KIND_MEMBER,
        capabilities=_project_role_capabilities(project_role),
        is_manager=False,
    )


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
) -> tuple[User, Project]:
    """Refresh and fence account/project rows without granting project visibility."""
    with db.no_autoflush:
        user = await _locked_access_row(
            db,
            select(User)
            .where(User.id == actor_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True),
            busy_reason="account_busy",
        )
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
) -> tuple[User, Project, _ProjectAccess]:
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
