"""Validation shared by annotation and task comments."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.enums import MANAGER_PLATFORM_ROLES, PlatformRole
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.identity_read_relations import account_profiles
from app.services.workspace_read_relations import project_facts, project_member_facts


async def validate_mentions(
    db: AsyncSession, project_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> None:
    """Require mentions to resolve to a project member or project manager."""

    if not user_ids:
        return
    member_facts = project_member_facts()
    membership = aliased(ProjectMember, member_facts, adapt_on_names=True)
    rows = (
        await db.execute(
            select(membership.user_id)
            .select_from(membership)
            .where(
                membership.project_id == project_id,
                membership.user_id.in_(user_ids),
            )
        )
    ).all()
    found = {row[0] for row in rows}
    project = aliased(Project, project_facts(), adapt_on_names=True)
    manager_rows = (
        await db.execute(
            select(account_profiles.c.id)
            .select_from(account_profiles)
            .join(project, project.owner_id == account_profiles.c.id)
            .where(
                project.id == project_id,
                account_profiles.c.id.in_(user_ids),
                account_profiles.c.role.in_(sorted(MANAGER_PLATFORM_ROLES)),
            )
        )
    ).all()
    super_rows = (
        await db.execute(
            select(account_profiles.c.id).where(
                account_profiles.c.id.in_(user_ids),
                account_profiles.c.role == PlatformRole.SUPER_ADMIN.value,
            )
        )
    ).all()
    found |= {row[0] for row in manager_rows}
    found |= {row[0] for row in super_rows}
    missing = [str(user_id) for user_id in user_ids if user_id not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "mentions_invalid", "non_member_user_ids": missing},
        )


def validate_comment_body(comment: AnnotationComment, body: str | None) -> None:
    if body is not None:
        if not body.strip():
            has_drawing = bool(
                comment.canvas_drawing and (comment.canvas_drawing.get("shapes") or [])
            )
            if not comment.attachments and not has_drawing:
                raise HTTPException(
                    status_code=422,
                    detail="评论需至少包含正文、附件或画布批注之一",
                )
