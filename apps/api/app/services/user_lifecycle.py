"""Shared account locking for remaining Workspace and bootstrap coordinators."""

from __future__ import annotations

import uuid
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


class UserLifecycleService:
    """Serialize account authority with existing project and bootstrap writers."""

    @staticmethod
    async def lock_accounts(
        db: AsyncSession,
        user_ids: Iterable[uuid.UUID],
        *,
        nowait: bool = True,
        authority_only: bool = False,
    ) -> dict[uuid.UUID, User]:
        """Lock current accounts; authority-only KEY SHARE admits non-auth projections.

        Account role, active-state and password writers retain the default FOR UPDATE.
        """
        try:
            accounts = await db.scalars(
                select(User)
                .where(User.id.in_(sorted(set(user_ids))))
                .order_by(User.id)
                .with_for_update(
                    read=authority_only, key_share=authority_only, nowait=nowait
                )
                .execution_options(populate_existing=True)
            )
        except DBAPIError as exc:
            await db.rollback()
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise HTTPException(
                    status_code=409, detail="账号正在更新，请刷新后重试"
                ) from exc
            raise
        return {user.id: user for user in accounts}
