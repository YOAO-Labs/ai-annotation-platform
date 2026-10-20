"""Run the Identity-owned bootstrap command in the caller's transaction."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.request_id import request_id_var
from app.services.identity_commands import IdentityCommandService


async def bootstrap_admin(
    db: AsyncSession, *, email: str, name: str, password: str
) -> bool:
    account_id = await IdentityCommandService(db).create_bootstrap_super_admin(
        email=email,
        name=name,
        password=password,
        now=datetime.now(timezone.utc),
        request_id=request_id_var.get() or None,
    )
    return account_id is not None
