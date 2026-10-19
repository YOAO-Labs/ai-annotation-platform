import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ratelimit import limiter
from app.core.security import hash_password, verify_password
from app.deps import get_current_user, get_db
from app.db.models.user import User
from app.schemas.me import AvatarRefUpdate, PasswordChange, ProfileUpdate
from app.schemas.user import UserOut, UserPreferencesRead

from app.services import avatar as avatar_service
from app.services.avatar_image import (
    ALLOWED_CONTENT_TYPES as ALLOWED_IMAGE_CONTENT_TYPES,
    MAX_AVATAR_FILE_BYTES,
    AvatarImageError,
    normalize_avatar,
)
from app.services.audit import AuditAction, AuditService
from app.services.deactivation_service import DeactivationService
from app.services.identity_commands import IdentityCommandService
from app.services.notification import NotificationService
from aap_identity.preferences import (
    PreferencePolicyError,
    PreferenceValidationError,
    patch_preferences,
    read_preferences,
)

router = APIRouter()


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v0.8.3 · 在线状态心跳。

    前端每 30s 调一次（document.visibilityState === 'visible' 时），刷新
    last_seen_at 与 status='online'。Celery beat `mark_inactive_offline` 任务
    据 last_seen_at 把超 OFFLINE_THRESHOLD_MINUTES 的用户置 offline。
    """
    identity = IdentityCommandService(db)
    user = await identity.lock_active_account(user.id)
    identity.record_heartbeat_for_locked_user(user, datetime.now(timezone.utc))
    await db.commit()
    return None


class DeactivationRequest(BaseModel):
    """v0.8.1 · 自助注销申请：可附原因（≤500 字符）。"""

    reason: str | None = Field(default=None, max_length=500)


@router.patch("", response_model=UserOut)
async def update_profile(
    payload: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    identity = IdentityCommandService(db)
    user = await identity.lock_active_account(user.id)
    new_name = payload.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="姓名不能为空")
    old_name = identity.update_name_for_locked_user(user, new_name)

    if user.name != old_name:
        await AuditService.log(
            db,
            actor=user,
            action=AuditAction.USER_PROFILE_UPDATE,
            target_type="user",
            target_id=str(user.id),
            request=request,
            status_code=200,
            detail={"old_name": old_name, "new_name": user.name},
        )
    await db.commit()
    await db.refresh(user)
    return user


async def _apply_avatar_ref(
    db: AsyncSession,
    user: User,
    new_ref: str | None,
    request: Request,
    *,
    detail: dict,
) -> User:
    """Write the avatar reference for a caller-locked active account, then clean up.

    The caller acquires the fresh account lock before any object write. Commit before
    deleting the previous object so a failed commit cannot leave a dangling reference.
    """
    identity = IdentityCommandService(db)
    previous_ref = user.avatar_ref
    if previous_ref == new_ref:
        return user
    identity.update_avatar_for_locked_user(user, new_ref)
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_PROFILE_UPDATE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"field": "avatar", "avatar_ref": new_ref, **detail},
    )
    await db.commit()
    await db.refresh(user)
    await asyncio.to_thread(avatar_service.delete_ref_object, previous_ref)
    return user


@router.post("/avatar", response_model=UserOut)
@limiter.limit("10/minute")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上传自定义头像。

    服务端是唯一可信方:客户端声明的 MIME 只作早退,真实格式 / 像素规模由 Pillow 判定,
    产物统一为 256×256 方形 WebP 后写入 ``avatars`` 桶。请求体另有中间件级上限
    (``middleware/upload_body_limits``),此处再按文件字节复核一次。
    """
    declared = (file.content_type or "").lower()
    if declared and declared not in {
        "application/octet-stream",
        *ALLOWED_IMAGE_CONTENT_TYPES,
    }:
        raise HTTPException(status_code=415, detail="仅支持 PNG / JPEG / WebP 格式")

    raw = await file.read(MAX_AVATAR_FILE_BYTES + 1)
    # ``normalize_avatar`` 同步解码 / 裁剪 / 重编码，最坏可处理 4000 万像素的压缩图；
    # ``write_avatar_object`` 是同步 boto3 PUT。都放进线程池，避免单 worker 生产配置下
    # 一次上传阻塞整个事件循环。
    try:
        normalized = await asyncio.to_thread(normalize_avatar, raw)
    except AvatarImageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = await IdentityCommandService(db).lock_active_account(user.id)

    token = avatar_service.new_upload_token()
    await asyncio.to_thread(avatar_service.write_avatar_object, token, normalized)
    return await _apply_avatar_ref(
        db,
        user,
        avatar_service.build_upload_ref(token),
        request,
        detail={"kind": "upload", "bytes": len(normalized)},
    )


@router.patch("/avatar", response_model=UserOut)
async def set_avatar_ref(
    payload: AvatarRefUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """选择内置头像（``preset:<slug>``）或清除头像（``avatar_ref: null``）。

    ``upload:`` 前缀一律拒绝,否则用户可以把别人的 token 挂到自己名下。
    """
    try:
        new_ref = avatar_service.validate_client_ref(payload.avatar_ref)
    except avatar_service.InvalidAvatarRef as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user = await IdentityCommandService(db).lock_active_account(user.id)
    return await _apply_avatar_ref(
        db, user, new_ref, request, detail={"kind": "preset_or_clear"}
    )


@router.delete("/avatar", response_model=UserOut)
async def clear_avatar(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """恢复默认头像（= 回退首字母圆片）。"""
    user = await IdentityCommandService(db).lock_active_account(user.id)
    return await _apply_avatar_ref(
        db, user, None, request, detail={"kind": "preset_or_clear"}
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    identity = IdentityCommandService(db)
    user = await identity.lock_active_account(user.id)
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")

    identity.set_password_for_locked_user(user, hash_password(payload.new_password))
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_PASSWORD_CHANGE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=204,
        detail={"email": user.email},
    )
    await db.commit()
    return None


@router.post("/deactivation-request", response_model=UserOut)
async def request_self_deactivation(
    payload: DeactivationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v0.8.1 · 自助注销申请。7 天冷静期，期间可撤销。"""
    pending_notifications = await DeactivationService.request(
        db, user=user, reason=payload.reason, request=request
    )
    await db.commit()
    if pending_notifications:
        await NotificationService(db).publish_committed(pending_notifications)
    await db.refresh(user)
    return user


@router.get("/preferences", response_model=UserPreferencesRead)
async def get_preferences(user: User = Depends(get_current_user)) -> JSONResponse:
    """v0.9.41 · 读取当前用户的标注偏好。空字段走 schema 默认值。"""
    return JSONResponse(read_preferences(user.preferences or {}))


@router.patch(
    "/preferences",
    response_model=UserPreferencesRead,
    responses={409: {"description": "Layout or named-preset revision conflict"}},
)
async def update_preferences(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """锁定最新偏好快照并应用稀疏 PATCH 策略，保留工作区与命名预设冲突语义。"""
    identity = IdentityCommandService(db)
    user = await identity.lock_active_account(user.id, nowait=False)
    try:
        merged, response = patch_preferences(user.preferences or {}, payload)
    except PreferenceValidationError as error:
        raise RequestValidationError(
            [
                {
                    "type": item["type"],
                    "loc": ("body", *item["loc"]),
                    "msg": item["msg"],
                }
                for item in error.errors
            ]
        ) from None
    except PreferencePolicyError as error:
        raise HTTPException(
            status_code=error.status_code, detail=error.detail
        ) from None

    response = JSONResponse(response)
    identity.update_preferences_for_locked_user(user, merged)
    await db.commit()
    return response


@router.delete("/deactivation-request", response_model=UserOut)
async def cancel_self_deactivation(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """v0.8.1 · 冷静期内撤销自助注销申请。"""
    await DeactivationService.cancel(db, user=user, request=request)
    await db.commit()
    await db.refresh(user)
    return user


# Workbench task-event ingestion
from app.config import settings  # noqa: E402
from app.schemas.task_event import (  # noqa: E402
    TaskEventBatchIn,
    TaskEventBatchOut,
    TaskEventDiscarded,
)
from app.services.task_event_ingestion import (  # noqa: E402
    TaskEventRejection,
    insert_task_events,
    task_event_rejection_reason,
    validate_api_events,
)


def _enqueue_task_events(payload_list: list[dict]) -> bool:
    """投递到 Celery；broker 不可用时返回 False 让上层 fallback。"""
    try:
        from app.workers.task_events import persist_task_events_batch

        persist_task_events_batch.delay(payload_list)
        return True
    except Exception:  # pragma: no cover - defensive
        return False


@router.post("/task-events:batch", response_model=TaskEventBatchOut)
async def submit_task_events(
    payload: TaskEventBatchIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Accept only closed intervals for tasks the current account can access.

    The task relation is authoritative for project attribution.  The validated
    rows are also rechecked by the worker after an async handoff.
    """

    rows, rejected = await validate_api_events(db, user=user, events=payload.events)
    # Preserve the single-event HTTP contract for direct callers. The browser
    # collector drops this one permanently invalid event; mixed batches use the
    # per-row response below so valid events continue through the same request.
    if len(payload.events) == 1 and rejected:
        raise rejected[0].error
    payload_list = [
        {
            **row,
            "id": str(row["id"]),
            "task_id": str(row["task_id"]),
            "user_id": str(row["user_id"]),
            "project_id": str(row["project_id"]),
            "started_at": row["started_at"].isoformat(),
            "ended_at": row["ended_at"].isoformat(),
        }
        for row in rows
    ]

    queued = False
    if settings.task_events_async:
        queued = _enqueue_task_events(payload_list)

    if not queued:
        result = await insert_task_events(db, rows)
        if result.conflicts:
            rejected_indexes = {item.index for item in rejected}
            for index, event in enumerate(payload.events):
                if (
                    event.client_id in result.conflicts
                    and index not in rejected_indexes
                ):
                    rejected.append(
                        TaskEventRejection(
                            index=index,
                            client_id=event.client_id,
                            error=HTTPException(
                                status_code=409,
                                detail={"reason": "duplicate_client_event_conflict"},
                            ),
                        )
                    )
            rows = [row for row in rows if row["id"] not in result.conflicts]
            if len(payload.events) == 1:
                raise rejected[0].error

    return TaskEventBatchOut(
        accepted=len(rows),
        queued_async=queued,
        discarded=[
            TaskEventDiscarded(
                index=item.index,
                client_id=item.client_id,
                reason=task_event_rejection_reason(item.error),
            )
            for item in rejected
        ],
    )
