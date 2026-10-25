"""全局 ⌘K 搜索：跨实体快速联想（项目 / 任务 / 数据集 / 成员）。

项目、任务、成员范围分别由 Workspace relational facts、Labeling assignment
scope 和 Identity accounts 提供；数据集对所有登录用户可见。
单次返回 limit×4 条，前端渲染 group + 键盘导航 + 跳转。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.db.enums import PlatformRole, ProjectRole
from app.db.models.dataset import Dataset
from app.services.workspace_project_query import execute_project_query
from app.services.workspace_project_query_data import (
    project_search_member_ids,
    project_search_scope,
)
from app.services.scheduler import task_visibility_clause
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_read_relations import (
    account_profiles,
    read_account_profiles,
)

from app.services.labeling_core_read_relations import batch_row, task_row

router = APIRouter()


@router.get("")
async def global_search(
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    pattern = f"%{q}%"
    project_items = await execute_project_query(
        db,
        operation="search_projects",
        actor=user,
        filters={"q": q, "limit": limit},
    )

    # Filter task authority before LIMIT so hidden matches cannot starve valid ones.
    project_scope = project_search_scope(user)
    tasks_q = (
        select(task_row, project_scope.c.project_name)
        .join(project_scope, task_row.project_id == project_scope.c.project_id)
        .where(
            or_(task_row.display_id.ilike(pattern), task_row.file_name.ilike(pattern))
        )
        .order_by(task_row.updated_at.desc())
        .limit(limit)
    )
    if user.role != PlatformRole.SUPER_ADMIN.value:
        roles = (
            [ProjectRole.VIEWER.value]
            if user.role == PlatformRole.VIEWER.value
            else [role.value for role in ProjectRole]
        )
        membership_scope = or_(
            *[
                and_(
                    project_scope.c.member_role == role,
                    or_(
                        task_visibility_clause(
                            user.id,
                            project_role=role,
                            task_model=task_row,
                            batch_model=batch_row,
                        ),
                        and_(
                            task_row.file_type == "video",
                            project_scope.c.video_collaboration["enabled"]
                            .as_boolean()
                            .is_(True),
                        ),
                    ),
                )
                for role in roles
            ]
        )
        tasks_q = tasks_q.outerjoin(batch_row, task_row.batch_id == batch_row.id).where(
            or_(
                project_scope.c.owner_id == user.id
                if user.role == PlatformRole.PROJECT_ADMIN.value
                else false(),
                membership_scope,
            )
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
            select(account_profiles)
            .where(
                or_(
                    account_profiles.c.name.ilike(pattern),
                    account_profiles.c.email.ilike(pattern),
                )
            )
            .where(account_profiles.c.is_active.is_(True))
            .order_by(account_profiles.c.name)
            .limit(limit)
        )
    else:
        # 仅返回当前用户共项目的成员
        member_scope = project_search_member_ids(user)
        users_q = (
            select(account_profiles)
            .join(member_scope, member_scope.c.user_id == account_profiles.c.id)
            .where(
                or_(
                    account_profiles.c.name.ilike(pattern),
                    account_profiles.c.email.ilike(pattern),
                )
            )
            .where(account_profiles.c.is_active.is_(True))
            .distinct()
            .limit(limit)
        )
    users = await read_account_profiles(db, users_q)
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
