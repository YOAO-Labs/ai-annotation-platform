from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.group import Group
from app.db.models.user import User
from app.deps import get_db, require_roles
from app.schemas.group import GroupCreate, GroupOut, GroupUpdate
from app.schemas.management import GroupPage
from app.services.audit import AuditService
from app.services.management import fetch_group_page
from app.services.account_groups import AccountGroupService

router = APIRouter()

# Data groups are a platform-level catalog (organising accounts), not a project
# resource: authorization stays on the platform role.  An employee/viewer with
# no global administrative role never manages groups.
_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


async def _list_with_counts(db: AsyncSession) -> list[GroupOut]:
    rows = await db.execute(
        select(Group, func.count(User.id))
        .outerjoin(User, (User.group_id == Group.id) & (User.is_active.is_(True)))
        .group_by(Group.id)
        .order_by(Group.name.asc())
    )
    out: list[GroupOut] = []
    for grp, cnt in rows.all():
        out.append(
            GroupOut(
                id=grp.id,
                name=grp.name,
                description=grp.description,
                member_count=int(cnt or 0),
                created_at=grp.created_at,
            )
        )
    return out


@router.get("", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGERS)),
):
    return await _list_with_counts(db)


@router.get("/query", response_model=GroupPage)
async def query_groups(
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_MANAGERS)),
):
    if page < 1 or page_size < 1 or page_size > 500:
        raise HTTPException(status_code=422, detail="分页参数超出范围")
    items, total = await fetch_group_page(
        db, page=page, page_size=page_size, search=search
    )
    return GroupPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    actor, grp = await AccountGroupService(db).create(
        actor_id=actor.id, payload=payload
    )

    await AuditService.log(
        db,
        actor=actor,
        action="group.create",
        target_type="group",
        target_id=str(grp.id),
        request=request,
        status_code=201,
        detail={"name": grp.name},
    )
    await db.commit()
    await db.refresh(grp)
    return GroupOut(
        id=grp.id,
        name=grp.name,
        description=grp.description,
        member_count=0,
        created_at=grp.created_at,
    )


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: uuid.UUID,
    payload: GroupUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    actor, grp, changes = await AccountGroupService(db).update(
        group_id, actor_id=actor.id, payload=payload
    )

    if changes:
        await AuditService.log(
            db,
            actor=actor,
            action="group.update",
            target_type="group",
            target_id=str(grp.id),
            request=request,
            status_code=200,
            detail={"name": grp.name, **changes},
        )
    await db.commit()
    await db.refresh(grp)

    cnt = (
        await db.execute(
            select(func.count(User.id)).where(
                User.group_id == grp.id, User.is_active.is_(True)
            )
        )
    ).scalar() or 0
    return GroupOut(
        id=grp.id,
        name=grp.name,
        description=grp.description,
        member_count=int(cnt),
        created_at=grp.created_at,
    )


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    actor, name = await AccountGroupService(db).delete(group_id, actor_id=actor.id)

    await AuditService.log(
        db,
        actor=actor,
        action="group.delete",
        target_type="group",
        target_id=str(group_id),
        request=request,
        status_code=204,
        detail={"name": name},
    )
    await db.commit()
