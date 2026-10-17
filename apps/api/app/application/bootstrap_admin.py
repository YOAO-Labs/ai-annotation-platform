"""Compose initial administrator identity and audit writes in one caller transaction."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.audit import AuditAction, AuditService
from app.services.identity_commands import IdentityCommandService


async def bootstrap_admin(
    db: AsyncSession, *, email: str, name: str, password: str
) -> bool:
    admin = await IdentityCommandService(db).create_bootstrap_super_admin(
        email=email,
        name=name,
        password=password,
        now=datetime.now(timezone.utc),
    )
    if admin is None:
        return False

    await AuditService.log(
        db,
        actor=admin,
        action=AuditAction.SYSTEM_BOOTSTRAP_ADMIN,
        target_type="user",
        target_id=admin.id,
        detail={"email": admin.email},
    )
    return True
