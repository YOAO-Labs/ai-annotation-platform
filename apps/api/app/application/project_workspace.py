"""Application workflows for project workspace ownership changes."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from aap_workspace_contract.access import ProjectAccess
from aap_workspace_contract.project_query_contract import ProjectOwnerTransferEvidence
from app.services.project_access import (
    lock_current_project_access,
)
from app.services.identity_contracts import IdentityActorFacts
from app.services.project_workspace import lock_project_row
from app.services.workspace_project_query import prepare_transfer_projection
from app.services.workspace_owner_transfer import (
    transfer_project_owner as execute_owner_transfer,
)


def _assert_project_manager(access: ProjectAccess) -> None:
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")


async def lock_workspace_project(db: AsyncSession, project_id: uuid.UUID) -> Project:
    """Refresh the project under its bounded exclusive configuration fence."""
    try:
        with db.no_autoflush:
            project = await lock_project_row(
                db,
                project_id,
                nowait=True,
                populate_existing=True,
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
    _, project, _ = await lock_managed_project_context(
        db,
        actor_id=actor_id,
        project_id=project_id,
        exclusive=exclusive,
    )
    return project


async def lock_managed_project_context(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    exclusive: bool = True,
) -> tuple[IdentityActorFacts, Project, ProjectAccess]:
    actor, project, access = await lock_current_project_access(
        db, actor_id=actor_id, project_id=project_id
    )
    _assert_project_manager(access)
    if exclusive:
        project = await lock_workspace_project(db, project_id)
    return actor, project, access


async def transfer_project_owner(
    db: AsyncSession,
    *,
    actor: IdentityActorFacts,
    project_id: uuid.UUID,
    new_owner_id: uuid.UUID,
) -> ProjectOwnerTransferEvidence:
    transfer = await execute_owner_transfer(
        db,
        actor_id=actor.id,
        project_id=project_id,
        new_owner_id=new_owner_id,
    )
    return await prepare_transfer_projection(db, actor=actor, transfer=transfer)
