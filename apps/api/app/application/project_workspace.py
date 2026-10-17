"""Application workflows for project workspace ownership changes."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project import Project
from app.services.project_workspace import transfer_project_owner as write_project_owner
from app.services.user_lifecycle import UserLifecycleService


async def transfer_project_owner(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    new_owner_id: uuid.UUID,
) -> Project:
    accounts = await UserLifecycleService.lock_accounts(db, [actor_id, new_owner_id])
    actor = accounts.get(actor_id)
    if actor is None or not actor.is_active or actor.role != UserRole.SUPER_ADMIN.value:
        raise HTTPException(status_code=403, detail="当前账号已无管理权限")
    target = accounts.get(new_owner_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="目标用户不存在")

    try:
        project = await db.scalar(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from exc
        raise
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if target.role != UserRole.PROJECT_ADMIN:
        raise HTTPException(status_code=400, detail="仅可转移给 project_admin")

    write_project_owner(project=project, new_owner_id=target.id)
    return project
