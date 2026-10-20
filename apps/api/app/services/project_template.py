"""Database adapter for organization scope facts used by template policy."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aap_workspace.configuration_policy import (
    ConfigurationPolicyError,
    assert_template_visible as assert_policy_template_visible,
)
from app.db.enums import UserRole
from app.db.models.organization import OrganizationMember
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts


async def list_user_org_ids(
    db: AsyncSession, user: User | IdentityActorFacts
) -> list[uuid.UUID]:
    """Read the actor's non-deleted organization memberships."""
    rows = await db.execute(
        select(OrganizationMember.organization_id).where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.deleted_at.is_(None),
        )
    )
    return [row[0] for row in rows.all()]


def _actor_role(user: User | IdentityActorFacts) -> str:
    role = user.role
    return role.value if isinstance(role, UserRole) else role


async def assert_template_visible(
    db: AsyncSession, template: ProjectTemplate, user: User | IdentityActorFacts
) -> None:
    organization_ids: list[uuid.UUID] = []
    if (
        _actor_role(user) != UserRole.SUPER_ADMIN.value
        and template.scope == "organization"
        and template.organization_id is not None
    ):
        organization_ids = await list_user_org_ids(db, user)

    try:
        assert_policy_template_visible(
            {
                "scope": template.scope,
                "organization_id": template.organization_id,
                "created_by": template.created_by,
            },
            {"id": user.id, "role": _actor_role(user)},
            organization_ids,
        )
    except ConfigurationPolicyError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail=error.detail,
            headers=error.headers,
        ) from error
