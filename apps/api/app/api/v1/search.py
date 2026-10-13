"""v0.7.2 · 全局 ⌘K 搜索：跨实体快速联想（项目 / 任务 / 数据集 / 成员）。

每类调用现有 service.search 或最小 SQL，统一遵守该用户的可见性边界
（项目按 _visible_project_filter；任务跟项目可见性绑定；数据集是公开资源）。
单次返回 limit×4 条，前端渲染 group + 键盘导航 + 跳转。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.db.enums import PlatformRole
from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.api.v1.projects import _visible_project_filter

router = APIRouter()


@router.get("")
async def global_search(
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    pattern = f"%{q}%"

    # 项目（按 _visible_project_filter）
    proj_q = select(Project).where(Project.name.ilike(pattern))
    cond = _visible_project_filter(user)
    if cond is not None:
        proj_q = proj_q.where(cond)
    projects = (
        (await db.execute(proj_q.order_by(Project.updated_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    project_items = [
        {
            "id": str(p.id),
            "display_id": p.display_id,
            "name": p.name,
            "type_key": p.type_key,
            "data_type": p.data_type,
            "type_label": p.type_label,
        }
        for p in projects
    ]

    # 任务（按 display_id 前缀 / file_name 模糊；可见项目内）
    visible_proj_ids_q = select(Project.id)
    if cond is not None:
        visible_proj_ids_q = visible_proj_ids_q.where(cond)
    tasks_q = (
        select(Task, Project.name)
        .join(Project, Task.project_id == Project.id)
        .where(Task.project_id.in_(visible_proj_ids_q))
        .where(or_(Task.display_id.ilike(pattern), Task.file_name.ilike(pattern)))
        .order_by(Task.updated_at.desc())
        .limit(limit)
    )
    task_rows = (await db.execute(tasks_q)).all()
    task_items = [
        {
            "id": str(t.id),
            "display_id": t.display_id,
            "file_name": t.file_name,
            "project_id": str(t.project_id),
            "project_name": pname,
        }
        for t, pname in task_rows
    ]

    # 数据集（数据集对所有登录用户可见；service 层已无角色过滤）
    ds_q = (
        select(Dataset)
        .where(Dataset.name.ilike(pattern))
        .order_by(Dataset.updated_at.desc())
        .limit(limit)
    )
    datasets = (await db.execute(ds_q)).scalars().all()
    dataset_items = [
        {
            "id": str(d.id),
            "name": d.name,
            "data_type": d.data_type,
        }
        for d in datasets
    ]

    # 成员（仅可见项目里的成员；平台角色只决定是否全局，项目内成员身份由
    # project_members 决定）
    if user.role == PlatformRole.SUPER_ADMIN.value:
        users_q = (
            select(User)
            .where(or_(User.name.ilike(pattern), User.email.ilike(pattern)))
            .where(User.is_active.is_(True))
            .order_by(User.name)
            .limit(limit)
        )
    else:
        # 仅返回当前用户共项目的成员
        users_q = (
            select(User)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id.in_(visible_proj_ids_q))
            .where(or_(User.name.ilike(pattern), User.email.ilike(pattern)))
            .where(User.is_active.is_(True))
            .distinct()
            .limit(limit)
        )
    users = (await db.execute(users_q)).scalars().all()
    member_items = [
        {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "role": u.role,
        }
        for u in users
    ]

    return {
        "projects": project_items,
        "tasks": task_items,
        "datasets": dataset_items,
        "members": member_items,
    }
