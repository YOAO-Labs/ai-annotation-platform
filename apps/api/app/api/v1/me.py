import asyncio
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from aap_identity.contract import Principal
from app.core.ratelimit import limiter
from app.deps import bearer_scheme, get_current_principal, get_current_user, get_db
from app.schemas.me import AvatarRefUpdate, PasswordChange, ProfileUpdate
from app.schemas.user import UserOut, UserPreferencesRead
from app.api.v1.account_commands import execute_account, get_account_commands
from app.api.v1.deactivation_commands import (
    execute_deactivation,
    get_deactivation_commands,
)
from app.services.identity_account_commands import AccountCommandsPort
from app.services.identity_deactivation_commands import DeactivationCommandsPort
from app.services import avatar as avatar_service
from app.services.avatar_image import (
    ALLOWED_CONTENT_TYPES as ALLOWED_IMAGE_CONTENT_TYPES,
    MAX_AVATAR_FILE_BYTES,
    AvatarImageError,
    normalize_avatar,
)
from app.services.identity_contracts import IdentityActorFacts

router = APIRouter()


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def heartbeat(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    """刷新在线心跳；既有调度任务按最后活跃时间执行离线判定。"""
    await execute_account(request, credentials, commands, "heartbeat", {})


class DeactivationRequest(BaseModel):
    """自助注销申请，可附原因。"""

    reason: str | None = Field(default=None, max_length=500)


@router.patch("", response_model=UserOut)
async def update_profile(
    payload: ProfileUpdate,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    data = await execute_account(
        request, credentials, commands, "update_name", {"name": payload.name}
    )
    return data["account"]


async def _set_avatar(
    request, credentials, commands, avatar_ref, *, kind, size=None, operation_id=None
):
    data = await execute_account(
        request,
        credentials,
        commands,
        "set_avatar_ref",
        {"avatar_ref": avatar_ref, "kind": kind, "bytes": size},
        operation_id=operation_id,
    )
    if data["changed"]:
        await asyncio.to_thread(
            avatar_service.delete_ref_object, data["previous_avatar_ref"]
        )
    return data["account"]


@router.post("/avatar", response_model=UserOut)
@limiter.limit("10/minute")
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    """上传自定义头像，服务端校验真实图片并规范化为 256×256 WebP。

    头像引用提交成功后才清理旧上传对象；提交结果未知时保留新字节以便核对。
    """
    declared = (file.content_type or "").lower()
    if declared and declared not in {
        "application/octet-stream",
        *ALLOWED_IMAGE_CONTENT_TYPES,
    }:
        raise HTTPException(status_code=415, detail="仅支持 PNG / JPEG / WebP 格式")
    raw = await file.read(MAX_AVATAR_FILE_BYTES + 1)
    try:
        normalized = await asyncio.to_thread(normalize_avatar, raw)
    except AvatarImageError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    # Reject a stale account before staging bytes, then recheck current credentials
    # at pointer commit. Remote transactions never span image processing or S3 IO.
    await execute_account(request, credentials, commands, "read_self_account", {})
    token = avatar_service.new_upload_token()
    avatar_ref = avatar_service.build_upload_ref(token)
    try:
        await asyncio.to_thread(avatar_service.write_avatar_object, token, normalized)
    except Exception:
        await asyncio.to_thread(avatar_service.delete_ref_object, avatar_ref)
        raise
    try:
        return await _set_avatar(
            request,
            credentials,
            commands,
            avatar_ref,
            kind="upload",
            size=len(normalized),
            operation_id=uuid.uuid4(),
        )
    except HTTPException as error:
        if error.status_code != 503:
            # A definite rejection has no committed pointer. An unavailable
            # owner's commit may be unknown: retain those bytes for reconciliation.
            await asyncio.to_thread(avatar_service.delete_ref_object, avatar_ref)
        raise


@router.patch("/avatar", response_model=UserOut)
async def set_avatar_ref(
    payload: AvatarRefUpdate,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    """选择内置头像或清除头像；上传引用只能由上传端点生成。"""
    try:
        avatar_ref = avatar_service.validate_client_ref(payload.avatar_ref)
    except avatar_service.InvalidAvatarRef as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return await _set_avatar(
        request, credentials, commands, avatar_ref, kind="preset_or_clear"
    )


@router.delete("/avatar", response_model=UserOut)
async def clear_avatar(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    """恢复默认头像，回退到姓名首字母。"""
    return await _set_avatar(
        request, credentials, commands, None, kind="preset_or_clear"
    )


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: PasswordChange,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
):
    await execute_account(
        request,
        credentials,
        commands,
        "change_password",
        {"old_password": payload.old_password, "new_password": payload.new_password},
    )


@router.post("/deactivation-request", response_model=UserOut)
async def request_self_deactivation(
    payload: DeactivationRequest,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DeactivationCommandsPort = Depends(get_deactivation_commands),
):
    """自助注销申请。七天冷静期，期间可撤销。"""
    return await execute_deactivation(
        request,
        credentials,
        commands,
        "request_self_deactivation",
        {"reason": payload.reason},
    )


@router.get("/preferences", response_model=UserPreferencesRead)
async def get_preferences(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
) -> JSONResponse:
    """读取当前账号偏好，未设置字段使用默认值，保留 opaque 工作区和快捷键。"""
    data = await execute_account(request, credentials, commands, "get_preferences", {})
    return JSONResponse(data["preferences"])


@router.patch(
    "/preferences",
    response_model=UserPreferencesRead,
    responses={409: {"description": "Layout or named-preset revision conflict"}},
)
async def update_preferences(
    payload: dict,
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AccountCommandsPort = Depends(get_account_commands),
) -> JSONResponse:
    """在身份所有者事务内合并稀疏偏好并校验版本冲突。"""
    data = await execute_account(
        request, credentials, commands, "patch_preferences", {"patch": payload}
    )
    return JSONResponse(data["preferences"])


@router.delete("/deactivation-request", response_model=UserOut)
async def cancel_self_deactivation(
    request: Request,
    _principal: Principal = Depends(get_current_principal),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DeactivationCommandsPort = Depends(get_deactivation_commands),
):
    """冷静期内撤销自助注销申请。"""
    return await execute_deactivation(
        request, credentials, commands, "cancel_self_deactivation", {}
    )


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
    """Send task events to the selected queue.

    Returning False triggers synchronous insertion.
    """
    try:
        if settings.jobs_queue_url:
            from app.services.jobs_queue import dispatch_job_sync

            dispatch_job_sync(
                task_name="app.workers.task_events.persist_task_events_batch",
                execution_id=str(uuid.uuid4()),
                args=[payload_list],
                options={"queue": "audit"},
            )
        else:
            from app.workers.task_events import persist_task_events_batch

            persist_task_events_batch.delay(payload_list)
        return True
    except Exception:  # pragma: no cover - defensive
        return False


@router.post("/task-events:batch", response_model=TaskEventBatchOut)
async def submit_task_events(
    payload: TaskEventBatchIn,
    db: AsyncSession = Depends(get_db),
    user: IdentityActorFacts = Depends(get_current_user),
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
