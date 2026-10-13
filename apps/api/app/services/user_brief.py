"""v0.7.2 · 责任人可视化辅助：批量将 user_id 列表解析为 UserBrief。

用于 list_tasks / list_batches 等 hot path：避免前端为了渲染头像组而单独
请求 /users 或 /projects/{id}/members。一次 IN 查询补全名 / 邮箱 / 角色。
"""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.project_member import ProjectMember
from app.schemas.user import UserBrief


def _initial(name: str | None, email: str | None) -> str:
    src = (name or email or "?").strip()
    return src[:1].upper() if src else "?"


def _to_brief(u: User, role: str | None = None) -> UserBrief:
    return UserBrief(
        id=u.id,
        name=u.name,
        email=u.email,
        role=role or u.role,
        avatar_initial=_initial(u.name, u.email),
        avatar_ref=u.avatar_ref,
    )


async def resolve_briefs(
    db: AsyncSession,
    user_ids: Iterable[uuid.UUID | str],
) -> dict[str, UserBrief]:
    """把 user_id 列表批量解析为 {str(user_id): UserBrief}。"""
    ids = [uuid.UUID(str(x)) for x in user_ids if x is not None]
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {str(u.id): _to_brief(u) for u in rows}


async def resolve_briefs_with_project_role(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_ids: Iterable[uuid.UUID | str],
) -> dict[str, UserBrief]:
    """同 resolve_briefs，但用 project_members.role 覆盖 user.role
    （用户在不同项目可担任不同角色）。"""
    ids = [uuid.UUID(str(x)) for x in user_ids if x is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(User, ProjectMember.role)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .where(ProjectMember.project_id == project_id, User.id.in_(ids))
        )
    ).all()
    by_id: dict[str, UserBrief] = {str(u.id): _to_brief(u, role) for u, role in rows}

    # 兜底：批次成员可能不在 project_members（owner / 历史数据），仍要返回 brief
    missing_ids = [i for i in ids if str(i) not in by_id]
    if missing_ids:
        extra = (
            (await db.execute(select(User).where(User.id.in_(missing_ids))))
            .scalars()
            .all()
        )
        for u in extra:
            by_id[str(u.id)] = _to_brief(u)
    return by_id
