"""v0.5.4 逐框评论路由：annotation_comments 表 CRUD + audit_log 通知钩子。

注册到 api/v1/router.py 后端点路径形如：
- GET    /annotations/{aid}/comments
- POST   /annotations/{aid}/comments
- PATCH  /comments/{id}
- DELETE /comments/{id}（软删）
- POST   /annotations/{aid}/comment-attachments/upload-init  (v0.6.2)
"""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import (
    get_db,
    require_roles,
    require_active_task_actor,
)
from app.db.enums import MANAGER_PLATFORM_ROLES, PlatformRole, ProjectRole
from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.schemas.annotation_comment import (
    ATTACHMENT_KEY_PREFIX,
    AnnotationCommentCreate,
    AnnotationCommentCountsOut,
    AnnotationCommentListPage,
    AnnotationCommentOut,
    AnnotationCommentUpdate,
    CommentAttachmentUploadInitRequest,
    CommentAttachmentUploadInitResponse,
    TaskDiscussionPage,
)
from app.services.audit import AuditAction, AuditService
from app.services.discussion_notifications import (
    prepare_annotation_comment_mention_notifications,
)
from app.services.discussion_actions import discussion_actions
from app.services.notification import NotificationService
from app.services.storage import storage_service
from app.services.task_discussion import (
    list_task_discussion,
    require_visible_annotation,
    require_visible_task,
)

router = APIRouter(dependencies=[Depends(require_active_task_actor)])

#: Platform roles allowed to read/write task discussion.  The legacy global
#: annotator/reviewer account roles are historical and are not an authorization
#: source; project responsibility comes from the resolved membership.
_ALL_ANNOTATORS = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)


async def _resolve_task_access(db: AsyncSession, task, user: User):
    """Resolve current project access for a task's actual project."""

    from app.db.models.project import Project
    from app.services.project_access import resolve_project_access

    project = await db.get(Project, task.project_id)
    return await resolve_project_access(db, user=user, project=project)


def _to_out(
    c: AnnotationComment, author_name: str | None = None
) -> AnnotationCommentOut:
    return AnnotationCommentOut(
        id=c.id,
        annotation_id=c.annotation_id,
        project_id=c.project_id,
        author_id=c.author_id,
        author_name=author_name,
        body=c.body,
        is_resolved=c.is_resolved,
        is_active=c.is_active,
        mentions=c.mentions or [],
        attachments=c.attachments or [],
        canvas_drawing=c.canvas_drawing,
        anchor=c.anchor,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


async def _validate_project_members(
    db: AsyncSession,
    project_id: uuid.UUID,
    user_ids: list[uuid.UUID],
) -> None:
    """mentions[].userId 必须是该项目的成员（含 owner / project_admin / super_admin 不强制走 project_members 表）。
    出于简化，仅校验 user 存在且是该项目 project_members 表成员；超管不在表里时也允许。"""
    if not user_ids:
        return
    rows = (
        await db.execute(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id.in_(user_ids),
            )
        )
    ).all()
    found = {r[0] for r in rows}
    # 超管 / 合法项目负责人 也允许（不一定在 project_members 表中）。
    # 平台 project_admin 仅在其拥有该项目时才算负责人，不与成员身份混淆。
    from app.db.models.project import Project

    manager_rows = (
        await db.execute(
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
        await db.execute(
            select(User.id).where(
                User.id.in_(user_ids),
                User.role == PlatformRole.SUPER_ADMIN.value,
            )
        )
    ).all()
    found |= {r[0] for r in manager_rows}
    found |= {r[0] for r in super_rows}
    missing = [str(uid) for uid in user_ids if uid not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"error": "mentions_invalid", "non_member_user_ids": missing},
        )


@router.get(
    "/annotations/{annotation_id}/comments", response_model=list[AnnotationCommentOut]
)
async def list_comments(
    annotation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """v0.8.8 · 旧端点保留作向后兼容；新调用方走 ``/comments/page`` keyset 分页。"""
    await require_visible_annotation(db, annotation_id, current_user)
    rows = (
        await db.execute(
            select(AnnotationComment, User.name)
            .join(User, User.id == AnnotationComment.author_id)
            .where(
                AnnotationComment.annotation_id == annotation_id,
                AnnotationComment.is_active.is_(True),
            )
            .order_by(AnnotationComment.created_at.desc())
        )
    ).all()
    return [_to_out(c, name) for c, name in rows]


def _encode_comment_cursor(ts: datetime, cid: uuid.UUID) -> str:
    """v0.8.8 · base64-urlsafe(<iso_ts>|<uuid_hex>)，与 task cursor 同款，避免 + / 在 URL 中的问题。"""
    iso = ts.isoformat()
    return base64.urlsafe_b64encode(f"{iso}|{cid.hex}".encode()).decode()


def _decode_comment_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = base64.urlsafe_b64decode(raw.encode()).decode()
        ts_part, id_hex = decoded.split("|", 1)
        return datetime.fromisoformat(ts_part), uuid.UUID(id_hex)
    except (ValueError, IndexError, UnicodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc


@router.get(
    "/tasks/{task_id}/discussion/page",
    response_model=TaskDiscussionPage,
)
async def list_task_discussion_page(
    task_id: uuid.UUID,
    scope: Literal["all", "task", "annotation"] = Query("all"),
    annotation_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """Read the authoritative, mixed comments feed for one visible task."""

    if scope not in {"all", "task", "annotation"}:
        raise HTTPException(status_code=422, detail="invalid discussion scope")
    if scope == "annotation" and annotation_id is None:
        raise HTTPException(
            status_code=422,
            detail="annotation_id is required for annotation scope",
        )
    if scope != "annotation" and annotation_id is not None:
        raise HTTPException(
            status_code=422,
            detail="annotation_id is only valid for annotation scope",
        )

    task = await require_visible_task(db, task_id, current_user)
    if scope == "annotation":
        # Resolve the persisted annotation against the requested task.  This also
        # intentionally permits soft-deleted annotations for historical comments.
        await require_visible_annotation(
            db,
            annotation_id,
            current_user,
            task_id=task_id,
        )

    return await list_task_discussion(
        db,
        task=task,
        user=current_user,
        scope=scope,
        annotation_id=annotation_id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/tasks/{task_id}/discussion/annotation-counts",
    response_model=AnnotationCommentCountsOut,
)
async def list_annotation_comment_counts(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """Return sparse counts for active comments on available saved annotations."""

    task = await require_visible_task(db, task_id, current_user)
    rows = (
        await db.execute(
            select(AnnotationComment.annotation_id, func.count(AnnotationComment.id))
            .join(Annotation, Annotation.id == AnnotationComment.annotation_id)
            .where(
                Annotation.task_id == task_id,
                Annotation.project_id == task.project_id,
                Annotation.is_active.is_(True),
                Annotation.was_cancelled.is_(False),
                AnnotationComment.is_active.is_(True),
            )
            .group_by(AnnotationComment.annotation_id)
        )
    ).all()
    return AnnotationCommentCountsOut(
        counts={str(annotation_id): int(count) for annotation_id, count in rows}
    )


@router.get(
    "/tasks/{task_id}/comments/page",
    response_model=AnnotationCommentListPage,
)
async def list_task_comments_paged(
    task_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """I4 · 任务级评论 — DiscussionPanel 在未选中标注时降级展示.

    聚合该 task 下所有 annotation 的 active 评论, DESC(created_at, id) keyset 分页.
    """
    await require_visible_task(db, task_id, current_user)
    q = (
        select(AnnotationComment, User.name)
        .join(User, User.id == AnnotationComment.author_id)
        .join(Annotation, Annotation.id == AnnotationComment.annotation_id)
        .where(
            Annotation.task_id == task_id,
            AnnotationComment.is_active.is_(True),
        )
    )
    if cursor:
        last_ts, last_id = _decode_comment_cursor(cursor)
        q = q.where(
            or_(
                AnnotationComment.created_at < last_ts,
                and_(
                    AnnotationComment.created_at == last_ts,
                    AnnotationComment.id < last_id,
                ),
            )
        )
    q = q.order_by(
        AnnotationComment.created_at.desc(), AnnotationComment.id.desc()
    ).limit(limit)
    rows = (await db.execute(q)).all()
    items = [_to_out(c, name) for c, name in rows]
    next_cursor: str | None = None
    if len(rows) == limit and rows:
        last = rows[-1][0]
        next_cursor = _encode_comment_cursor(last.created_at, last.id)
    return AnnotationCommentListPage(items=items, next_cursor=next_cursor)


@router.get(
    "/annotations/{annotation_id}/comments/page",
    response_model=AnnotationCommentListPage,
)
async def list_comments_paged(
    annotation_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """v0.8.8 · keyset 分页：DESC(created_at, id)。

    单条标注 100+ 评论时初始化卡顿明显（CommentsPanel 全量拉），改为「最新 50
    + 加载更早」按需拉取。返回 ``next_cursor=None`` 即末尾。
    """
    await require_visible_annotation(db, annotation_id, current_user)
    q = (
        select(AnnotationComment, User.name)
        .join(User, User.id == AnnotationComment.author_id)
        .where(
            AnnotationComment.annotation_id == annotation_id,
            AnnotationComment.is_active.is_(True),
        )
    )
    if cursor:
        last_ts, last_id = _decode_comment_cursor(cursor)
        q = q.where(
            or_(
                AnnotationComment.created_at < last_ts,
                and_(
                    AnnotationComment.created_at == last_ts,
                    AnnotationComment.id < last_id,
                ),
            )
        )
    q = q.order_by(
        AnnotationComment.created_at.desc(), AnnotationComment.id.desc()
    ).limit(limit)

    rows = (await db.execute(q)).all()
    items = [_to_out(c, name) for c, name in rows]
    next_cursor: str | None = None
    if len(rows) == limit and rows:
        last = rows[-1][0]
        next_cursor = _encode_comment_cursor(last.created_at, last.id)
    return AnnotationCommentListPage(items=items, next_cursor=next_cursor)


@router.post(
    "/annotations/{annotation_id}/comments",
    response_model=AnnotationCommentOut,
    status_code=201,
)
async def create_comment(
    annotation_id: uuid.UUID,
    data: AnnotationCommentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    _ann, task = await require_visible_annotation(
        db,
        annotation_id,
        current_user,
        require_active=True,
    )

    # mentions 必须是项目成员
    if data.mentions:
        await _validate_project_members(
            db, task.project_id, [m.user_id for m in data.mentions]
        )

    comment = AnnotationComment(
        id=uuid.uuid4(),
        annotation_id=annotation_id,
        project_id=task.project_id,
        author_id=current_user.id,
        body=data.body,
        mentions=[m.model_dump(by_alias=True, mode="json") for m in data.mentions],
        attachments=[
            a.model_dump(by_alias=True, mode="json") for a in data.attachments
        ],
        canvas_drawing=data.canvas_drawing.model_dump(by_alias=True, mode="json")
        if data.canvas_drawing
        else None,
        anchor=data.anchor.model_dump(by_alias=True, mode="json")
        if data.anchor
        else None,
    )
    db.add(comment)
    await db.flush()
    # ADR-0027 第二段 · 双写到 annotation_feedbacks (kind=comment, anchor=annotation)
    if task.project_id is not None:
        from app.services.feedback import FeedbackService

        await FeedbackService(db).mirror_annotation_comment(comment, task_id=task.id)
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_COMMENT_ADD,
        target_type="annotation",
        target_id=str(annotation_id),
        request=request,
        status_code=201,
        detail={
            "project_id": str(task.project_id),
            "comment_id": str(comment.id),
            "preview": data.body[:120],
            "mention_count": len(data.mentions),
            "attachment_count": len(data.attachments),
            "has_canvas_drawing": data.canvas_drawing is not None,
            "has_anchor": data.anchor is not None,
        },
    )
    pending_notifications = await prepare_annotation_comment_mention_notifications(
        db,
        comment=comment,
        task=task,
        actor=current_user,
        mentioned_user_ids=[mention.user_id for mention in data.mentions],
    )
    await db.commit()
    await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(comment)
    return _to_out(comment, current_user.name)


@router.patch("/comments/{comment_id}", response_model=AnnotationCommentOut)
async def patch_comment(
    comment_id: uuid.UUID,
    data: AnnotationCommentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    c = await db.get(AnnotationComment, comment_id)
    if not c or not c.is_active:
        raise HTTPException(status_code=404, detail="Comment not found")
    _ann, task = await require_visible_annotation(db, c.annotation_id, current_user)
    access = await _resolve_task_access(db, task, current_user)
    actions = discussion_actions(
        "annotation_comment",
        "comment",
        is_author=c.author_id == current_user.id,
        is_admin=access.is_manager,
        is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
        is_accessible=True,
        can_reply=False,
    )
    if not actions.edit:
        raise HTTPException(
            status_code=403, detail="Only the author can edit this comment"
        )
    if data.body is not None:
        # 清空正文需保证评论仍有附件 / 画布批注（与 create 的 _require_content 同口径）。
        if not data.body.strip():
            has_drawing = bool(
                c.canvas_drawing and (c.canvas_drawing.get("shapes") or [])
            )
            if not c.attachments and not has_drawing:
                raise HTTPException(
                    status_code=422,
                    detail="评论需至少包含正文、附件或画布批注之一",
                )
        c.body = data.body
    if data.is_resolved is not None:
        c.is_resolved = data.is_resolved
    await db.commit()
    await db.refresh(c)
    return _to_out(c)


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    c = await db.get(AnnotationComment, comment_id)
    if not c or not c.is_active:
        raise HTTPException(status_code=404, detail="Comment not found")
    _ann, task = await require_visible_annotation(db, c.annotation_id, current_user)
    access = await _resolve_task_access(db, task, current_user)
    actions = discussion_actions(
        "annotation_comment",
        "comment",
        is_author=c.author_id == current_user.id,
        is_admin=access.is_manager,
        is_reviewer=access.project_role == ProjectRole.REVIEWER.value,
        is_accessible=True,
        can_reply=False,
    )
    if not actions.delete:
        raise HTTPException(
            status_code=403, detail="Only the author can delete this comment"
        )
    c.is_active = False
    # v0.7.2 · annotation 编辑历史可追溯
    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.ANNOTATION_COMMENT_DELETE,
        target_type="annotation",
        target_id=str(c.annotation_id),
        request=request,
        status_code=204,
        detail={"comment_id": str(comment_id), "preview": (c.body or "")[:120]},
    )
    await db.commit()
    return


@router.post(
    "/annotations/{annotation_id}/comment-attachments/upload-init",
    response_model=CommentAttachmentUploadInitResponse,
)
async def comment_attachment_upload_init(
    annotation_id: uuid.UUID,
    data: CommentAttachmentUploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """v0.6.2：评论附件签发预签名 PUT URL。

    storage_key 形如 `comment-attachments/{aid}/{uuid}-{filename}`，固定前缀使得后端可
    校验 attachments[].storageKey；同时让 MinIO 桶层级清晰。"""
    _ann, _task = await require_visible_annotation(
        db,
        annotation_id,
        current_user,
        require_active=True,
    )
    safe_name = data.file_name.replace("/", "_").replace("\\", "_")
    storage_key = f"{ATTACHMENT_KEY_PREFIX}{annotation_id}/{uuid.uuid4()}-{safe_name}"
    upload_url = storage_service.generate_upload_url(storage_key, data.content_type)
    return CommentAttachmentUploadInitResponse(
        storage_key=storage_key,
        upload_url=upload_url,
        expires_in=900,
    )


@router.get("/annotations/{annotation_id}/comment-attachments/download")
async def comment_attachment_download(
    annotation_id: uuid.UUID,
    key: str,
    as_json: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """Authorize the original annotation before issuing a short-lived download URL.

    Legacy clients retain the redirect. Bearer-authenticated browser callers
    request JSON first so a normal link does not omit the authorization header
    or forward it to the object-storage redirect target.
    """
    expected_prefix = f"{ATTACHMENT_KEY_PREFIX}{annotation_id}/"
    if not key.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="invalid attachment key")
    # Resolve the annotation's actual task/project.  The path parameter is only an
    # identifier; access must not be granted from a stale denormalized project_id.
    await require_visible_annotation(db, annotation_id, current_user)
    # 评论附件私链要求严格 5 分钟有效期, 不走缓存对齐 (否则可能被拉长到 ~15 分钟)。
    url = storage_service.generate_download_url(key, expires_in=300, align=False)
    if as_json:
        return JSONResponse({"download_url": url})
    return RedirectResponse(url, status_code=302)
