"""Application workflows for project workspace ownership changes."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.user import User
from aap_workspace.access_contract import ProjectAccess
from app.services.project_access import (
    lock_current_project_access,
    resolve_project_access_by_id,
)
from app.services.workspace_owner_transfer import (
    transfer_project_owner as execute_owner_transfer,
)


def _assert_project_manager(access: ProjectAccess) -> None:
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")


async def read_managed_project(
    db: AsyncSession, *, actor_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    actor = await db.get(User, actor_id)
    if actor is None:
        raise HTTPException(
            status_code=401,
            detail="账号已停用或不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    project, access = await resolve_project_access_by_id(
        db, actor_id=actor.id, project_id=project_id
    )
    _assert_project_manager(access)
    return project


async def lock_workspace_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    """Refresh the project under its bounded exclusive configuration fence."""
    try:
        with db.no_autoflush:
            project = await db.scalar(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if state in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from exc
        raise
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


async def lock_managed_project(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    exclusive: bool = True,
) -> Project:
    _, project, access = await lock_current_project_access(
        db, actor_id=actor_id, project_id=project_id
    )
    _assert_project_manager(access)
    if exclusive:
        project = await lock_workspace_project(db, project_id)
    return project


async def transfer_project_owner(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    new_owner_id: uuid.UUID,
) -> Project:
    await execute_owner_transfer(
        db, actor_id=actor_id, project_id=project_id, new_owner_id=new_owner_id
    )
    with db.no_autoflush:
        project = await db.scalar(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project
