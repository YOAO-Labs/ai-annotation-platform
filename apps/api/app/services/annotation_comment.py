from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm import make_transient_to_detached

from aap_labeling.discussion_contract import (
    DISCUSSION_PROTOCOL,
    AnnotationCommentRow,
    ClearAnnotationCommentAttachments,
    CreateAnnotationComment,
    DeleteCommentsForTasks,
    LockAnnotationComment,
    SoftDeleteAnnotationComment,
    UpdateAnnotationComment,
)

from app.db.enums import MANAGER_PLATFORM_ROLES, PlatformRole
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.services.identity_read_relations import account_profiles
from app.services.labeling_discussion_data import discussion_command
from app.services.labeling_discussion_relations import annotation_comment_facts
from app.services.workspace_read_relations import project_facts, project_member_facts
from app.services.labeling_core_read_relations import annotation_row


class AnnotationCommentService:
    """Own legacy annotation-comment validation and row mutations."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _attach(self, row: AnnotationCommentRow) -> AnnotationComment:
        comment = AnnotationComment(**row.model_dump(mode="python"))
        make_transient_to_detached(comment)
        return await self.db.merge(comment, load=False)

    async def refresh(self, comment: AnnotationComment) -> AnnotationComment:
        refreshed = await self.db.scalar(
            select(annotation_comment_facts)
            .where(annotation_comment_facts.id == comment.id)
            .execution_options(populate_existing=True)
        )
        if refreshed is None:
            raise InvalidRequestError(f"Could not refresh instance '{comment}'")
        return refreshed

    async def select_expired_attachment_comments(
        self, *, now: datetime | None = None
    ) -> list[AnnotationComment]:
        """Select inactive comments older than seven days, including empty attachments."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=7)
        result = await self.db.execute(
            select(annotation_comment_facts)
            .where(
                annotation_comment_facts.is_active.is_(False),
                annotation_comment_facts.updated_at < cutoff,
            )
            .limit(500)
        )
        return list(result.scalars().all())

    async def clear_attachments(self, comment_id: uuid.UUID) -> None:
        """Clear one comment's attachment row without committing the caller's transaction."""
        result = await discussion_command(
            self.db,
            ClearAnnotationCommentAttachments(
                schema=DISCUSSION_PROTOCOL,
                operation="clear_comment_attachments",
                comment_id=comment_id,
            ),
            flush_before=self.db.autoflush,
        )
        if result.comment is not None:
            await self._attach(result.comment)

    async def validate_mentions(
        self, project_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> None:
        """Require mentions to resolve to a project member or project manager."""

        if not user_ids:
            return
        member_facts = project_member_facts()
        membership = aliased(ProjectMember, member_facts, adapt_on_names=True)
        rows = (
            await self.db.execute(
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
            await self.db.execute(
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
            await self.db.execute(
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
        result = await discussion_command(
            self.db,
            CreateAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="create_comment",
                comment_id=uuid.uuid4(),
                annotation_id=annotation_id,
                project_id=project_id,
                author_id=author_id,
                body=body,
                mentions=mentions,
                attachments=attachments,
                canvas_drawing=canvas_drawing,
                anchor=anchor,
            ),
        )
        assert result.comment is not None
        return await self._attach(result.comment)

    async def get(self, comment_id: uuid.UUID) -> AnnotationComment | None:
        row = await self.db.scalar(
            select(annotation_comment_facts).where(
                annotation_comment_facts.id == comment_id
            )
        )
        return row

    async def lock(self, comment_id: uuid.UUID) -> AnnotationComment:
        result = await discussion_command(
            self.db,
            LockAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="lock_comment",
                comment_id=comment_id,
            ),
            flush_before=False,
        )
        assert result.comment is not None
        return await self._attach(result.comment)

    async def list_for_annotation(
        self, annotation_id: uuid.UUID, *, active_only: bool = True
    ) -> list[AnnotationComment]:
        query = select(annotation_comment_facts).where(
            annotation_comment_facts.annotation_id == annotation_id
        )
        if active_only:
            query = query.where(annotation_comment_facts.is_active.is_(True))
        return list((await self.db.scalars(query)).all())

    async def list_for_task(
        self,
        task_id: uuid.UUID,
        *,
        annotation_id: uuid.UUID | None = None,
        active_only: bool = True,
        cursor: tuple[datetime, uuid.UUID] | None = None,
        limit: int | None = None,
    ) -> list[AnnotationComment]:
        query = (
            select(annotation_comment_facts)
            .join(
                annotation_row,
                annotation_row.id == annotation_comment_facts.annotation_id,
            )
            .where(annotation_row.task_id == task_id)
        )
        if annotation_id is not None:
            query = query.where(annotation_comment_facts.annotation_id == annotation_id)
        if active_only:
            query = query.where(annotation_comment_facts.is_active.is_(True))
        if cursor is not None:
            timestamp, comment_id = cursor
            query = query.where(
                (annotation_comment_facts.created_at < timestamp)
                | (
                    (annotation_comment_facts.created_at == timestamp)
                    & (annotation_comment_facts.id < comment_id)
                )
            )
        query = query.order_by(
            annotation_comment_facts.created_at.desc(),
            annotation_comment_facts.id.desc(),
        )
        if limit is not None:
            query = query.limit(limit)
        return list((await self.db.scalars(query)).all())

    async def counts_for_active_annotations(
        self, *, task_id: uuid.UUID, project_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, int]]:
        from sqlalchemy import func

        return list(
            (
                await self.db.execute(
                    select(
                        annotation_comment_facts.annotation_id,
                        func.count(annotation_comment_facts.id),
                    )
                    .join(
                        annotation_row,
                        annotation_row.id == annotation_comment_facts.annotation_id,
                    )
                    .where(
                        annotation_row.task_id == task_id,
                        annotation_row.project_id == project_id,
                        annotation_row.is_active.is_(True),
                        annotation_row.was_cancelled.is_(False),
                        annotation_comment_facts.is_active.is_(True),
                    )
                    .group_by(annotation_comment_facts.annotation_id)
                )
            ).all()
        )

    async def delete_for_tasks(self, task_ids: list[uuid.UUID]) -> int:
        result = await discussion_command(
            self.db,
            DeleteCommentsForTasks(
                schema=DISCUSSION_PROTOCOL,
                operation="delete_comments_for_tasks",
                task_ids=task_ids,
            ),
            flush_before=self.db.autoflush,
        )
        return int(result.deleted_comments or 0)

    async def update(
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
        fields: dict[str, object] = {"comment_id": comment.id}
        if body is not None:
            fields["body"] = body
        if is_resolved is not None:
            fields["is_resolved"] = is_resolved
        result = await discussion_command(
            self.db,
            UpdateAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="update_comment",
                **fields,
            ),
            flush_before=False,
        )
        assert result.comment is not None
        return await self._attach(result.comment)

    async def soft_delete(self, comment: AnnotationComment) -> AnnotationComment:
        """Deactivate a row admitted and refreshed by the application workflow."""
        result = await discussion_command(
            self.db,
            SoftDeleteAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="soft_delete_comment",
                comment_id=comment.id,
            ),
            flush_before=False,
        )
        assert result.comment is not None
        return await self._attach(result.comment)
