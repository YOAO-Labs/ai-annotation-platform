"""I18 · AnnotationFeedback 统一反馈表 router.

端点:
- GET    /feedbacks?project_id=&task_id=&annotation_id=&kind=&anchor_type=&status=&cursor=
- POST   /feedbacks                         创建 (issue/comment/bug/reject)
- PATCH  /feedbacks/{id}                    改 status/severity/title/body
- DELETE /feedbacks/{id}                    软删 (is_active=false)
- POST   /feedbacks/{id}/replies            子评论 (thread_parent_id 链)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _assert_task_visible, _visible_task_ids
from app.api.v1.annotation_comments import _validate_project_members
from app.db.enums import PlatformRole, ProjectRole
from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.point_cloud_quality import PointCloudQualityIssue
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import (
    assert_project_visible,
    get_db,
    require_roles,
    require_active_task_actor,
)
from app.schemas.annotation_feedback import (
    AnnotationFeedbackCreate,
    AnnotationFeedbackListPage,
    AnnotationFeedbackOut,
    AnnotationFeedbackPatch,
    AnnotationFeedbackReply,
    AnnotationFeedbackThreadPage,
)
from app.schemas.user import UserBrief
from app.services.audit import AuditAction, AuditService
from app.services.discussion_notifications import (
    prepare_feedback_comment_mention_notifications,
    prepare_feedback_reply_notifications,
    prepare_feedback_status_notifications,
)
from app.services.discussion_actions import discussion_actions
from app.services.feedback import FeedbackService
from app.services.mask_qc.service import effective_issue_status
from app.services.point_cloud_quality.service import refresh_issue_staleness
from app.services.project_access import resolve_project_access_by_id
from app.services.notification import NotificationService
from app.services.user_brief import resolve_briefs

router = APIRouter(dependencies=[Depends(require_active_task_actor)])
logger = logging.getLogger(__name__)

#: Platform roles allowed to use feedback endpoints.  Project responsibility is
#: resolved from the membership; legacy global staff roles are not allowed.
_ALL = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)


async def _to_out(
    db: AsyncSession,
    entry: AnnotationFeedback,
    *,
    user: User | None = None,
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
        is_author=user is not None and entry.author_id == user.id,
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
    db: AsyncSession, project_id: uuid.UUID, user: User
) -> tuple[bool, str | None]:
    """Return ``(is_manager, project_role)`` from current project access.

    Used for feedback actions so moderators are the project's legitimate
    managers/reviewers rather than holders of a legacy global staff role.
    """

    _project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    return access.is_manager, access.project_role


def _same_task_id(left: uuid.UUID | None, right: uuid.UUID | None) -> bool:
    return left == right


async def _assert_feedback_scope(
    db: AsyncSession,
    entry: AnnotationFeedback,
    user: User,
    *,
    service: FeedbackService | None = None,
) -> AnnotationFeedback:
    """Recheck a feedback row's project/task/root visibility for every mutation."""

    svc = service or FeedbackService(db)
    root = await svc.resolve_root(entry.id)
    if (
        entry.project_id != root.project_id
        or not _same_task_id(entry.task_id, root.task_id)
        or entry.anchor_type != root.anchor_type
        or entry.annotation_id != root.annotation_id
    ):
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    await assert_project_visible(root.project_id, db, user)
    if root.task_id is not None:
        task = await db.get(Task, root.task_id)
        if task is None or task.project_id != root.project_id:
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )
        # The feedback project is denormalized.  Re-resolve the actual task's
        # project before applying batch visibility so a forged/malformed row
        # cannot borrow the caller's access to another project.
        await assert_project_visible(task.project_id, db, user)
        await _assert_task_visible(db, task, user)
    if root.annotation_id is not None:
        annotation = await db.get(Annotation, root.annotation_id)
        if (
            annotation is None
            or annotation.project_id != root.project_id
            or annotation.task_id != root.task_id
        ):
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )
    return root


async def _assert_create_scope(
    db: AsyncSession, payload: AnnotationFeedbackCreate, user: User
) -> Task | None:
    await assert_project_visible(payload.project_id, db, user)
    task: Task | None = None
    if payload.task_id is not None:
        task = await db.get(Task, payload.task_id)
        if task is None or task.project_id != payload.project_id:
            raise HTTPException(status_code=404, detail="Task not found")
        await assert_project_visible(task.project_id, db, user)
        await _assert_task_visible(db, task, user)
    if payload.annotation_id is not None:
        annotation = await db.get(Annotation, payload.annotation_id)
        if annotation is None:
            raise HTTPException(status_code=404, detail="Annotation not found")
        if (
            annotation.project_id != payload.project_id
            or annotation.task_id != payload.task_id
        ):
            # Keep the existing video-anchor API contract (422 for a foreign
            # object) while preventing a cross-task annotation from being
            # persisted by non-video callers.
            raise HTTPException(
                status_code=422,
                detail="Feedback annotation does not belong to task",
            )
        if (
            payload.kind == "issue"
            and payload.thread_parent_id is None
            and task is not None
            and task.file_type == "image"
            and (not annotation.is_active or bool(annotation.was_cancelled))
        ):
            raise HTTPException(
                status_code=422,
                detail={"reason": "feedback_annotation_unavailable"},
            )
    return task


def _assert_canvas_drawing_scope(
    payload: AnnotationFeedbackCreate, task: Task | None
) -> None:
    """Task drawings are native root comments on image tasks only."""

    if payload.canvas_drawing is None:
        return
    if task is None or task.file_type != "image":
        raise HTTPException(
            status_code=422,
            detail="canvas_drawing requires an image task",
        )


async def _quality_anchor_is_current(
    db: AsyncSession, entry: AnnotationFeedback
) -> bool:
    """Return whether a quality locator may accept a new reply.

    Unknown video-context JSON remains readable/replyable for compatibility;
    only the existing mask/point-cloud quality freshness checks gate replies.
    """

    anchor = entry.anchor_position or {}
    mask_qc_issue_id = anchor.get("mask_qc_issue_id")
    if mask_qc_issue_id:
        try:
            issue = await db.get(MaskQCIssue, uuid.UUID(str(mask_qc_issue_id)))
        except (TypeError, ValueError):
            return False
        if issue is None:
            return False
        return await effective_issue_status(db, issue) != "stale"
    point_cloud_issue_id = anchor.get("point_cloud_quality_issue_id")
    if point_cloud_issue_id:
        try:
            issue = await db.get(
                PointCloudQualityIssue, uuid.UUID(str(point_cloud_issue_id))
            )
        except (TypeError, ValueError):
            return False
        if issue is None:
            return False
        try:
            return not await refresh_issue_staleness(db, issue)
        except Exception:
            logger.exception("unable to validate point-cloud feedback anchor")
            return False
    return True


async def _assert_reply_parent(
    db: AsyncSession,
    parent: AnnotationFeedback,
    user: User,
) -> AnnotationFeedback:
    if not parent.is_active:
        raise HTTPException(status_code=404, detail="feedback not found")
    return await _assert_feedback_scope(db, parent, user)


def _serialize_anchor(payload: AnnotationFeedbackCreate) -> dict | None:
    anchor = payload.anchor_position
    if anchor is None:
        return None
    value = anchor.model_dump(mode="json", by_alias=True)
    if anchor.video_context is None:
        # Adding the optional context must not add null fields to legacy locators.
        value.pop("video_context", None)
    else:
        value["video_context"] = anchor.video_context.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    if payload.anchor_type == "pixel":
        for key in (
            "point_cloud_quality_issue_id",
            "scene_id",
            "scene_track_id",
            "auxiliary_layers",
        ):
            value.pop(key, None)
    elif payload.anchor_type == "point_cloud":
        value = {
            key: value[key]
            for key in (
                "frame",
                "point_cloud_quality_issue_id",
                "scene_id",
                "scene_track_id",
                "auxiliary_layers",
            )
        }
    return value


@router.get(
    "/feedbacks",
    response_model=AnnotationFeedbackListPage,
    response_model_exclude_unset=True,
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
    user: User = Depends(require_roles(*_ALL)),
):
    project, access = await resolve_project_access_by_id(
        db, user=user, project_id=project_id
    )
    if task_id is not None:
        task = await db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise HTTPException(status_code=404, detail="Task not found")
        await assert_project_visible(task.project_id, db, user)
        await _assert_task_visible(db, task, user)
    if annotation_id is not None:
        annotation = await db.get(Annotation, annotation_id)
        if (
            annotation is None
            or annotation.project_id != project_id
            or (task_id is not None and annotation.task_id != task_id)
        ):
            raise HTTPException(status_code=404, detail="Annotation not found")
        if task_id is None:
            task = await db.get(Task, annotation.task_id)
            if task is None or task.project_id != project_id:
                raise HTTPException(status_code=404, detail="Annotation not found")
            await assert_project_visible(task.project_id, db, user)
            await _assert_task_visible(db, task, user)
    allowed_task_ids: set[uuid.UUID] | None = None
    if not access.is_manager:
        project_task_ids = list(
            (
                await db.execute(select(Task.id).where(Task.project_id == project_id))
            ).scalars()
        )
        allowed_task_ids = await _visible_task_ids(db, project, user, project_task_ids)
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
                    is_author=r.author_id == user.id,
                    is_admin=access.is_manager,
                    is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
                    is_accessible=True,
                    can_reply=(
                        r.kind == "issue" and await _quality_anchor_is_current(db, r)
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
)
async def get_feedback_thread(
    root_feedback_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_ALL)),
):
    root_entry = await db.get(AnnotationFeedback, root_feedback_id)
    if root_entry is None or not root_entry.is_active:
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    if root_entry.thread_parent_id is not None:
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    if root_entry.kind == "comment" and root_entry.anchor_type == "annotation":
        # These rows are legacy annotation-comment mirrors.  Their source of
        # truth remains /comments; they must never become Issue threads.
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    await _assert_feedback_scope(db, root_entry, user)
    svc = FeedbackService(db)
    root, replies, next_cursor, total = await svc.thread_paged(
        root_feedback_id,
        cursor=cursor,
        limit=limit,
    )
    author_ids = {root.author_id, *(reply.author_id for reply in replies)}
    briefs = await resolve_briefs(db, author_ids)
    thread_is_manager, thread_project_role = await _project_action_context(
        db, root.project_id, user
    )
    root_out = await _to_out(
        db,
        root,
        user=user,
        project_role=thread_project_role,
        is_manager=thread_is_manager,
        can_reply=(root.kind == "issue" and await _quality_anchor_is_current(db, root)),
        briefs=briefs,
    )
    reply_out = [
        await _to_out(
            db,
            reply,
            user=user,
            project_role=thread_project_role,
            is_manager=thread_is_manager,
            can_reply=(
                reply.kind == "issue" and await _quality_anchor_is_current(db, reply)
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


@router.post("/feedbacks", response_model=AnnotationFeedbackOut)
async def create_feedback(
    payload: AnnotationFeedbackCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_ALL)),
):
    task = await _assert_create_scope(db, payload, user)
    _assert_canvas_drawing_scope(payload, task)
    if payload.mentions:
        await _validate_project_members(
            db, payload.project_id, [mention.user_id for mention in payload.mentions]
        )
    svc = FeedbackService(db)
    root: AnnotationFeedback | None = None
    if payload.thread_parent_id is not None:
        if payload.kind != "comment":
            raise HTTPException(
                status_code=422,
                detail="feedback replies must use kind=comment",
            )
        if payload.severity is not None or payload.title is not None:
            raise HTTPException(
                status_code=422,
                detail="feedback replies cannot set severity or title",
            )
        parent, _ = await svc.validate_parent(
            payload.thread_parent_id,
            project_id=payload.project_id,
            task_id=payload.task_id,
            annotation_id=payload.annotation_id,
            anchor_type=payload.anchor_type,
            anchor_position=_serialize_anchor(payload),
        )
        root = await _assert_reply_parent(db, parent, user)
        if root.kind == "comment" and root.anchor_type == "annotation":
            raise HTTPException(
                status_code=404, detail="feedback thread is unavailable"
            )
        if not payload.attachments and not payload.body.strip():
            raise HTTPException(
                status_code=422, detail="feedback replies must contain text"
            )
        # The generic create endpoint remains compatible with legacy native
        # feedback replies (including non-Issue comment roots).  The shared
        # policy intentionally keeps their UI ``actions.reply`` false; only
        # annotation-comment mirror roots are unavailable here.
    anchor = payload.anchor_position
    if anchor and anchor.mask_qc_issue_id:
        issue = await db.get(MaskQCIssue, anchor.mask_qc_issue_id)
        if issue is None:
            raise HTTPException(status_code=404, detail="Mask QC issue not found")
        expected_bbox = issue.region_bbox
        expected_bbox_values = (
            [
                expected_bbox["x0"],
                expected_bbox["y0"],
                expected_bbox["x1"],
                expected_bbox["y1"],
            ]
            if expected_bbox is not None
            else None
        )
        actual_bbox = list(anchor.region_bbox) if anchor.region_bbox else None
        if (
            issue.project_id != payload.project_id
            or issue.task_id != payload.task_id
            or issue.annotation_id != payload.annotation_id
            or issue.frame_start != anchor.frame
            or issue.region_digest != anchor.region_digest
            or actual_bbox != expected_bbox_values
        ):
            raise HTTPException(
                status_code=409,
                detail={"reason": "mask_qc_feedback_anchor_conflict"},
            )
        if await effective_issue_status(db, issue) == "stale":
            raise HTTPException(
                status_code=409,
                detail={"reason": "mask_qc_issue_stale"},
            )
    if anchor and anchor.point_cloud_quality_issue_id:
        issue = await db.get(
            PointCloudQualityIssue, anchor.point_cloud_quality_issue_id
        )
        if issue is None or issue.task_id is None:
            raise HTTPException(status_code=404, detail="3D Quality issue not found")
        issue_task = await db.get(Task, issue.task_id)
        if issue_task is None:
            raise HTTPException(status_code=404, detail="3D Quality issue not found")
        await _assert_task_visible(db, issue_task, user)
        locator = issue.locator or {}
        if (
            issue.project_id != payload.project_id
            or issue.task_id != payload.task_id
            or issue.annotation_id != payload.annotation_id
            or issue.scene_id != anchor.scene_id
            or issue.scene_track_id != anchor.scene_track_id
            or issue.frame_start != anchor.frame
            or locator.get("auxiliary_layers") != anchor.auxiliary_layers
        ):
            raise HTTPException(
                status_code=409,
                detail={"reason": "point_cloud_quality_feedback_anchor_conflict"},
            )
        if await refresh_issue_staleness(db, issue):
            raise HTTPException(
                status_code=409,
                detail={"reason": "point_cloud_quality_issue_stale"},
            )
    entry = await svc.create(
        author_id=user.id,
        kind=payload.kind,
        anchor_type=payload.anchor_type,
        project_id=payload.project_id,
        task_id=payload.task_id,
        annotation_id=payload.annotation_id,
        anchor_position=_serialize_anchor(payload),
        severity=payload.severity,
        title=payload.title,
        body=payload.body,
        attachments=payload.attachments,
        mentions=[
            mention.model_dump(by_alias=True, mode="json")
            for mention in payload.mentions
        ],
        thread_parent_id=payload.thread_parent_id,
        canvas_drawing=(
            payload.canvas_drawing.model_dump(mode="json")
            if payload.canvas_drawing is not None
            else None
        ),
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.FEEDBACK_CREATED,
        target_type="feedback",
        target_id=entry.id,
        request=request,
        status_code=200,
        detail={
            "kind": entry.kind,
            "anchor_type": entry.anchor_type,
            "project_id": str(entry.project_id),
            "task_id": str(entry.task_id) if entry.task_id else None,
            "annotation_id": (
                str(entry.annotation_id) if entry.annotation_id else None
            ),
            "mention_count": len(payload.mentions),
        },
    )
    pending_notifications = []
    if task is not None:
        pending_notifications = await prepare_feedback_comment_mention_notifications(
            db,
            comment=entry,
            task=task,
            actor=user,
            mentioned_user_ids=[mention.user_id for mention in payload.mentions],
        )
    if root is not None:
        pending_notifications.extend(
            await prepare_feedback_reply_notifications(
                db,
                root=root,
                reply=entry,
                actor=user,
            )
        )
    await db.commit()
    await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(entry)
    create_is_manager, create_project_role = await _project_action_context(
        db, entry.project_id, user
    )
    return await _to_out(
        db,
        entry,
        user=user,
        project_role=create_project_role,
        is_manager=create_is_manager,
        can_reply=(
            entry.kind == "issue" and await _quality_anchor_is_current(db, entry)
        ),
    )


@router.patch("/feedbacks/{feedback_id}", response_model=AnnotationFeedbackOut)
async def patch_feedback(
    feedback_id: uuid.UUID,
    payload: AnnotationFeedbackPatch,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_ALL)),
):
    entry = await db.get(AnnotationFeedback, feedback_id)
    if entry is None or not entry.is_active:
        raise HTTPException(status_code=404, detail="feedback not found")
    svc = FeedbackService(db)
    root = await _assert_feedback_scope(db, entry, user, service=svc)
    # Reviewers may change an Issue status, but a mixed request is rejected as
    # a whole.  Checking model_fields_set also catches explicit nulls.
    is_author = entry.author_id == user.id
    is_admin, action_project_role = await _project_action_context(
        db, entry.project_id, user
    )
    is_reviewer = action_project_role == ProjectRole.REVIEWER.value
    fields = payload.model_fields_set
    forbidden_reviewer_fields = fields & {"severity", "title", "body", "mentions"}
    reviewer_status_only = is_reviewer and not (is_author or is_admin)
    if reviewer_status_only and forbidden_reviewer_fields:
        raise HTTPException(
            status_code=403,
            detail="reviewers may update Issue status only",
        )
    capabilities = discussion_actions(
        "feedback",
        entry.kind,
        is_author=is_author,
        is_admin=is_admin,
        is_reviewer=is_reviewer,
        is_accessible=True,
        can_reply=False,
    )
    wants_status = "status" in fields and payload.status is not None
    wants_content = bool(fields & {"severity", "title", "body", "mentions"})
    if reviewer_status_only and not wants_status:
        raise HTTPException(
            status_code=403,
            detail="reviewers may update Issue status only",
        )
    if (wants_status and not capabilities.change_status) or (
        wants_content and not capabilities.edit
    ):
        raise HTTPException(status_code=403, detail="not allowed")
    if payload.mentions is not None:
        if not (
            entry.kind == "comment"
            and entry.anchor_type == "task"
            and entry.thread_parent_id is None
        ):
            raise HTTPException(
                status_code=422,
                detail="mentions require a native root task comment",
            )
        await _validate_project_members(
            db, root.project_id, [mention.user_id for mention in payload.mentions]
        )
    if (
        payload.body is not None
        and not entry.attachments
        and not (entry.canvas_drawing or {}).get("shapes")
        and not payload.body.strip()
        and (
            entry.thread_parent_id is not None
            or (entry.kind == "comment" and entry.anchor_type == "task")
        )
    ):
        raise HTTPException(
            status_code=422, detail="feedback replies must contain text"
        )
    old_status = entry.status
    updated = await svc.patch(
        feedback_id,
        actor_id=user.id,
        status=payload.status,
        severity=payload.severity,
        title=payload.title,
        body=payload.body,
        mentions=(
            [
                mention.model_dump(by_alias=True, mode="json")
                for mention in payload.mentions
            ]
            if payload.mentions is not None
            else None
        ),
    )
    if payload.status is not None and payload.status != old_status:
        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.FEEDBACK_STATUS_CHANGED,
            target_type="feedback",
            target_id=feedback_id,
            request=request,
            status_code=200,
            detail={"from": old_status, "to": payload.status},
        )
    pending_notifications = []
    if (
        payload.status is not None
        and payload.status != old_status
        and entry.id == root.id
        and root.kind == "issue"
    ):
        pending_notifications = await prepare_feedback_status_notifications(
            db,
            root=root,
            actor=user,
            from_status=old_status,
            to_status=payload.status,
        )
    await db.commit()
    await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(updated)
    return await _to_out(
        db,
        updated,
        user=user,
        project_role=action_project_role,
        is_manager=is_admin,
        can_reply=(root.kind == "issue" and await _quality_anchor_is_current(db, root)),
    )


@router.delete("/feedbacks/{feedback_id}", status_code=204)
async def delete_feedback(
    feedback_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_ALL)),
):
    entry = await db.get(AnnotationFeedback, feedback_id)
    if entry is None or not entry.is_active:
        raise HTTPException(status_code=404, detail="feedback not found")
    svc = FeedbackService(db)
    await _assert_feedback_scope(db, entry, user, service=svc)
    is_author = entry.author_id == user.id
    delete_is_manager, delete_project_role = await _project_action_context(
        db, entry.project_id, user
    )
    capabilities = discussion_actions(
        "feedback",
        entry.kind,
        is_author=is_author,
        is_admin=delete_is_manager,
        is_reviewer=delete_project_role == ProjectRole.REVIEWER.value,
        is_accessible=True,
        can_reply=False,
    )
    if not capabilities.delete:
        raise HTTPException(status_code=403, detail="not allowed")
    await svc.soft_delete(feedback_id)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.FEEDBACK_DELETED,
        target_type="feedback",
        target_id=feedback_id,
        request=request,
        status_code=204,
        detail=None,
    )
    await db.commit()


@router.post("/feedbacks/{feedback_id}/replies", response_model=AnnotationFeedbackOut)
async def reply_feedback(
    feedback_id: uuid.UUID,
    payload: AnnotationFeedbackReply,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(*_ALL)),
):
    parent = await db.get(AnnotationFeedback, feedback_id)
    if parent is None or not parent.is_active:
        raise HTTPException(status_code=404, detail="feedback not found")
    svc = FeedbackService(db)
    root = await _assert_reply_parent(db, parent, user)
    if root.kind == "comment" and root.anchor_type == "annotation":
        raise HTTPException(status_code=404, detail="feedback thread is unavailable")
    mask_qc_issue_id = (parent.anchor_position or {}).get("mask_qc_issue_id")
    if mask_qc_issue_id:
        issue = await db.get(MaskQCIssue, uuid.UUID(str(mask_qc_issue_id)))
        if issue is None or await effective_issue_status(db, issue) == "stale":
            raise HTTPException(
                status_code=409,
                detail={"reason": "mask_qc_issue_stale"},
            )
    point_cloud_quality_issue_id = (parent.anchor_position or {}).get(
        "point_cloud_quality_issue_id"
    )
    if point_cloud_quality_issue_id:
        issue = await db.get(
            PointCloudQualityIssue, uuid.UUID(str(point_cloud_quality_issue_id))
        )
        if issue is None or issue.task_id is None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "point_cloud_quality_issue_stale"},
            )
        issue_task = await db.get(Task, issue.task_id)
        if issue_task is None:
            raise HTTPException(
                status_code=409,
                detail={"reason": "point_cloud_quality_issue_stale"},
            )
        await _assert_task_visible(db, issue_task, user)
        if await refresh_issue_staleness(db, issue):
            raise HTTPException(
                status_code=409,
                detail={"reason": "point_cloud_quality_issue_stale"},
            )
    if not payload.attachments and not payload.body.strip():
        raise HTTPException(
            status_code=422, detail="feedback replies must contain text"
        )
    # 子评论继承 parent 的 anchor; kind 强制为 comment.
    reply = await svc.create(
        author_id=user.id,
        kind="comment",
        anchor_type=parent.anchor_type,
        project_id=parent.project_id,
        task_id=parent.task_id,
        annotation_id=parent.annotation_id,
        anchor_position=parent.anchor_position,
        severity=None,
        title=None,
        body=payload.body,
        attachments=payload.attachments,
        thread_parent_id=parent.id,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.FEEDBACK_CREATED,
        target_type="feedback",
        target_id=reply.id,
        request=request,
        status_code=200,
        detail={"reply_to": str(parent.id)},
    )
    pending_notifications = await prepare_feedback_reply_notifications(
        db,
        root=root,
        reply=reply,
        actor=user,
    )
    await db.commit()
    await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(reply)
    reply_is_manager, reply_project_role = await _project_action_context(
        db, root.project_id, user
    )
    return await _to_out(
        db,
        reply,
        user=user,
        project_role=reply_project_role,
        is_manager=reply_is_manager,
    )
