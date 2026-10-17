from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import MANAGER_PLATFORM_ROLES, PlatformRole
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User


class AnnotationCommentService:
    """Own legacy annotation-comment validation and row mutations."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def validate_mentions(
        self, project_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> None:
        """Require mentions to resolve to a project member or project manager."""

        if not user_ids:
            return
        rows = (
            await self.db.execute(
                select(ProjectMember.user_id).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id.in_(user_ids),
                )
            )
        ).all()
        found = {row[0] for row in rows}
        manager_rows = (
            await self.db.execute(
                select(User.id)
                .join(Project, Project.owner_id == User.id)
                .where(
                    Project.id == project_id,
                    User.id.in_(user_ids),
                    User.role.in_(sorted(MANAGER_PLATFORM_ROLES)),
                )
            )
        ).all()
        super_rows = (
            await self.db.execute(
                select(User.id).where(
                    User.id.in_(user_ids),
                    User.role == PlatformRole.SUPER_ADMIN.value,
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

    async def create(
        self,
        *,
        annotation_id: uuid.UUID,
        project_id: uuid.UUID,
        author_id: uuid.UUID,
        body: str,
        mentions: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        canvas_drawing: dict[str, Any] | None,
        anchor: dict[str, Any] | None,
    ) -> AnnotationComment:
        comment = AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=annotation_id,
            project_id=project_id,
            author_id=author_id,
            body=body,
            mentions=mentions,
            attachments=attachments,
            canvas_drawing=canvas_drawing,
            anchor=anchor,
        )
        self.db.add(comment)
        await self.db.flush()
        return comment

    def update(
        self,
        comment: AnnotationComment,
        *,
        body: str | None,
        is_resolved: bool | None,
    ) -> AnnotationComment:
        """Mutate a row admitted and refreshed by the application workflow."""
        if body is not None:
            # Emptying the body is allowed only while attachments or a drawing remain.
            if not body.strip():
                has_drawing = bool(
                    comment.canvas_drawing
                    and (comment.canvas_drawing.get("shapes") or [])
                )
                if not comment.attachments and not has_drawing:
                    raise HTTPException(
                        status_code=422,
                        detail="评论需至少包含正文、附件或画布批注之一",
                    )
            comment.body = body
        if is_resolved is not None:
            comment.is_resolved = is_resolved
        return comment

    def soft_delete(self, comment: AnnotationComment) -> AnnotationComment:
        """Deactivate a row admitted and refreshed by the application workflow."""
        comment.is_active = False
        return comment
