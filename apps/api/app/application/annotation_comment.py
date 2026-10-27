from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import (
    lock_core_nowait,
    lock_current_task_target,
)
from app.db.enums import ProjectRole
from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.task import Task
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.schemas.annotation_comment import (
    AnnotationCommentCreate,
    AnnotationCommentUpdate,
)
from app.services.annotation_comment import AnnotationCommentService
from app.services import task_access
from app.services.discussion_actions import discussion_actions
from app.services.audit import AuditAction, AuditService
from app.services.audit_origin_contract import AuditOriginContext
from app.services.discussion_notifications import (
    prepare_annotation_comment_mention_notifications,
)
from app.services.feedback import FeedbackService
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.notification import NotificationService
from aap_workspace_contract.access import ProjectAccess
from app.services.task_discussion import require_visible_annotation
from app.services.labeling_core_read_relations import lock_annotation_rows_for_core_read


class AnnotationCommentWorkflow:
    """Compose legacy annotation-comment writes with their atomic side effects."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.comments = AnnotationCommentService(db)

    async def create(
        self,
        annotation_id: uuid.UUID,
        actor_id: uuid.UUID,
        data: AnnotationCommentCreate,
        *,
        origin: AuditOriginContext,
        attachment_authority=None,
        attachment_rows=(),
        attachment_payload: list[dict] | None = None,
    ) -> tuple[AnnotationComment, IdentityActorFacts]:
        actor = await IdentityAccountLockData(self.db).read_actor_status(actor_id)
        if actor is None:
            raise HTTPException(
                status_code=401,
                detail="账号已停用或不存在",
                headers={"WWW-Authenticate": "Bearer"},
            )
        annotation, task = await require_visible_annotation(
            self.db,
            annotation_id,
            actor_id,
            require_active=True,
        )
        expected_access = await task_access.resolve_task_access(self.db, task, actor.id)
        await self.comments.validate_mentions(
            task.project_id, [mention.user_id for mention in data.mentions]
        )
        annotation, task, actor, _ = await self._lock_annotation_target(
            annotation,
            task,
            actor,
            expected_access=expected_access,
            require_active=True,
        )

        comment = await self.comments.create(
            annotation_id=annotation.id,
            project_id=task.project_id,
            author_id=actor.id,
            body=data.body,
            mentions=[
                mention.model_dump(by_alias=True, mode="json")
                for mention in data.mentions
            ],
            attachments=(
                attachment_payload
                if attachment_payload is not None
                else [
                    attachment.model_dump(by_alias=True, mode="json")
                    for attachment in data.attachments
                ]
            ),
            canvas_drawing=(
                data.canvas_drawing.model_dump(by_alias=True, mode="json")
                if data.canvas_drawing
                else None
            ),
            anchor=(
                data.anchor.model_dump(by_alias=True, mode="json")
                if data.anchor
                else None
            ),
        )
        if task.project_id is not None:
            await FeedbackService(self.db).mirror_annotation_comment(
                comment, task_id=task.id
            )
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_COMMENT_ADD,
            target_type="annotation",
            target_id=str(annotation_id),
            origin=origin,
            status_code=201,
            detail={
                "project_id": str(task.project_id),
                "comment_id": str(comment.id),
                "preview": data.body[:120],
                "mention_count": len(data.mentions),
                "attachment_count": len(
                    attachment_payload
                    if attachment_payload is not None
                    else data.attachments
                ),
                "has_canvas_drawing": data.canvas_drawing is not None,
                "has_anchor": data.anchor is not None,
            },
        )
        pending_notifications = await prepare_annotation_comment_mention_notifications(
            self.db,
            comment=comment,
            task=task,
            actor=actor,
            mentioned_user_ids=[mention.user_id for mention in data.mentions],
        )
        attachment_expiries: tuple = ()
        if attachment_authority is not None:
            from app.services import comment_attachment_uploads as attachment_uploads

            rows = list(attachment_rows)
            await attachment_uploads.bind(self.db, rows, comment.id)
            await attachment_uploads.reauthorize(self.db, attachment_authority)
            attachment_uploads.validate_bound(rows, comment.id)
            attachment_expiries = tuple(row.expires_at for row in rows)
        await self.db.commit()
        if attachment_authority is not None:
            attachment_uploads.check_deadlines(
                attachment_authority, attachment_expiries
            )
        await NotificationService(self.db).publish_committed(pending_notifications)
        comment = await self.comments.refresh(comment)
        if attachment_authority is not None:
            attachment_uploads.check_deadlines(
                attachment_authority, attachment_expiries
            )
        return comment, actor

    async def update(
        self,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        data: AnnotationCommentUpdate,
    ) -> AnnotationComment:
        comment, annotation, task, actor, expected_access = await self._read_comment(
            comment_id, actor_id, operation="edit"
        )
        comment, actor = await self._lock_comment(
            comment,
            annotation,
            task,
            actor,
            expected_access=expected_access,
            operation="edit",
        )
        comment = await self.comments.update(
            comment, body=data.body, is_resolved=data.is_resolved
        )
        await self.db.commit()
        comment = await self.comments.refresh(comment)
        return comment

    async def delete(
        self,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        origin: AuditOriginContext,
    ) -> None:
        comment, annotation, task, actor, expected_access = await self._read_comment(
            comment_id, actor_id, operation="delete"
        )
        comment, actor = await self._lock_comment(
            comment,
            annotation,
            task,
            actor,
            expected_access=expected_access,
            operation="delete",
        )
        await self.comments.soft_delete(comment)
        from app.services import comment_attachment_uploads as attachment_uploads

        await attachment_uploads.retire_for_comments(self.db, [comment_id])
        await AuditService.log(
            self.db,
            actor=actor,
            action=AuditAction.ANNOTATION_COMMENT_DELETE,
            target_type="annotation",
            target_id=str(comment.annotation_id),
            origin=origin,
            status_code=204,
            detail={
                "comment_id": str(comment_id),
                "preview": (comment.body or "")[:120],
            },
        )
        await self.db.commit()

    def _assert_action(
        self,
        comment: AnnotationComment,
        actor: IdentityActorFacts | IdentityActorStatus,
        access: ProjectAccess,
        *,
        operation: Literal["edit", "delete"],
    ) -> None:
        actions = discussion_actions(
            "annotation_comment",
            "comment",
            is_author=comment.author_id == actor.id,
            is_admin=access.is_manager,
            is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
            is_accessible=True,
            can_reply=False,
        )
        allowed = actions.edit if operation == "edit" else actions.delete
        if not allowed:
            raise HTTPException(
                status_code=403, detail=f"Only the author can {operation} this comment"
            )

    async def _read_comment(
        self,
        comment_id: uuid.UUID,
        actor_id: uuid.UUID,
        *,
        operation: Literal["edit", "delete"],
    ) -> tuple[AnnotationComment, Annotation, Task, IdentityActorStatus, ProjectAccess]:
        comment = await self.comments.get(comment_id)
        if comment is None or not comment.is_active:
            raise HTTPException(status_code=404, detail="Comment not found")
        actor = await IdentityAccountLockData(self.db).read_actor_status(actor_id)
        if actor is None:
            raise HTTPException(
                status_code=401,
                detail="账号已停用或不存在",
                headers={"WWW-Authenticate": "Bearer"},
            )
        annotation, task = await require_visible_annotation(
            self.db, comment.annotation_id, actor_id
        )
        access = await task_access.resolve_task_access(self.db, task, actor.id)
        self._assert_action(comment, actor, access, operation=operation)
        return comment, annotation, task, actor, access

    async def _lock_annotation_target(
        self,
        annotation: Annotation,
        task: Task,
        actor: IdentityActorFacts | IdentityActorStatus,
        *,
        expected_access: ProjectAccess,
        require_active: bool,
    ) -> tuple[Annotation, Task, IdentityActorFacts, ProjectAccess]:
        annotation_id, task_id = annotation.id, task.id
        try:
            task, actor, _, access = await lock_current_task_target(
                self.db,
                task_id=task_id,
                actor_id=actor.id,
                expected_access=expected_access,
            )
            await task_access.assert_task_visible(
                self.db, task, actor.id, access=access
            )
            annotations = await lock_core_nowait(
                self.db,
                lambda: lock_annotation_rows_for_core_read(
                    self.db,
                    [annotation_id],
                    lock_mode="update",
                    nowait=True,
                    populate_existing=True,
                ),
                reason="annotation_busy",
            )
            annotation = annotations[0] if annotations else None
        except TaskWorkflowError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if annotation is None:
            raise HTTPException(status_code=404, detail="Annotation not found")
        if annotation.task_id != task_id:
            raise HTTPException(
                status_code=409, detail={"reason": "annotation_target_changed"}
            )
        if require_active and (
            not annotation.is_active or bool(annotation.was_cancelled)
        ):
            raise HTTPException(status_code=404, detail="Annotation not found")
        return annotation, task, actor, access

    async def _lock_comment(
        self,
        comment: AnnotationComment,
        annotation: Annotation,
        task: Task,
        actor: IdentityActorFacts | IdentityActorStatus,
        *,
        expected_access: ProjectAccess,
        operation: Literal["edit", "delete"],
    ) -> tuple[AnnotationComment, IdentityActorFacts]:
        comment_id = comment.id
        annotation, _, actor, access = await self._lock_annotation_target(
            annotation,
            task,
            actor,
            expected_access=expected_access,
            require_active=False,
        )
        comment = await self.comments.lock(comment_id)
        if comment is None or not comment.is_active:
            raise HTTPException(status_code=404, detail="Comment not found")
        if comment.annotation_id != annotation.id:
            raise HTTPException(
                status_code=409, detail={"reason": "comment_target_changed"}
            )
        self._assert_action(comment, actor, access, operation=operation)
        return comment, actor
