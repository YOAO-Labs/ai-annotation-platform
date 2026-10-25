"""I18 · AnnotationFeedback 统一反馈表 router.

端点:
- GET    /feedbacks?project_id=&task_id=&annotation_id=&kind=&anchor_type=&status=&cursor=
- POST   /feedbacks                         创建 (issue/comment/bug/reject)
- PATCH  /feedbacks/{id}                    改 status/severity/title/body
- DELETE /feedbacks/{id}                    软删 (is_active=false)
- POST   /feedbacks/{id}/replies            子评论 (thread_parent_id 链)
"""

from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.annotation_feedback import AnnotationFeedbackWorkflow
from app.application.task_workflow_errors import TaskWorkflowError
from app.deps import (
    assert_project_visible,
    get_db,
    get_current_user_id,
    require_roles,
    require_active_task_actor,
)
from app.db.enums import PlatformRole, ProjectRole
from app.db.models.annotation_feedback import AnnotationFeedback
from app.schemas.annotation_feedback import (
    AnnotationFeedbackCreate,
    AnnotationFeedbackListPage,
    AnnotationFeedbackOut,
    AnnotationFeedbackPatch,
    AnnotationFeedbackReply,
    AnnotationFeedbackThreadPage,
)
from app.schemas.user import UserBrief
from app.services import task_access
from app.services.audit import audit_origin_from_request
from app.services.discussion_actions import discussion_actions
from app.services.feedback import FeedbackService
from app.services.project_access import resolve_project_access_by_id
from app.services.user_brief import resolve_briefs

from app.services.labeling_core_read_relations import (
    annotation_row,
    get_core_row,
    task_row,
)

router = APIRouter(dependencies=[Depends(require_active_task_actor)])

#: Platform roles allowed to use feedback endpoints.  Project responsibility is
#: resolved from the membership; legacy global staff roles are not allowed.
_ALL = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)


def _raise_workflow_error(error: TaskWorkflowError) -> NoReturn:
    raise HTTPException(status_code=error.status_code, detail=error.detail) from error


async def _to_out(
    db: AsyncSession,
    entry: AnnotationFeedback,
    *,
    actor_id: uuid.UUID | None = None,
    project_role: str | None = None,
    is_manager: bool = False,
    is_accessible: bool = True,
    can_reply: bool = False,
    briefs: dict[str, UserBrief] | None = None,
) -> AnnotationFeedbackOut:
    if briefs is None:
        briefs = await resolve_briefs(db, [entry.author_id])
    brief = briefs.get(str(entry.author_id))
    actions = discussion_actions(
        "feedback",
        entry.kind,
        is_author=actor_id is not None and entry.author_id == actor_id,
        is_admin=is_manager,
        is_reviewer=project_role == ProjectRole.REVIEWER.value,
        is_accessible=is_accessible and entry.is_active,
        can_reply=can_reply,
    )
    return AnnotationFeedbackOut(
        id=entry.id,
        kind=entry.kind,
        anchor_type=entry.anchor_type,
        project_id=entry.project_id,
        task_id=entry.task_id,
        annotation_id=entry.annotation_id,
        anchor_position=entry.anchor_position,
        status=entry.status,
        severity=entry.severity,
        title=entry.title,
        body=entry.body,
        author_id=entry.author_id,
        author_name=brief.name if brief else None,
        attachments=entry.attachments or [],
        mentions=entry.mentions or [],
        canvas_drawing=entry.canvas_drawing,
        thread_parent_id=entry.thread_parent_id,
        is_active=entry.is_active,
        resolved_at=entry.resolved_at,
        resolved_by_id=entry.resolved_by_id,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        actions=actions,
    )


async def _project_action_context(
    db: AsyncSession, project_id: uuid.UUID, actor_id: uuid.UUID
) -> tuple[bool, str | None]:
    """Return ``(is_manager, project_role)`` from current project access.

    Used for feedback actions so moderators are the project's legitimate
    managers/reviewers rather than holders of a legacy global staff role.
    """

    _project, access = await resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )
    return access.is_manager, access.project_role


@router.get(
    "/feedbacks",
    response_model=AnnotationFeedbackListPage,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def list_feedbacks(
    project_id: uuid.UUID = Query(...),
    task_id: uuid.UUID | None = None,
    annotation_id: uuid.UUID | None = None,
    kind: str | None = None,
    anchor_type: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    root_only: bool = Query(False),
    include_counts: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    project, access = await resolve_project_access_by_id(
        db, actor_id=actor_id, project_id=project_id
    )
    if task_id is not None:
        task = await get_core_row(db, task_row, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(status_code=404, detail="Task not found")
        await assert_project_visible(task.project_id, db, actor_id)
        await task_access.assert_task_visible(db, task, actor_id)
    if annotation_id is not None:
        annotation = await get_core_row(db, annotation_row, annotation_id)
        if (
            annotation is None
            or annotation.project_id != project_id
            or (task_id is not None and annotation.task_id != task_id)
        ):
            raise HTTPException(status_code=404, detail="Annotation not found")
        if task_id is None:
            task = await get_core_row(db, task_row, annotation.task_id)
            if task is None or task.project_id != project_id:
                raise HTTPException(status_code=404, detail="Annotation not found")
            await assert_project_visible(task.project_id, db, actor_id)
            await task_access.assert_task_visible(db, task, actor_id)
    allowed_task_ids: set[uuid.UUID] | None = None
    if not access.is_manager:
        project_task_ids = list(
            (
                await db.execute(
                    select(task_row.id).where(task_row.project_id == project_id)
                )
            ).scalars()
        )
        allowed_task_ids = await task_access.visible_task_ids(
            db, project, actor_id, project_task_ids
        )
    svc = FeedbackService(db)
    rows, next_cursor = await svc.list_paged(
        project_id=project_id,
        task_id=task_id,
        annotation_id=annotation_id,
        kind=kind,
        anchor_type=anchor_type,
        status=status,
        allowed_task_ids=allowed_task_ids,
        cursor=cursor,
        limit=limit,
        root_only=root_only,
    )
    total: int | None = None
    status_counts: dict[str, int] | None = None
    if include_counts:
        total = await svc.count_paged(
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            kind=kind,
            anchor_type=anchor_type,
            status=status,
            allowed_task_ids=allowed_task_ids,
            root_only=root_only,
        )
        status_counts = await svc.status_counts(
            project_id=project_id,
            task_id=task_id,
            annotation_id=annotation_id,
            kind=kind,
            anchor_type=anchor_type,
            allowed_task_ids=allowed_task_ids,
            root_only=root_only,
        )
    # 一次解析全部 author
    author_ids = {r.author_id for r in rows}
    briefs = await resolve_briefs(db, list(author_ids))
    items: list[AnnotationFeedbackOut] = []
    workflow = AnnotationFeedbackWorkflow(db)
    for r in rows:
        brief = briefs.get(str(r.author_id))
        items.append(
            AnnotationFeedbackOut(
                id=r.id,
                kind=r.kind,
                anchor_type=r.anchor_type,
                project_id=r.project_id,
                task_id=r.task_id,
                annotation_id=r.annotation_id,
                anchor_position=r.anchor_position,
                status=r.status,
                severity=r.severity,
                title=r.title,
                body=r.body,
                author_id=r.author_id,
                author_name=brief.name if brief else None,
                attachments=r.attachments or [],
                mentions=r.mentions or [],
                canvas_drawing=r.canvas_drawing,
                thread_parent_id=r.thread_parent_id,
                is_active=r.is_active,
                resolved_at=r.resolved_at,
                resolved_by_id=r.resolved_by_id,
                created_at=r.created_at,
                updated_at=r.updated_at,
                actions=discussion_actions(
                    "feedback",
                    r.kind,
                    is_author=r.author_id == actor_id,
                    is_admin=access.is_manager,
                    is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
                    is_accessible=True,
                    can_reply=(
                        r.kind == "issue"
                        and await workflow.quality_anchor_is_current(r)
                    ),
                ),
            )
        )
    response = {"items": items, "next_cursor": next_cursor}
    if include_counts:
        response.update(total=total, status_counts=status_counts)
    return AnnotationFeedbackListPage(**response)


@router.get(
    "/feedbacks/{root_feedback_id}/thread",
    response_model=AnnotationFeedbackThreadPage,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def get_feedback_thread(
    root_feedback_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    root_entry = await FeedbackService(db).get(root_feedback_id, populate_existing=True)
    if root_entry is None or not root_entry.is_active:
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    if root_entry.thread_parent_id is not None:
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    if root_entry.kind == "comment" and root_entry.anchor_type == "annotation":
        # These rows are legacy annotation-comment mirrors.  Their source of
        # truth remains /comments; they must never become Issue threads.
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    workflow = AnnotationFeedbackWorkflow(db)
    try:
        await workflow.assert_feedback_scope(root_entry.id, actor_id=actor_id)
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
    svc = FeedbackService(db)
    root, replies, next_cursor, total = await svc.thread_paged(
        root_feedback_id,
        cursor=cursor,
        limit=limit,
    )
    author_ids = {root.author_id, *(reply.author_id for reply in replies)}
    briefs = await resolve_briefs(db, author_ids)
    thread_is_manager, thread_project_role = await _project_action_context(
        db, root.project_id, actor_id
    )
    root_out = await _to_out(
        db,
        root,
        actor_id=actor_id,
        project_role=thread_project_role,
        is_manager=thread_is_manager,
        can_reply=(
            root.kind == "issue" and await workflow.quality_anchor_is_current(root)
        ),
        briefs=briefs,
    )
    reply_out = [
        await _to_out(
            db,
            reply,
            actor_id=actor_id,
            project_role=thread_project_role,
            is_manager=thread_is_manager,
            can_reply=(
                reply.kind == "issue"
                and await workflow.quality_anchor_is_current(reply)
            ),
            briefs=briefs,
        )
        for reply in replies
    ]
    return AnnotationFeedbackThreadPage(
        root=root_out,
        items=reply_out,
        next_cursor=next_cursor,
        total=total,
    )


@router.post(
    "/feedbacks",
    response_model=AnnotationFeedbackOut,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def create_feedback(
    payload: AnnotationFeedbackCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    try:
        result = await AnnotationFeedbackWorkflow(db).create(
            payload,
            actor_id=actor_id,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
    return await _to_out(
        db,
        result.entry,
        actor_id=actor_id,
        project_role=result.project_role,
        is_manager=result.is_manager,
        can_reply=result.can_reply,
    )


@router.patch(
    "/feedbacks/{feedback_id}",
    response_model=AnnotationFeedbackOut,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def patch_feedback(
    feedback_id: uuid.UUID,
    payload: AnnotationFeedbackPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    try:
        result = await AnnotationFeedbackWorkflow(db).patch(
            feedback_id,
            payload,
            actor_id=actor_id,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
    return await _to_out(
        db,
        result.entry,
        actor_id=actor_id,
        project_role=result.project_role,
        is_manager=result.is_manager,
        can_reply=result.can_reply,
    )


@router.delete(
    "/feedbacks/{feedback_id}",
    status_code=204,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def delete_feedback(
    feedback_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    try:
        await AnnotationFeedbackWorkflow(db).soft_delete(
            feedback_id,
            actor_id=actor_id,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)


@router.post(
    "/feedbacks/{feedback_id}/replies",
    response_model=AnnotationFeedbackOut,
    dependencies=[Depends(require_roles(*_ALL))],
)
async def reply_feedback(
    feedback_id: uuid.UUID,
    payload: AnnotationFeedbackReply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    try:
        result = await AnnotationFeedbackWorkflow(db).reply(
            feedback_id,
            payload,
            actor_id=actor_id,
            origin=audit_origin_from_request(request),
        )
    except TaskWorkflowError as exc:
        _raise_workflow_error(exc)
    return await _to_out(
        db,
        result.entry,
        actor_id=actor_id,
        project_role=result.project_role,
        is_manager=result.is_manager,
        can_reply=result.can_reply,
    )
