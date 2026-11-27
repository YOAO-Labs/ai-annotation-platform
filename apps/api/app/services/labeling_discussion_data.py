"""Execute Labeling discussion and event commands in the caller transaction."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import make_transient_to_detached

from yoao_labeling.discussion_contract import (
    DISCUSSION_PROTOCOL,
    AnnotationCommentRow,
    ClearAnnotationCommentAttachments,
    CreateAnnotationComment,
    DeleteCommentsForTasks,
    DiscussionCommand,
    DiscussionData,
    LabelingDiscussionUnavailable,
    LabelingTaskEventsUnavailable,
    LockAnnotationComment,
    SoftDeleteAnnotationComment,
    TaskEventCommand,
    UpdateAnnotationComment,
)
from yoao_labeling.discussion_data import (
    LabelingDiscussionDataClient,
    LabelingTaskEventDataClient,
)
from app.db.models.annotation_comment import AnnotationComment
from app.services.labeling_annotation_data import _native_error_fields
from app.services.labeling_core_read_relations import annotation_row
from app.services.labeling_discussion_relations import annotation_comment_facts

_LOCK_FAILURES = {"55P03", "40P01", "40001"}


async def discussion_command(
    db: AsyncSession,
    request: DiscussionCommand,
    *,
    flush_before: bool = True,
) -> DiscussionData:
    if flush_before:
        await db.flush()

    async def execute(payload: str):
        async with db.begin_nested():
            with db.no_autoflush:
                return await db.scalar(
                    text(
                        "SELECT yoao_labeling_v1.discussion_command(CAST(:request AS jsonb))"
                    ),
                    {"request": payload},
                )

    try:
        return await LabelingDiscussionDataClient(execute).command(request)
    except LabelingDiscussionUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "labeling_discussion_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_labeling_discussion_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "labeling_discussion_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in _LOCK_FAILURES:
            reason = (
                "comment_busy"
                if request.operation.endswith("comment")
                else "feedback_busy"
            )
            raise HTTPException(409, detail={"reason": reason}) from exc
        raise


async def task_event_command(
    db: AsyncSession,
    request: TaskEventCommand,
    *,
    flush_before: bool = True,
):
    if flush_before and db.autoflush:
        await db.flush()

    async def execute(payload: str):
        async with db.begin_nested():
            with db.no_autoflush:
                return await db.scalar(
                    text(
                        "SELECT yoao_labeling_v1.task_event_command(CAST(:request AS jsonb))"
                    ),
                    {"request": payload},
                )

    try:
        return await LabelingTaskEventDataClient(execute).command(request)
    except LabelingTaskEventsUnavailable as exc:
        raise HTTPException(
            503, detail={"reason": "labeling_task_events_unavailable"}
        ) from exc
    except DBAPIError as exc:
        message, detail, sqlstate = _native_error_fields(exc)
        if message == "aap_labeling_task_events_error":
            try:
                value = json.loads(detail or "{}")
                status_code = int(value["status_code"])
                response_detail = value["detail"]
            except (KeyError, TypeError, ValueError):
                status_code, response_detail = (
                    409,
                    {"reason": "task_event_conflict"},
                )
            raise HTTPException(status_code, detail=response_detail) from exc
        if sqlstate in _LOCK_FAILURES:
            raise HTTPException(409, detail={"reason": "task_event_busy"}) from exc
        raise


class AnnotationCommentData:
    """Read comment projections and apply Labeling commands in the caller transaction."""

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

    async def ids_for_task_deletion(self, task_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        doomed_comments = (
            select(annotation_comment_facts.id)
            .join(
                annotation_row,
                annotation_row.id == annotation_comment_facts.annotation_id,
            )
            .where(annotation_row.task_id.in_(task_ids))
        )
        with self.db.no_autoflush:
            return list(await self.db.scalars(doomed_comments))

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
