"""v0.10.14 · E2 · 项目模板库端点.

挂载于 /project-templates/* 之下.
- GET    /project-templates              list (按 scope 可见性过滤)
- POST   /project-templates              create (private/organization 任意角色, public 仅 super_admin)
- GET    /project-templates/{id}         detail
- PATCH  /project-templates/{id}         update (created_by 或 super_admin)
- DELETE /project-templates/{id}         delete (created_by 或 super_admin)
- POST   /project-templates/{id}/duplicate  克隆 (任何可见模板都可)
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.deps import get_current_user, get_db
from aap_workspace.configuration_requests import (
    ProjectTemplateCreate,
    ProjectTemplateOut,
    ProjectTemplateUpdate,
)
from app.services.workspace_configuration import execute_configuration
from app.services.project_template import assert_template_visible, list_user_org_ids
from app.services.identity_contracts import IdentityActorFacts


router = APIRouter()


async def _serialize(db: AsyncSession, template: ProjectTemplate) -> dict:
    """挂上 created_by_name 方便前端展示."""
    creator_name: str | None = None
    if template.created_by:
        row = await db.execute(select(User.name).where(User.id == template.created_by))
        creator_name = row.scalar_one_or_none()
    data = {c.name: getattr(template, c.name) for c in template.__table__.columns}
    data["created_by_name"] = creator_name
    return data


@router.get("", response_model=list[ProjectTemplateOut])
async def list_templates(
    scope: Annotated[
        str | None, Query(pattern="^(private|organization|public)$")
    ] = None,
    type_key: Annotated[list[str] | None, Query()] = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    q = select(ProjectTemplate)

    # 可见性过滤
    if user.role == UserRole.SUPER_ADMIN:
        pass  # 全部可见
    else:
        org_ids = await list_user_org_ids(db, user)
        visibility_clauses = [
            ProjectTemplate.scope == "public",
            and_(
                ProjectTemplate.scope == "private",
                ProjectTemplate.created_by == user.id,
            ),
        ]
        if org_ids:
            visibility_clauses.append(
                and_(
                    ProjectTemplate.scope == "organization",
                    ProjectTemplate.organization_id.in_(org_ids),
                )
            )
        q = q.where(or_(*visibility_clauses))

    if scope:
        q = q.where(ProjectTemplate.scope == scope)
    if type_key:
        q = q.where(ProjectTemplate.type_key.in_(type_key))
    if search:
        q = q.where(ProjectTemplate.name.ilike(f"%{search}%"))

    rows = await db.execute(
        q.order_by(ProjectTemplate.created_at.desc()).execution_options(
            populate_existing=True
        )
    )
    templates = rows.scalars().all()
    return [await _serialize(db, t) for t in templates]


@router.post("", response_model=ProjectTemplateOut, status_code=201)
async def create_template(
    data: ProjectTemplateCreate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    result = await execute_configuration(
        db,
        operation="create_template",
        payload=data.model_dump(exclude_unset=True, exclude_none=False),
        actor_id=user.id,
    )
    await db.commit()
    template = await db.get(ProjectTemplate, result.resource_id, populate_existing=True)
    if template is None:
        raise RuntimeError("Created configuration template disappeared")
    return await _serialize(db, template)


async def _load_template_or_404(
    db: AsyncSession, template_id: uuid.UUID
) -> ProjectTemplate:
    template = await db.get(ProjectTemplate, template_id, populate_existing=True)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


@router.get("/{template_id}", response_model=ProjectTemplateOut)
async def get_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    template = await _load_template_or_404(db, template_id)
    await assert_template_visible(db, template, user)
    return await _serialize(db, template)


@router.patch("/{template_id}", response_model=ProjectTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    data: ProjectTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    result = await execute_configuration(
        db,
        operation="update_template",
        target_id=template_id,
        payload=data.model_dump(exclude_unset=True),
        actor_id=user.id,
    )
    await db.commit()
    template = await db.get(ProjectTemplate, result.resource_id, populate_existing=True)
    if template is None:
        raise RuntimeError("Updated configuration template disappeared")
    return await _serialize(db, template)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    await execute_configuration(
        db, operation="delete_template", target_id=template_id, actor_id=user.id
    )
    await db.commit()


@router.post(
    "/{template_id}/duplicate", response_model=ProjectTemplateOut, status_code=201
)
async def duplicate_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
):
    """克隆任意可见模板为当前用户私有模板."""
    result = await execute_configuration(
        db, operation="duplicate_template", target_id=template_id, actor_id=user.id
    )
    await db.commit()
    template = await db.get(ProjectTemplate, result.resource_id, populate_existing=True)
    if template is None:
        raise RuntimeError("Duplicated configuration template disappeared")
    return await _serialize(db, template)
