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
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
    Attachment,
    TaskDiscussionPage,
)
from app.services.audit import AuditAction, AuditService
from app.services.discussion_notifications import (
    prepare_annotation_comment_mention_notifications,
)
from app.services.discussion_actions import discussion_actions
from app.services.notification import NotificationService
from app.services import comment_attachment_uploads as attachment_uploads
from app.services import comment_attachment_storage as attachment_storage
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
    authority = None
    rows = []
    normalized = []
    if data.attachments:
        current_user, authority = await attachment_uploads.lock_authority(
            db, current_user, request
        )
        ctx = await attachment_uploads.context(
            db, current_user, annotation_id, active=True
        )
        task = ctx.task
        rows, normalized = await attachment_uploads.normalize_for_comment(
            db, current_user.id, ctx, data.attachments
        )
    else:
        _ann, task = await require_visible_annotation(
            db, annotation_id, current_user, require_active=True
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
        attachments=normalized,
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
    if authority is not None:
        await attachment_uploads.bind(db, rows, comment.id)
        await attachment_uploads.reauthorize(db, authority)
        attachment_uploads.validate_bound(rows, comment.id)
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
    current_user, authority = await attachment_uploads.lock_authority(
        db, current_user, request
    )
    with db.no_autoflush:
        annotation_id = await db.scalar(
            select(AnnotationComment.annotation_id).where(
                AnnotationComment.id == comment_id
            )
        )
    if annotation_id is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    ctx = await attachment_uploads.context(db, current_user, annotation_id)
    c = await attachment_uploads.locked_comment(db, comment_id)
    if c is None or not c.is_active:
        raise HTTPException(status_code=404, detail="Comment not found")
    if c.annotation_id != annotation_id:
        attachment_uploads.reject("comment_attachment_context_changed")
    access = ctx.access
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
    await attachment_uploads.retire_for_comments(db, [comment_id])
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
    await attachment_uploads.reauthorize(db, authority)
    await db.commit()
    return


@router.post(
    "/annotations/{annotation_id}/comment-attachments/upload-init",
    response_model=CommentAttachmentUploadInitResponse,
    deprecated=True,
)
async def comment_attachment_upload_init(
    annotation_id: uuid.UUID,
    data: CommentAttachmentUploadInitRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """旧上传初始化已停用，请改用认证文件上传。"""
    await require_visible_annotation(
        db, annotation_id, current_user, require_active=True
    )
    raise HTTPException(
        409,
        {
            "code": "comment_attachment_upload_init_retired",
            "upload_path": f"/api/v1/annotations/{annotation_id}/comment-attachments/upload",
        },
    )


@router.post(
    "/annotations/{annotation_id}/comment-attachments/upload",
    response_model=Attachment,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def upload_comment_attachment(
    annotation_id: uuid.UUID,
    request: Request,
    file_name: str = Query(..., min_length=1, max_length=255),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """上传最多20MiB的原文件字节并返回经核验的附件回执，允许空文件。"""
    if request.headers.get("content-encoding", "identity").lower() not in {
        "",
        "identity",
    }:
        raise HTTPException(415, {"code": "comment_attachment_content_encoding"})
    try:
        file_name = attachment_storage.safe_file_name(file_name)
        mime_type = attachment_storage.normalize_mime_type(
            request.headers.get("content-type")
        )
    except attachment_storage.CommentAttachmentStorageError as exc:
        raise attachment_uploads.http_error(exc) from None
    proof, ctx = await attachment_uploads.capture_upload(
        db, current_user, request, annotation_id
    )
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > attachment_storage.MAX_BYTES:
            raise HTTPException(413, {"code": "comment_attachment_too_large"})
        data.extend(chunk)
    return await attachment_uploads.upload(
        db, proof, ctx, bytes(data), mime_type, file_name
    )


@router.get(
    "/annotations/{annotation_id}/comment-attachments/download", response_class=Response
)
async def comment_attachment_download(
    annotation_id: uuid.UUID,
    key: str,
    request: Request,
    as_json: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_roles(*_ALL_ANNOTATORS)),
):
    """返回原评论已绑定且完整核验的文件字节；as_json仅保留参数兼容。"""
    from urllib.parse import quote

    # Keep the existing wrong-annotation-prefix rejection before any storage I/O.
    if not key.startswith(f"{ATTACHMENT_KEY_PREFIX}{annotation_id}/"):
        raise HTTPException(400, detail="invalid attachment key")
    receipt, data = await attachment_uploads.read(
        db, current_user, request, annotation_id, key
    )
    return Response(
        data,
        media_type=receipt.mime_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(receipt.file_name, safe=""),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
