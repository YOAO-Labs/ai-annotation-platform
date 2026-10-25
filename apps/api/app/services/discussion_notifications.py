"""Notification fan-out for task discussion events.

Discussion notifications are deliberately prepared in the request transaction and
published by the route only after its business commit succeeds.  This module owns
recipient selection; :mod:`app.services.notification` remains the persistence and
transport owner.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import task_access
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.notification import Notification
from app.db.models.task import Task
from app.services.identity_contracts import IdentityActorFacts
from app.services.identity_read_relations import account_profiles
from app.services.labeling_core_read_relations import get_core_row, task_row
from app.services.project_access import resolve_project_access_by_id
from app.services.feedback import FeedbackService
from app.services.notification import NotificationService


def _ordered_unique(
    user_ids: list[uuid.UUID], *, exclude: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Preserve event order while removing duplicate/actor recipients."""

    seen: set[uuid.UUID] = set()
    result: list[uuid.UUID] = []
    for user_id in user_ids:
        if exclude is not None and user_id == exclude:
            continue
        if user_id in seen:
            continue
        seen.add(user_id)
        result.append(user_id)
    return result


async def _accessible_active_user_ids(
    db: AsyncSession,
    user_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Return active recipients who can currently open the task.

    Discussion navigation is task-owned.  A project-only feedback row has no
    current task to authorize or navigate to, so it produces no discussion event.
    Visibility is checked as the candidate user, not as the actor who created the
    event; this preserves assignment, batch-state, video-collaboration and project
    membership rules from the existing helpers.
    """

    if not user_ids or task_id is None:
        return []

    task = await get_core_row(db, task_row, task_id)
    if task is None or task.project_id != project_id:
        return []

    ordered_ids = _ordered_unique(user_ids)
    if not ordered_ids:
        return []
    if db.autoflush:
        await db.flush()
    active_ids = set(
        (
            await db.execute(
                select(account_profiles.c.id).where(
                    account_profiles.c.id.in_(ordered_ids),
                    account_profiles.c.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )

    visible: list[uuid.UUID] = []
    for user_id in ordered_ids:
        if user_id not in active_ids:
            continue
        try:
            await resolve_project_access_by_id(
                db, actor_id=user_id, project_id=task.project_id
            )
            await task_access.assert_task_visible(db, task, user_id)
        except HTTPException:
            # A transferred/hidden task is not a notification destination.  Keep
            # the same 404-hiding behavior as the regular discussion read routes.
            continue
        visible.append(user_id)
    return visible


async def _active_descendant_author_ids(
    db: AsyncSession, root: AnnotationFeedback
) -> list[uuid.UUID]:
    """Return authors of every active descendant, including nested replies.

    ``thread_paged`` intentionally returns a page.  Status fan-out must not be
    page-limited, so reuse its cycle-aware recursive CTE and hydrate author IDs
    from the source table.  Inactive intermediate rows remain traversal nodes in
    the CTE, while inactive reply rows themselves are excluded.
    """

    return await FeedbackService(db).active_descendant_author_ids(root)


def _feedback_payload(
    *,
    root: AnnotationFeedback,
    actor: IdentityActorFacts,
    reply_id: uuid.UUID | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
) -> dict[str, str | None]:
    payload: dict[str, str | None] = {
        "project_id": str(root.project_id),
        "task_id": str(root.task_id) if root.task_id is not None else None,
        "source": "feedback",
        "actor_name": actor.name,
    }
    if reply_id is not None:
        payload["reply_id"] = str(reply_id)
    if from_status is not None:
        payload["from_status"] = from_status
    if to_status is not None:
        payload["to_status"] = to_status
    return payload


async def prepare_feedback_reply_notifications(
    db: AsyncSession,
    *,
    root: AnnotationFeedback,
    reply: AnnotationFeedback,
    actor: IdentityActorFacts,
) -> list[Notification]:
    """Persist deferred reply notifications for an Issue root.

    The caller must invoke ``publish_committed`` on the returned rows after the
    enclosing business transaction commits.
    """

    if (
        root.kind != "issue"
        or root.thread_parent_id is not None
        or reply.thread_parent_id is None
    ):
        return []

    recipient_ids = await _accessible_active_user_ids(
        db,
        [root.author_id],
        project_id=root.project_id,
        task_id=root.task_id,
    )
    if not recipient_ids:
        return []
    return await NotificationService(db).notify_many(
        user_ids=_ordered_unique(recipient_ids, exclude=actor.id),
        type="feedback.reply_created",
        target_type="feedback",
        target_id=root.id,
        payload=_feedback_payload(root=root, actor=actor, reply_id=reply.id),
        defer_publish=True,
    )


async def prepare_feedback_status_notifications(
    db: AsyncSession,
    *,
    root: AnnotationFeedback,
    actor: IdentityActorFacts,
    from_status: str,
    to_status: str,
) -> list[Notification]:
    """Persist deferred status notifications for an actual Issue root change."""

    if root.kind != "issue" or root.thread_parent_id is not None:
        return []

    descendant_authors = await _active_descendant_author_ids(db, root)
    recipient_ids = await _accessible_active_user_ids(
        db,
        _ordered_unique([root.author_id, *descendant_authors], exclude=actor.id),
        project_id=root.project_id,
        task_id=root.task_id,
    )
    if not recipient_ids:
        return []
    return await NotificationService(db).notify_many(
        user_ids=recipient_ids,
        type="feedback.status_changed",
        target_type="feedback",
        target_id=root.id,
        payload=_feedback_payload(
            root=root,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
        ),
        defer_publish=True,
    )


async def prepare_annotation_comment_mention_notifications(
    db: AsyncSession,
    *,
    comment: AnnotationComment,
    task: Task,
    actor: IdentityActorFacts,
    mentioned_user_ids: list[uuid.UUID],
) -> list[Notification]:
    """Persist deferred notifications for validated legacy comment mentions."""

    if comment.project_id is None or comment.project_id != task.project_id:
        return []
    recipient_ids = await _accessible_active_user_ids(
        db,
        _ordered_unique(mentioned_user_ids, exclude=actor.id),
        project_id=task.project_id,
        task_id=task.id,
    )
    if not recipient_ids:
        return []
    return await NotificationService(db).notify_many(
        user_ids=recipient_ids,
        type="annotation.comment_mentioned",
        target_type="annotation_comment",
        target_id=comment.id,
        payload={
            "project_id": str(task.project_id),
            "task_id": str(task.id),
            "source": "annotation_comment",
            "actor_name": actor.name,
            "annotation_id": str(comment.annotation_id),
        },
        defer_publish=True,
    )


async def prepare_feedback_comment_mention_notifications(
    db: AsyncSession,
    *,
    comment: AnnotationFeedback,
    task: Task,
    actor: IdentityActorFacts,
    mentioned_user_ids: list[uuid.UUID],
) -> list[Notification]:
    """Persist deferred notifications for a native task-comment mention."""

    if (
        comment.kind != "comment"
        or comment.anchor_type != "task"
        or comment.thread_parent_id is not None
        or comment.project_id != task.project_id
        or comment.task_id != task.id
    ):
        return []
    recipient_ids = await _accessible_active_user_ids(
        db,
        _ordered_unique(mentioned_user_ids, exclude=actor.id),
        project_id=task.project_id,
        task_id=task.id,
    )
    if not recipient_ids:
        return []
    return await NotificationService(db).notify_many(
        user_ids=recipient_ids,
        type="feedback.comment_mentioned",
        target_type="feedback",
        target_id=comment.id,
        payload={
            "project_id": str(task.project_id),
            "task_id": str(task.id),
            "source": "feedback",
            "actor_name": actor.name,
        },
        defer_publish=True,
    )
