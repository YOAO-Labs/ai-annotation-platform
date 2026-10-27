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
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.annotation_comment import AnnotationCommentWorkflow
from app.deps import (
    get_current_user,
    get_db,
    get_current_user_id,
    require_roles,
    require_active_task_actor,
)
from app.db.enums import PlatformRole
from app.db.models.annotation_comment import AnnotationComment
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
from app.services.audit import audit_origin_from_request
from app.services.identity_read_relations import account_profiles
from app.services.labeling_discussion_relations import annotation_comment_facts
from app.services.annotation_comment import AnnotationCommentService
from app.services.identity_contracts import IdentityActorFacts
from app.services import comment_attachment_uploads as attachment_uploads
from app.services import comment_attachment_storage as attachment_storage
from app.services.task_discussion import (
    list_task_discussion,
    require_visible_annotation,
    require_visible_task,
)

from app.services.labeling_core_read_relations import annotation_row

router = APIRouter(dependencies=[Depends(require_active_task_actor)])

#: Platform roles allowed to read/write task discussion.  The legacy global
#: annotator/reviewer account roles are historical and are not an authorization
#: source; project responsibility comes from the resolved membership.
_ALL_ANNOTATORS = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)


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


@router.get(
    "/annotations/{annotation_id}/comments",
    response_model=list[AnnotationCommentOut],
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def list_comments(
    annotation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """v0.8.8 · 旧端点保留作向后兼容；新调用方走 ``/comments/page`` keyset 分页。"""
    await require_visible_annotation(db, annotation_id, actor_id)
    author = account_profiles.alias("annotation_comment_author")
    rows = (
        await db.execute(
            select(annotation_comment_facts, author.c.name)
            .join(author, author.c.id == annotation_comment_facts.author_id)
            .where(
                annotation_comment_facts.annotation_id == annotation_id,
                annotation_comment_facts.is_active.is_(True),
            )
            .order_by(annotation_comment_facts.created_at.desc())
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
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def list_task_discussion_page(
    task_id: uuid.UUID,
    scope: Literal["all", "task", "annotation"] = Query("all"),
    annotation_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
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

    task = await require_visible_task(db, task_id, actor_id)
    if scope == "annotation":
        # Resolve the persisted annotation against the requested task.  This also
        # intentionally permits soft-deleted annotations for historical comments.
        await require_visible_annotation(
            db,
            annotation_id,
            actor_id,
            task_id=task_id,
        )

    return await list_task_discussion(
        db,
        task=task,
        actor_id=actor_id,
        scope=scope,
        annotation_id=annotation_id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/tasks/{task_id}/discussion/annotation-counts",
    response_model=AnnotationCommentCountsOut,
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def list_annotation_comment_counts(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """Return sparse counts for active comments on available saved annotations."""

    task = await require_visible_task(db, task_id, actor_id)
    rows = await AnnotationCommentService(db).counts_for_active_annotations(
        task_id=task_id, project_id=task.project_id
    )
    return AnnotationCommentCountsOut(
        counts={str(annotation_id): int(count) for annotation_id, count in rows}
    )


@router.get(
    "/tasks/{task_id}/comments/page",
    response_model=AnnotationCommentListPage,
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def list_task_comments_paged(
    task_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """I4 · 任务级评论 — DiscussionPanel 在未选中标注时降级展示.

    聚合该 task 下所有 annotation 的 active 评论, DESC(created_at, id) keyset 分页.
    """
    await require_visible_task(db, task_id, actor_id)
    author = account_profiles.alias("annotation_comment_author")
    q = (
        select(annotation_comment_facts, author.c.name)
        .join(author, author.c.id == annotation_comment_facts.author_id)
        .join(
            annotation_row, annotation_row.id == annotation_comment_facts.annotation_id
        )
        .where(
            annotation_row.task_id == task_id,
            annotation_comment_facts.is_active.is_(True),
        )
    )
    if cursor:
        last_ts, last_id = _decode_comment_cursor(cursor)
        q = q.where(
            or_(
                annotation_comment_facts.created_at < last_ts,
                and_(
                    annotation_comment_facts.created_at == last_ts,
                    annotation_comment_facts.id < last_id,
                ),
            )
        )
    q = q.order_by(
        annotation_comment_facts.created_at.desc(), annotation_comment_facts.id.desc()
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
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def list_comments_paged(
    annotation_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """v0.8.8 · keyset 分页：DESC(created_at, id)。

    单条标注 100+ 评论时初始化卡顿明显（CommentsPanel 全量拉），改为「最新 50
    + 加载更早」按需拉取。返回 ``next_cursor=None`` 即末尾。
    """
    await require_visible_annotation(db, annotation_id, actor_id)
    author = account_profiles.alias("annotation_comment_author")
    q = (
        select(annotation_comment_facts, author.c.name)
        .join(author, author.c.id == annotation_comment_facts.author_id)
        .where(
            annotation_comment_facts.annotation_id == annotation_id,
            annotation_comment_facts.is_active.is_(True),
        )
    )
    if cursor:
        last_ts, last_id = _decode_comment_cursor(cursor)
        q = q.where(
            or_(
                annotation_comment_facts.created_at < last_ts,
                and_(
                    annotation_comment_facts.created_at == last_ts,
                    annotation_comment_facts.id < last_id,
                ),
            )
        )
    q = q.order_by(
        annotation_comment_facts.created_at.desc(), annotation_comment_facts.id.desc()
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
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def create_comment(
    annotation_id: uuid.UUID,
    data: AnnotationCommentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
    current_user: IdentityActorFacts = Depends(get_current_user),
):
    authority = None
    rows: list = []
    attachment_payload: list[dict] | None = None
    if data.attachments:
        _, authority = await attachment_uploads.lock_authority(
            db, current_user, request
        )
        ctx = await attachment_uploads.context(
            db, current_user, annotation_id, active=True
        )
        rows, attachment_payload = await attachment_uploads.normalize_for_comment(
            db, current_user.id, ctx, data.attachments
        )
    comment, actor = await AnnotationCommentWorkflow(db).create(
        annotation_id,
        actor_id,
        data,
        origin=audit_origin_from_request(request),
        attachment_authority=authority,
        attachment_rows=rows,
        attachment_payload=attachment_payload,
    )
    return _to_out(comment, actor.name)


@router.patch(
    "/comments/{comment_id}",
    response_model=AnnotationCommentOut,
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def patch_comment(
    comment_id: uuid.UUID,
    data: AnnotationCommentUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    comment = await AnnotationCommentWorkflow(db).update(comment_id, actor_id, data)
    return _to_out(comment)


@router.delete(
    "/comments/{comment_id}",
    status_code=204,
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def delete_comment(
    comment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    await AnnotationCommentWorkflow(db).delete(
        comment_id,
        actor_id,
        origin=audit_origin_from_request(request),
    )


@router.post(
    "/annotations/{annotation_id}/comment-attachments/upload-init",
    response_model=CommentAttachmentUploadInitResponse,
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def comment_attachment_upload_init(
    annotation_id: uuid.UUID,
    data: CommentAttachmentUploadInitRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
):
    """v0.6.2：评论附件签发预签名 PUT URL。

    storage_key 形如 `comment-attachments/{aid}/{uuid}-{filename}`，固定前缀使得后端可
    校验 attachments[].storageKey；同时让 MinIO 桶层级清晰。"""
    _ann, _task = await require_visible_annotation(
        db,
        annotation_id,
        actor_id,
        require_active=True,
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
    current_user: IdentityActorFacts = Depends(require_roles(*_ALL_ANNOTATORS)),
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
    current_user: IdentityActorFacts = Depends(get_current_user),
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
