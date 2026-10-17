"""Project-template visibility, scope, and payload helpers."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.organization import OrganizationMember
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.project_clone import CLONEABLE_PROJECT_FIELDS


# 与模板 model 列对齐的模板载荷字段名 (在 ProjectTemplate 与 Project 之间复制 / 应用).
# 与 CLONEABLE_PROJECT_FIELDS 完全一致, 不重新定义.
TEMPLATE_PAYLOAD_FIELDS: tuple[str, ...] = CLONEABLE_PROJECT_FIELDS


async def list_user_org_ids(db: AsyncSession, user: User) -> list[uuid.UUID]:
    """v0.10.14 · 取出 user 当前所属的全部 organization id (软删未过滤; 与
    OrganizationMember 当前没有 active 字段, 用 deleted_at IS NULL 兼容).
    """
    rows = await db.execute(
        select(OrganizationMember.organization_id).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.deleted_at.is_(None),
        )
    )
    return [r[0] for r in rows.all()]


async def assert_template_visible(
    db: AsyncSession, template: ProjectTemplate, user: User
) -> None:
    """看不到则 404 隐藏存在性 (与 require_project_visible 风格一致)."""
    from fastapi import HTTPException

    if user.role == UserRole.SUPER_ADMIN:
        return
    if template.scope == "public":
        return
    if template.scope == "private":
        if template.created_by == user.id:
            return
    elif template.scope == "organization":
        if template.organization_id is None:
            return  # 数据不一致, 但放过 (CHECK 已防御); 等价于 public 内可见
        org_ids = await list_user_org_ids(db, user)
        if template.organization_id in org_ids:
            return
    raise HTTPException(status_code=404, detail="模板不存在")


def can_edit_template(template: ProjectTemplate, user: User) -> bool:
    if user.role == UserRole.SUPER_ADMIN:
        return True
    return template.created_by == user.id


def assert_can_create_scope(scope: str, user: User) -> None:
    """v0.10.14 · scope=public 必须 super_admin; 其它角色可建 private / organization.

    organization scope 要求 organization_id 由 caller 显式给出; 不在这里校验 user
    是否属于该 org (留给后续策略, KISS).
    """
    from fastapi import HTTPException

    if scope == "public" and user.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="仅超级管理员可创建公共模板")


def assert_template_creation_scope(
    scope: str, organization_id: uuid.UUID | None, actor: User
) -> None:
    assert_can_create_scope(scope, actor)
    if scope == "organization" and organization_id is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400, detail="organization scope 必须指定 organization_id"
        )


def dump_project_to_template_payload(project: Any) -> dict[str, Any]:
    """从源 Project 抽出模板载荷字段, JSONB / list 深拷贝避免共享底层引用."""
    payload: dict[str, Any] = {}
    for field in TEMPLATE_PAYLOAD_FIELDS:
        if not hasattr(project, field):
            continue
        value = getattr(project, field)
        if isinstance(value, (dict, list)):
            value = copy.deepcopy(value)
        payload[field] = value
    # E1 整合: annotation_guide 同步进模板; guide_assets 不存
    if getattr(project, "annotation_guide", None) is not None:
        payload["annotation_guide"] = project.annotation_guide
    return payload


def merge_template_into_payload(
    template: ProjectTemplate, payload: dict[str, Any]
) -> dict[str, Any]:
    """v0.10.14 · 把模板载荷字段 deepcopy 进项目创建 payload, caller 显式给出的优先.

    包括 annotation_guide (模板携带的 markdown 文本). guide_assets 模板不存, 新项目
    需自行上传.
    """
    for field in TEMPLATE_PAYLOAD_FIELDS:
        if field in payload:
            continue
        if not hasattr(template, field):
            continue
        value = getattr(template, field)
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = copy.deepcopy(value)
        payload[field] = value

    if "annotation_guide" not in payload and template.annotation_guide is not None:
        payload["annotation_guide"] = template.annotation_guide
    return payload
