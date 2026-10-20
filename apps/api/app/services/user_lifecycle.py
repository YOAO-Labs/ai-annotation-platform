"""Shared account locking for remaining Workspace and bootstrap coordinators."""

from __future__ import annotations

import uuid
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.identity_contracts import IdentityActorFacts


class UserLifecycleService:
    """Serialize account authority with existing project and bootstrap writers."""

    @staticmethod
    async def lock_accounts(
        db: AsyncSession,
        user_ids: Iterable[uuid.UUID],
        *,
        nowait: bool = True,
        authority_only: bool = False,
    ) -> dict[uuid.UUID, IdentityActorFacts]:
        """Lock current accounts; authority-only KEY SHARE admits non-auth projections.

        Account role, active-state and password writers retain the default FOR UPDATE.
        """
        try:
            return await IdentityAccountLockData(db).lock_accounts(
                user_ids,
                mode="key_share" if authority_only else "update",
                nowait=nowait,
            )
        except DBAPIError as exc:
            await db.rollback()
            if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
                raise HTTPException(
                    status_code=409, detail="账号正在更新，请刷新后重试"
                ) from exc
            raise
