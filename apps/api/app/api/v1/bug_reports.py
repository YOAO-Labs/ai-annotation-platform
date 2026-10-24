import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.ratelimit import limiter
from app.deps import get_db, get_current_user, require_roles
from app.db.enums import UserRole
from app.db.models.notification import Notification
from app.db.models.user import User
from app.schemas.bug_report import (
    BugReportCreate,
    BugReportUpdate,
    BugReportOut,
    BugReportDetail,
    BugReportList,
    BugCommentCreate,
    BugCommentOut,
    BugAttachment,
    BUG_ATTACHMENT_MIME_TYPES,
)
from app.services.bug_report import BugReportService
from app.services.audit import AuditService
from app.services.notification import NotificationService
from app.services import bug_attachment_uploads, bug_attachment_storage
from sqlalchemy import select
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
log = logging.getLogger(__name__)

_BUG_REOPEN_REDIS_POOL: ConnectionPool | None = None
_BUG_REOPEN_REDIS_POOL_LOOP: asyncio.AbstractEventLoop | None = None


def _get_bug_reopen_redis_pool() -> ConnectionPool:
    global _BUG_REOPEN_REDIS_POOL, _BUG_REOPEN_REDIS_POOL_LOOP
    loop = asyncio.get_running_loop()
    if _BUG_REOPEN_REDIS_POOL is None or _BUG_REOPEN_REDIS_POOL_LOOP is not loop:
        _BUG_REOPEN_REDIS_POOL = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=8,
            decode_responses=True,
        )
        _BUG_REOPEN_REDIS_POOL_LOOP = loop
    return _BUG_REOPEN_REDIS_POOL


def _get_bug_reopen_redis() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_get_bug_reopen_redis_pool())


async def close_bug_reopen_redis_pool() -> None:
    global _BUG_REOPEN_REDIS_POOL, _BUG_REOPEN_REDIS_POOL_LOOP
    if _BUG_REOPEN_REDIS_POOL is None:
        return
    try:
        await asyncio.wait_for(
            _BUG_REOPEN_REDIS_POOL.disconnect(inuse_connections=True),
            timeout=2.0,
        )
    except (asyncio.TimeoutError, Exception) as exc:
        log.debug("close_bug_reopen_redis_pool: %s (continuing shutdown)", exc)
    finally:
        _BUG_REOPEN_REDIS_POOL = None
        _BUG_REOPEN_REDIS_POOL_LOOP = None


class ScreenshotInitRequest(BaseModel):
    file_name: str = Field(default="screenshot.png", min_length=1, max_length=200)
    content_type: str = Field(default="image/png", min_length=1, max_length=100)

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, content_type: str) -> str:
        if content_type not in BUG_ATTACHMENT_MIME_TYPES:
            raise ValueError("BUG 截图仅支持 PNG / JPEG / WebP")
        return content_type


class ScreenshotInitResponse(BaseModel):
    storage_key: str
    upload_url: str
    expires_in: int = 900


@router.post(
    "/bug_reports/screenshot/upload-init",
    response_model=ScreenshotInitResponse,
    deprecated=True,
)
async def init_bug_screenshot_upload(
    data: ScreenshotInitRequest,
    current_user: User = Depends(get_current_user),
):
    """Retain the old contract symbols while refusing replayable final-key PUTs."""
    raise HTTPException(
        409,
        {
            "code": "bug_attachment_upload_init_retired",
            "upload_path": "/api/v1/bug_reports/screenshot/upload",
        },
    )


@router.post(
    "/bug_reports/screenshot/upload",
    response_model=BugAttachment,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in sorted(BUG_ATTACHMENT_MIME_TYPES)
            },
        }
    },
)
async def upload_bug_screenshot(
    request: Request,
    file_name: str = Query("screenshot.png", min_length=1, max_length=255),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept original image bytes and return their independently verified receipt."""
    mime_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime_type not in BUG_ATTACHMENT_MIME_TYPES:
        raise HTTPException(422, {"code": "bug_attachment_invalid_mime_type"})
    try:
        file_name = bug_attachment_storage.safe_file_name(file_name)
    except bug_attachment_storage.BugAttachmentStorageError as exc:
        raise bug_attachment_uploads.http_error(exc) from None
    authority = await bug_attachment_uploads.capture_authority(
        db, current_user, request
    )
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > bug_attachment_storage.MAX_BYTES:
            raise HTTPException(413, {"code": "bug_attachment_too_large"})
        data.extend(chunk)
    raw = bytes(data)
    await bug_attachment_uploads.validated_image(raw, mime_type)
    return await bug_attachment_uploads.upload(
        db,
        authority,
        raw,
        mime_type,
        file_name,
    )


def _bug_is_visible_to_user(report, current_user: User) -> bool:
    is_admin = current_user.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    return bool(is_admin or report.reporter_id == current_user.id)


@router.post("/bug_reports", response_model=BugReportOut, status_code=201)
@limiter.limit("10/hour")
async def create_bug_report(
    data: BugReportCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user, authority = await bug_attachment_uploads.lock_authority(
        db,
        current_user,
        request,
    )
    svc = BugReportService(db)
    create_fields = data.model_dump(exclude_unset=True)
    route = create_fields.pop("route", "") or str(request.url.path)
    owners = await bug_attachment_uploads.normalize_for_report(
        db,
        current_user.id,
        create_fields,
    )
    report = await svc.create(
        reporter_id=current_user.id,
        user_role=current_user.role,
        route=route,
        **create_fields,
    )
    bug_attachment_uploads.bind(owners, report.id)
    await AuditService.log(
        db,
        actor=current_user,
        action="bug_report.created",
        target_type="bug_report",
        target_id=str(report.id),
        request=request,
        status_code=201,
        detail={"display_id": report.display_id, "route": route},
    )
    await bug_attachment_uploads.reauthorize(db, authority)
    # Deadlines are checked again after report/mirror/audit and all row waits.
    for owner in owners:
        bug_attachment_uploads._ready(owner)
    await db.commit()
    await db.refresh(report)
    return report


@router.get(
    "/bug_reports",
    response_model=BugReportList,
    responses={
        200: {
            "content": {
                "application/json": {},
                "text/markdown": {"schema": {"type": "string"}},
            }
        }
    },
)
async def list_bug_reports(
    status: str | None = Query(None),
    severity: str | None = Query(None),
    route: str | None = Query(None),
    format: str | None = Query(None, alias="format"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    svc = BugReportService(db)
    if format == "markdown":
        from fastapi.responses import PlainTextResponse

        md = await svc.list_markdown(status=status or "new")
        return PlainTextResponse(content=md, media_type="text/markdown; charset=utf-8")

    items, total = await svc.list(
        status=status, severity=severity, route=route, limit=limit, offset=offset
    )
    return BugReportList(items=items, total=total)


@router.get("/bug_reports/mine", response_model=BugReportList)
async def list_my_bug_reports(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = BugReportService(db)
    items, total = await svc.list(
        reporter_id=current_user.id, limit=limit, offset=offset
    )
    return BugReportList(items=items, total=total)


@router.get("/bug_reports/{report_id}", response_model=BugReportDetail)
async def get_bug_report(
    report_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = BugReportService(db)
    report, comment_rows = await svc.get_with_comments(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    if not _bug_is_visible_to_user(report, current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    comments_out = [
        BugCommentOut(
            id=c.id,
            bug_report_id=c.bug_report_id,
            author_id=c.author_id,
            author_name=name,
            author_role=role,
            body=c.body,
            created_at=c.created_at,
        )
        for (c, name, role) in comment_rows
    ]
    return BugReportDetail(**{**report.__dict__, "comments": comments_out})


@router.get(
    "/bug_reports/{report_id}/attachments/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in sorted(BUG_ATTACHMENT_MIME_TYPES)
            }
        }
    },
)
async def download_bug_attachment(
    report_id: uuid.UUID,
    key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from urllib.parse import quote

    receipt, data = await bug_attachment_uploads.read(
        db,
        current_user,
        request,
        report_id,
        key,
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


@router.patch("/bug_reports/{report_id}", response_model=BugReportOut)
async def update_bug_report(
    report_id: uuid.UUID,
    data: BugReportUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = BugReportService(db)
    report = await svc.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    is_admin = current_user.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    if not is_admin and report.reporter_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能编辑自己提交的反馈")
    old_status = report.status
    update_fields = data.model_dump(exclude_unset=True)
    new_status = update_fields.get("status")
    report = await svc.update(report_id, **update_fields)
    await AuditService.log(
        db,
        actor=current_user,
        action="bug_report.status_changed",
        target_type="bug_report",
        target_id=str(report.id),
        request=request,
        status_code=200,
        detail={"display_id": report.display_id, "new_status": report.status},
    )
    pending_notifications = []
    if (
        new_status
        and new_status != old_status
        and current_user.id != report.reporter_id
    ):
        pending_notifications = await NotificationService(db).notify(
            user_id=report.reporter_id,
            type="bug_report.status_changed",
            target_type="bug_report",
            target_id=report.id,
            payload={
                "display_id": report.display_id,
                "title": report.title,
                "from_status": old_status,
                "to_status": new_status,
                "actor_name": current_user.name,
                "resolution": report.resolution,
            },
        )
    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed([pending_notifications])
    await db.refresh(report)
    return report


@router.delete("/bug_reports/{report_id}", status_code=204)
async def delete_bug_report(
    report_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user, authority = await bug_attachment_uploads.lock_authority(
        db,
        current_user,
        request,
    )
    svc = BugReportService(db)
    report = await bug_attachment_uploads.locked_report(db, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    is_admin = current_user.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    if not is_admin and report.reporter_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能删除自己提交的反馈")
    await AuditService.log(
        db,
        actor=current_user,
        action="bug_report.deleted",
        target_type="bug_report",
        target_id=str(report.id),
        request=request,
        status_code=204,
        detail={"display_id": report.display_id},
    )
    retired = await bug_attachment_uploads.retire_report(db, report)
    await svc.delete(report_id)
    await bug_attachment_uploads.reauthorize(db, authority)
    await db.commit()
    try:
        await bug_attachment_uploads.cleanup(db, identifiers=retired)
    except Exception:
        # Deletion is already committed; the durable owners remain retryable.
        log.warning(
            "Bug attachment cleanup deferred after report deletion: %s", report_id
        )
    return Response(status_code=204)


@router.post(
    "/bug_reports/{report_id}/comments", response_model=BugCommentOut, status_code=201
)
@limiter.limit("60/hour")
async def add_bug_comment(
    report_id: uuid.UUID,
    data: BugCommentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    svc = BugReportService(db)
    report = await svc.get(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Bug report not found")
    is_admin = current_user.role in (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    if not is_admin and report.reporter_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有反馈提交者或管理员可以评论")

    # v0.7.0：reopen 单独限流 5/day/user/report，避免提交者刷 reopen 计数。
    # 60/h 整体限流仍生效；本检查只针对会触发 reopen 的评论（reporter 自己 + 已 closed 状态）。
    will_reopen = current_user.id == report.reporter_id and report.status in (
        "fixed",
        "wont_fix",
        "duplicate",
    )
    if will_reopen:
        rkey = f"bug:reopen:{current_user.id}:{report_id}:day"
        r = _get_bug_reopen_redis()
        try:
            count = await r.incr(rkey)
            if count == 1:
                await r.expire(rkey, 86400)
        finally:
            await r.aclose(close_connection_pool=False)
        if count > 5:
            raise HTTPException(
                status_code=429,
                detail="reopen 次数超出每日上限（5 次/日）",
            )

    result = await svc.add_comment(report_id, current_user.id, data.body)
    if not result:
        raise HTTPException(status_code=404, detail="Bug report not found")
    comment, was_reopened, author_name, author_role = result

    await AuditService.log(
        db,
        actor=current_user,
        action="bug_comment.created",
        target_type="bug_report",
        target_id=str(report_id),
        request=request,
        status_code=201,
        detail={"display_id": report.display_id},
    )
    if was_reopened:
        await AuditService.log(
            db,
            actor=current_user,
            action="bug_report.reopened",
            target_type="bug_report",
            target_id=str(report_id),
            request=request,
            status_code=201,
            detail={
                "display_id": report.display_id,
                "reopen_count": report.reopen_count,
                "from_status": "fixed/wont_fix/duplicate",
                "to_status": "triaged",
            },
        )

    # Notification fan-out (published after the commit below)
    notif_svc = NotificationService(db)
    snippet = data.body[:120]
    pending_notifications: list[Notification] = []
    if current_user.id == report.reporter_id:
        # 提交者评论 → 通知 assignee；若没有则通知所有 SUPER_ADMIN
        recipient_ids: list[uuid.UUID] = []
        if report.assigned_to_id:
            recipient_ids = [report.assigned_to_id]
        else:
            admin_rows = await db.execute(
                select(User.id).where(
                    User.role == UserRole.SUPER_ADMIN.value, User.is_active.is_(True)
                )
            )
            recipient_ids = [r[0] for r in admin_rows.all()]
        recipient_ids = [uid for uid in recipient_ids if uid != current_user.id]
        if recipient_ids:
            pending_notifications = await notif_svc.notify_many(
                user_ids=recipient_ids,
                type="bug_report.reopened" if was_reopened else "bug_report.commented",
                target_type="bug_report",
                target_id=report.id,
                payload={
                    "display_id": report.display_id,
                    "title": report.title,
                    "actor_name": current_user.name,
                    "actor_role": current_user.role,
                    "snippet": snippet,
                    "reopen": was_reopened,
                    "reopen_count": report.reopen_count if was_reopened else None,
                },
            )
    else:
        # 管理员评论 → 通知 reporter
        if report.reporter_id != current_user.id:
            row = await notif_svc.notify(
                user_id=report.reporter_id,
                type="bug_report.commented",
                target_type="bug_report",
                target_id=report.id,
                payload={
                    "display_id": report.display_id,
                    "title": report.title,
                    "actor_name": current_user.name,
                    "actor_role": current_user.role,
                    "snippet": snippet,
                    "reopen": False,
                },
            )
            if row is not None:
                pending_notifications = [row]

    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(comment)
    return BugCommentOut(
        id=comment.id,
        bug_report_id=comment.bug_report_id,
        author_id=comment.author_id,
        author_name=author_name,
        author_role=author_role,
        body=comment.body,
        created_at=comment.created_at,
    )


@router.post("/bug_reports/cluster")
async def cluster_bug_reports(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
    ),
):
    svc = BugReportService(db)
    items, _ = await svc.list(status="new", limit=50)
    merged: list[dict] = []
    for item in items:
        similar = await svc.cluster_similar(item.id)
        if similar:
            merged.append(
                {"report_id": str(item.id), "similar_ids": [str(s) for s in similar]}
            )
    return {"clusters": merged}
