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
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.annotation_comment import AnnotationCommentWorkflow
from app.deps import (
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
    TaskDiscussionPage,
)
from app.services.audit import audit_origin_from_request
from app.services.identity_read_relations import account_profiles
from app.services.labeling_discussion_relations import annotation_comment_facts
from app.services.annotation_comment import AnnotationCommentService
from app.services.storage import storage_service
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
):
    comment, actor = await AnnotationCommentWorkflow(db).create(
        annotation_id,
        actor_id,
        data,
        origin=audit_origin_from_request(request),
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
    safe_name = data.file_name.replace("/", "_").replace("\\", "_")
    storage_key = f"{ATTACHMENT_KEY_PREFIX}{annotation_id}/{uuid.uuid4()}-{safe_name}"
    upload_url = storage_service.generate_upload_url(storage_key, data.content_type)
    return CommentAttachmentUploadInitResponse(
        storage_key=storage_key,
        upload_url=upload_url,
        expires_in=900,
    )


@router.get(
    "/annotations/{annotation_id}/comment-attachments/download",
    dependencies=[Depends(require_roles(*_ALL_ANNOTATORS))],
)
async def comment_attachment_download(
    annotation_id: uuid.UUID,
    key: str,
    as_json: bool = False,
    db: AsyncSession = Depends(get_db),
    actor_id: uuid.UUID = Depends(get_current_user_id),
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
    await require_visible_annotation(db, annotation_id, actor_id)
    # 评论附件私链要求严格 5 分钟有效期, 不走缓存对齐 (否则可能被拉长到 ~15 分钟)。
    url = storage_service.generate_download_url(key, expires_in=300, align=False)
    if as_json:
        return JSONResponse({"download_url": url})
    return RedirectResponse(url, status_code=302)
