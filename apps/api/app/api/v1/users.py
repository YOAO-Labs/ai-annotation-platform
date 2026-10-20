import csv
import io
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.core.ratelimit import limiter
from app.deps import bearer_scheme, get_db, require_roles
from app.db.models.user import User
from app.db.enums import PLATFORM_ROLES, UserRole
from app.schemas.user import (
    OffboardingCommitRequest,
    OffboardingPreview,
    OffboardingResult,
    ReactivateRequest,
    UserOut,
)
from app.schemas.invitation import InvitationCreate, InvitationCreated
from app.schemas.management import (
    BulkGroupAssignmentPreview,
    BulkGroupAssignmentRequest,
    BulkGroupAssignmentResponse,
    BulkInviteRequest,
    BulkInviteResponse,
    RoleImpactPreview,
    UserPage,
)
from app.services.csv_export import csv_literal
from app.services.management import (
    role_impact_preview,
)
from app.api.v1.admin_password_commands import (
    execute_admin_password_reset,
    get_admin_password_commands,
)
from app.services.identity_admin_password_commands import AdminPasswordCommandsPort
from app.api.v1.directory_commands import execute_directory, get_directory_commands
from app.services.identity_directory_commands import DirectoryCommandsPort
from app.api.v1.group_commands import execute_group, get_group_commands
from app.api.v1.invitation_commands import (
    execute_invitation,
    get_invitation_manager_commands,
)
from app.services.identity_invitation_commands import InvitationCommandsPort
from app.services.identity_group_commands import GroupCommandsPort
from app.application.legacy_user_lifecycle import (
    change_user_role as change_user_role_workflow,
    deactivate_user as deactivate_user_workflow,
    delete_user as delete_user_workflow,
)
from app.application.user_lifecycle import UserLifecycleWorkflow
from sqlalchemy.exc import DBAPIError

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


class UsersStats(BaseModel):
    total: int
    online: int
    weekly_active: int


def _directory_filters(status_filter, project_id, group_id, role, search):
    return {
        "status": status_filter,
        "project_id": str(project_id) if project_id is not None else None,
        "group_id": str(group_id) if group_id is not None else None,
        "role": role,
        "search": search,
    }


@router.get("/stats", response_model=UsersStats)
async def users_stats(
    request: Request,
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DirectoryCommandsPort = Depends(get_directory_commands),
):
    """用户目录筛选统计，按最近七天活动时间聚合。"""
    return await execute_directory(
        request,
        credentials,
        commands,
        "user_stats",
        _directory_filters(status_filter, project_id, group_id, role, search),
    )


@router.get("/query", response_model=UserPage)
async def query_users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DirectoryCommandsPort = Depends(get_directory_commands),
):
    return await execute_directory(
        request,
        credentials,
        commands,
        "query_users",
        {
            **_directory_filters(status_filter, project_id, group_id, role, search),
            "page": page,
            "page_size": page_size,
        },
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    request: Request,
    role: str | None = None,
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(
        None, description="可选项目过滤；project_admin 入参被忽略，强制限定到其管理项目"
    ),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DirectoryCommandsPort = Depends(get_directory_commands),
):
    """用户列表。
    - super_admin：默认全量；可选 `project_id` 过滤到该项目成员。
    - project_admin：
      * `role=employee`：放开限制返回全量候选（用于指派 modal 选人）。
      * 其他场景：限定到 `Project.owner_id == actor.id` 的项目成员（actor 自身始终可见）。
    """
    data = await execute_directory(
        request,
        credentials,
        commands,
        "list_users",
        {
            "role": role,
            "status": status_filter,
            "project_id": str(project_id) if project_id is not None else None,
        },
    )
    return data["users"]


_ExportFormat = Literal["csv", "json"]


@router.get("/export")
async def export_users(
    request: Request,
    format: _ExportFormat = Query("csv"),
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DirectoryCommandsPort = Depends(get_directory_commands),
):
    data = await execute_directory(
        request,
        credentials,
        commands,
        "export_users",
        {
            **_directory_filters(status_filter, project_id, group_id, role, search),
            "format": format,
        },
    )
    rows = [
        {
            **row,
            "id": str(row["id"]),
            "group_id": str(row["group_id"]) if row["group_id"] else None,
            "created_at": row["created_at"].isoformat(),
        }
        for row in data["users"]
    ]
    exported_at = data["exported_at"]
    ts = exported_at.strftime("%Y%m%d_%H%M%S")
    if format == "json":
        body = json.dumps(
            {
                "_export_meta": {
                    "exported_by": data["exported_by"],
                    "exported_at": exported_at.isoformat(),
                    "request_id": data["request_id"],
                    "count": len(rows),
                },
                "users": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        media_type = "application/json; charset=utf-8"
    else:
        buf = io.StringIO()
        buf.write("\ufeff")
        buf.write(
            f"# Exported by: {data['exported_by'] or 'anonymous'}\n"
            f"# Exported at: {exported_at.isoformat()}\n"
            f"# Request ID: {data['request_id'] or ''}\n"
        )
        writer = csv.writer(buf)
        writer.writerow(
            [
                "id",
                "email",
                "name",
                "role",
                "group_name",
                "group_id",
                "status",
                "created_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    csv_literal(row["email"]),
                    csv_literal(row["name"]),
                    row["role"],
                    csv_literal(row["group_name"] or ""),
                    row["group_id"] or "",
                    row["status"],
                    row["created_at"],
                ]
            )
        body = buf.getvalue()
        media_type = "text/csv; charset=utf-8"
    return StreamingResponse(
        iter([body]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="users_{ts}.{format}"'},
    )


@router.post(
    "/invite",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
async def invite_user(
    payload: InvitationCreate,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    return await execute_invitation(
        request,
        credentials,
        commands,
        "create_invitation",
        payload.model_dump(mode="json"),
    )


@router.post("/bulk-invite/preview", response_model=BulkInviteResponse)
async def preview_bulk_invites(
    payload: BulkInviteRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    return await execute_invitation(
        request,
        credentials,
        commands,
        "preview_invitation_batch",
        payload.model_dump(mode="json"),
    )


@router.post("/bulk-invite", response_model=BulkInviteResponse)
async def apply_bulk_invites(
    payload: BulkInviteRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    return await execute_invitation(
        request,
        credentials,
        commands,
        "apply_invitation_batch",
        payload.model_dump(mode="json"),
    )


@router.post("/groups/bulk/preview", response_model=BulkGroupAssignmentPreview)
async def preview_bulk_group_assignment(
    payload: BulkGroupAssignmentRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    return await execute_group(
        request,
        credentials,
        commands,
        "preview_group_assignments",
        payload.model_dump(mode="json"),
    )


@router.post("/groups/bulk", response_model=BulkGroupAssignmentResponse)
async def apply_bulk_group_assignment(
    payload: BulkGroupAssignmentRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    return await execute_group(
        request,
        credentials,
        commands,
        "apply_group_assignments",
        payload.model_dump(mode="json"),
    )


class RoleChangePayload(BaseModel):
    role: str


@router.get("/{user_id}/role/preview", response_model=RoleImpactPreview)
async def preview_user_role_change(
    user_id: UUID,
    role: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Platform-role preview.  Super administrator only (plan AUTH-04)."""

    if role not in PLATFORM_ROLES:
        raise HTTPException(status_code=400, detail=f"非法平台角色: {role}")
    target = await db.get(User, user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    return await role_impact_preview(
        db,
        actor=actor,
        target=target,
        requested_role=role,
        manager_target_check=True,
        assignable_roles=PLATFORM_ROLES,
    )


@router.patch("/{user_id}/role", response_model=UserOut)
async def change_user_role(
    user_id: str,
    payload: RoleChangePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(UserRole.SUPER_ADMIN)),
):
    """Platform-role mutation.  Super administrator only (plan AUTH-04).

    A platform-role edit never cascades into any project's membership; project
    responsibility is changed through the project member role endpoints.
    """

    return await change_user_role_workflow(
        db, user_id=user_id, role=payload.role, actor=actor, request=request
    )


class AdminResetPasswordResponse(BaseModel):
    temp_password: str = Field(repr=False)
    message: str
    target_email: str


@router.post(
    "/{user_id}/admin-reset-password",
    response_model=AdminResetPasswordResponse,
    status_code=200,
)
@limiter.limit("3/minute")
async def admin_reset_password(
    user_id: UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: AdminPasswordCommandsPort = Depends(get_admin_password_commands),
):
    result = await execute_admin_password_reset(request, credentials, commands, user_id)

    return AdminResetPasswordResponse(
        temp_password=result.temp_password,
        message="请通过安全渠道告知用户首次登录后立即修改密码",
        target_email=result.target_email,
    )


class DeleteUserPayload(BaseModel):
    """可选转交目标 —— 当 target 仍持有未完成任务时必填。"""

    transfer_to_user_id: UUID | None = None


@router.delete("/{user_id}", response_model=UserOut)
async def delete_user(
    user_id: str,
    request: Request,
    payload: DeleteUserPayload | None = Body(None),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """软删除（is_active=False）。super_admin 可删任意（除自己/最后一名超管）；
    project_admin 可删启用的标注员/审核员，但目标的派生项目（成员、批次、
    任务、锁、名下项目）一旦出现在其他人的项目里（跨项目交接需上级处理）
    则拒绝；未分配账号无项目归属，可直接删除。

    若 target 仍持有未完成任务（assignee_id + status in pending/in_progress/review）或 task_lock，
    返回 409，要求传入 `transfer_to_user_id`；前端弹"先转交"二次 Modal。
    project_admin 的转交接收人必须是每个待转交任务所在项目的成员。
    """
    return await delete_user_workflow(
        db,
        user_id=user_id,
        actor=actor,
        request=request,
        transfer_to_user_id=payload.transfer_to_user_id if payload else None,
    )


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    return await deactivate_user_workflow(
        db, user_id=user_id, actor=actor, request=request
    )


@router.get(
    "/{user_id}/offboarding-preview",
    response_model=OffboardingPreview,
)
async def offboarding_preview(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """Return a complete, versioned handoff snapshot without mutating data."""

    return await UserLifecycleWorkflow.preview(db, target_id=user_id, actor=actor)


@router.post(
    "/{user_id}/offboarding",
    response_model=OffboardingResult,
)
async def offboarding_commit(
    user_id: UUID,
    payload: OffboardingCommitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """Atomically transfer current responsibilities and suspend the account."""

    try:
        result = await UserLifecycleWorkflow.offboard(
            db,
            target_id=user_id,
            actor=actor,
            payload=payload,
            request=request,
        )
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "offboarding_busy",
                    "message": "职责正在变化，请刷新预览后重试",
                },
            ) from exc
        raise
    return result


@router.post(
    "/{user_id}/reactivate",
    response_model=UserOut,
)
async def reactivate_user(
    user_id: UUID,
    request: Request,
    payload: ReactivateRequest | None = Body(None),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """Reactivate only an explicitly suspended account."""

    try:
        user = await UserLifecycleWorkflow.reactivate(
            db,
            target_id=user_id,
            actor=actor,
            reason=payload.reason if payload else None,
            request=request,
        )
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise HTTPException(
                status_code=409, detail="账号正在更新，请刷新后重试"
            ) from exc
        raise
    return user


class GroupAssignPayload(BaseModel):
    group_id: str | None = None


@router.patch("/{user_id}/group", response_model=UserOut)
async def assign_user_group(
    user_id: str,
    payload: GroupAssignPayload,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: GroupCommandsPort = Depends(get_group_commands),
):
    try:
        target_id = str(UUID(user_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="用户不存在") from None
    group_id = None
    if payload.group_id:
        try:
            group_id = str(UUID(payload.group_id))
        except ValueError:
            raise HTTPException(status_code=404, detail="数据组不存在") from None
    data = await execute_group(
        request,
        credentials,
        commands,
        "assign_user_group",
        {"user_id": target_id, "group_id": group_id},
    )
    return data["account"]
