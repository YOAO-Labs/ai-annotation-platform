"""SQL bindings to Identity's fixed public relations, never its private tables."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, String, column, select, table
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from aap_identity.read_relation_contract import AccountProfile

account_profiles = table(
    "account_profiles",
    column("id", UUID(as_uuid=True)),
    column("name", String),
    column("email", String),
    column("role", String),
    column("is_active", Boolean),
    column("status", String),
    column("avatar_ref", String),
    schema="aap_identity_v1",
)

invitation_audit_targets = table(
    "invitation_audit_targets",
    column("id", UUID(as_uuid=True)),
    column("project_id", UUID(as_uuid=True)),
    schema="aap_identity_v1",
)


async def read_account_profiles(
    db: AsyncSession, statement: Select
) -> list[AccountProfile]:
    # Core SELECT does not trigger ORM autoflush. Keep the replaced ORM read's
    # pending-work visibility without overriding a caller no_autoflush scope.
    if db.autoflush:
        await db.flush()
    result = await db.execute(statement)
    return [AccountProfile.model_validate(dict(row)) for row in result.mappings().all()]


async def read_account_profile(
    db: AsyncSession, account_id: uuid.UUID
) -> AccountProfile | None:
    if db.autoflush:
        await db.flush()
    result = await db.execute(
        select(account_profiles).where(account_profiles.c.id == account_id)
    )
    row = result.mappings().one_or_none()
    return AccountProfile.model_validate(dict(row)) if row is not None else None
