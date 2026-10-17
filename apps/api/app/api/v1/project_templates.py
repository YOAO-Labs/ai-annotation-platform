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

import copy
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.deps import assert_project_visible, get_current_user, get_db
from app.schemas.project_template import (
    ProjectTemplateCreate,
    ProjectTemplateOut,
    ProjectTemplateUpdate,
)
from app.services.display_id import next_display_id
from app.services.project import (
    coalesce_legacy_into_tool_bindings,
    resolve_project_kind,
)
from app.services.project_template import (
    TEMPLATE_PAYLOAD_FIELDS,
    assert_can_create_scope,
    assert_template_visible,
    can_edit_template,
    dump_project_to_template_payload,
    list_user_org_ids,
)


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
    user: User = Depends(get_current_user),
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

    rows = await db.execute(q.order_by(ProjectTemplate.created_at.desc()))
    templates = rows.scalars().all()
    return [await _serialize(db, t) for t in templates]


@router.post("", response_model=ProjectTemplateOut, status_code=201)
async def create_template(
    data: ProjectTemplateCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 角色门槛: super_admin / project_admin 可建; 其它角色拒
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可创建模板"
        )

    assert_can_create_scope(data.scope, user)
    if data.scope == "organization" and data.organization_id is None:
        raise HTTPException(
            status_code=400, detail="organization scope 必须指定 organization_id"
        )

    payload = data.model_dump(exclude_unset=True, exclude_none=False)
    source_project_id = payload.pop("source_project_id", None)
    source_project = None
    if source_project_id is not None:
        source_project = await assert_project_visible(source_project_id, db, user)

    payload["type_key"], payload["data_type"] = resolve_project_kind(
        payload.get("type_key"),
        payload.get("data_type"),
        inherited_type_key=(
            source_project.type_key if source_project is not None else None
        ),
        inherited_data_type=(
            source_project.data_type if source_project is not None else None
        ),
    )
    type_key = payload["type_key"]

    # Coalesce caller legacy fields before source defaults so they remain authoritative.
    coalesce_legacy_into_tool_bindings(payload, None, type_key)
    for _legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(_legacy_key, None)

    if source_project is not None:
        merged = dump_project_to_template_payload(source_project)
        for k, v in merged.items():
            payload.setdefault(k, v)
        payload.setdefault("name", f"{source_project.name} 模板")
        payload.setdefault("type_label", source_project.type_label)

    if (
        not payload.get("name")
        or not payload.get("type_label")
        or not payload.get("type_key")
    ):
        raise HTTPException(
            status_code=400,
            detail="name / type_label / type_key 必填 (或通过 source_project_id 兜底)",
        )

    template = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "project_templates"),
        created_by=user.id,
        source_project_id=source_project_id,
        **payload,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return await _serialize(db, template)


async def _load_template_or_404(
    db: AsyncSession, template_id: uuid.UUID
) -> ProjectTemplate:
    template = await db.get(ProjectTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return template


@router.get("/{template_id}", response_model=ProjectTemplateOut)
async def get_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    template = await _load_template_or_404(db, template_id)
    await assert_template_visible(db, template, user)
    return await _serialize(db, template)


@router.patch("/{template_id}", response_model=ProjectTemplateOut)
async def update_template(
    template_id: uuid.UUID,
    data: ProjectTemplateUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    template = await _load_template_or_404(db, template_id)
    await assert_template_visible(db, template, user)
    if not can_edit_template(template, user):
        raise HTTPException(status_code=403, detail="仅模板创建者或超级管理员可编辑")

    payload = data.model_dump(exclude_unset=True)
    new_scope = payload.get("scope", template.scope)
    if new_scope != template.scope:
        # scope 升级到 public 需 super_admin
        assert_can_create_scope(new_scope, user)
        if new_scope == "organization" and not payload.get(
            "organization_id", template.organization_id
        ):
            raise HTTPException(
                status_code=400,
                detail="organization scope 必须指定 organization_id",
            )

    # v0.10.22 · 同 create 路径: 兼容旧客户端 legacy 字段反向派生进 tool_bindings 后剔除.
    coalesce_legacy_into_tool_bindings(
        payload, template.tool_bindings, payload.get("type_key") or template.type_key
    )
    for _legacy_key in ("classes", "classes_config", "attribute_schema"):
        payload.pop(_legacy_key, None)

    for k, v in payload.items():
        setattr(template, k, v)
    await db.commit()
    await db.refresh(template)
    return await _serialize(db, template)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    template = await _load_template_or_404(db, template_id)
    await assert_template_visible(db, template, user)
    if not can_edit_template(template, user):
        raise HTTPException(status_code=403, detail="仅模板创建者或超级管理员可删除")
    await db.delete(template)
    await db.commit()


@router.post(
    "/{template_id}/duplicate", response_model=ProjectTemplateOut, status_code=201
)
async def duplicate_template(
    template_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """克隆任意可见模板为当前用户私有模板."""
    if user.role not in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN):
        raise HTTPException(
            status_code=403, detail="仅项目管理员或超级管理员可克隆模板"
        )

    src = await _load_template_or_404(db, template_id)
    await assert_template_visible(db, src, user)

    payload = {field: getattr(src, field) for field in TEMPLATE_PAYLOAD_FIELDS}
    # deep copy JSONB 字段
    for k, v in list(payload.items()):
        if isinstance(v, (dict, list)):
            payload[k] = copy.deepcopy(v)

    new = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=await next_display_id(db, "project_templates"),
        name=f"{src.name} (副本)",
        description=src.description,
        annotation_guide=src.annotation_guide,
        scope="private",
        organization_id=None,
        created_by=user.id,
        source_project_id=src.source_project_id,
        **payload,
    )
    db.add(new)
    await db.commit()
    await db.refresh(new)
    return await _serialize(db, new)
