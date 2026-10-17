"""Identity-owned data group catalog and account projection commands."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.group import Group
from app.db.models.user import User
from app.schemas.group import GroupCreate, GroupUpdate
from app.services.user_lifecycle import UserLifecycleService


class AccountGroupService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _manager(self, actor_id: uuid.UUID) -> User:
        actor = (
            await UserLifecycleService.lock_accounts(
                self.db, [actor_id], authority_only=True
            )
        ).get(actor_id)
        if (
            actor is None
            or not actor.is_active
            or actor.role
            not in {UserRole.SUPER_ADMIN.value, UserRole.PROJECT_ADMIN.value}
        ):
            raise HTTPException(status_code=403, detail="当前账号已无管理权限")
        return actor

    async def _group(self, group_id: uuid.UUID) -> Group:
        try:
            group = await self.db.scalar(
                select(Group)
                .where(Group.id == group_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}:
                raise HTTPException(
                    status_code=409, detail={"reason": "group_busy"}
                ) from exc
            raise
        if group is None:
            raise HTTPException(status_code=404, detail="数据组不存在")
        return group

    async def create(
        self, *, actor_id: uuid.UUID, payload: GroupCreate
    ) -> tuple[User, Group]:
        actor = await self._manager(actor_id)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        group = Group(name=name, description=payload.description or None)
        self.db.add(group)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409, detail=f"已存在同名数据组: {name}"
            ) from exc
        return actor, group

    async def update(
        self, group_id: uuid.UUID, *, actor_id: uuid.UUID, payload: GroupUpdate
    ) -> tuple[User, Group, dict[str, str]]:
        actor = await self._manager(actor_id)
        group = await self._group(group_id)
        changes: dict[str, str] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            if name != group.name:
                changes["name"] = f"{group.name}→{name}"
                group.name = name
                await self.db.execute(
                    update(User)
                    .where(User.group_id == group.id)
                    .values(group_name=name)
                )
        if payload.description is not None:
            group.description = payload.description or None
            changes["description"] = "updated"
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise HTTPException(
                status_code=409, detail=f"已存在同名数据组: {payload.name}"
            ) from exc
        return actor, group, changes

    async def delete(
        self, group_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> tuple[User, str]:
        actor = await self._manager(actor_id)
        group = await self._group(group_id)
        name = group.name
        await self.db.execute(
            update(User)
            .where(User.group_id == group.id)
            .values(group_id=None, group_name=None)
        )
        await self.db.delete(group)
        return actor, name
