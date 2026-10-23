import csv
import io
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from app.core.ratelimit import limiter
from app.deps import bearer_scheme
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
from app.api.v1.platform_role_commands import (
    execute_platform_role,
    get_platform_role_commands,
)
from app.services.identity_platform_role_commands import PlatformRoleCommandsPort
from app.api.v1.suspension_commands import execute_suspension, get_suspension_commands
from app.services.identity_suspension_commands import SuspensionCommandsPort
from app.api.v1.deletion_commands import execute_deletion, get_deletion_commands
from app.services.identity_deletion_commands import DeletionCommandsPort
from app.api.v1.offboarding_commands import (
    execute_offboarding,
    get_offboarding_commands,
)
from app.services.identity_offboarding_commands import OffboardingCommandsPort
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

router = APIRouter()


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
    request: Request,
    role: str = Query(..., min_length=1),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: PlatformRoleCommandsPort = Depends(get_platform_role_commands),
):
    """Preview a platform-role change with current project and work blockers."""
    return await execute_platform_role(
        request, credentials, commands, "preview_role_change", user_id, role
    )


@router.patch("/{user_id}/role", response_model=UserOut)
async def change_user_role(
    user_id: str,
    payload: RoleChangePayload,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: PlatformRoleCommandsPort = Depends(get_platform_role_commands),
):
    """Change a platform role while preserving existing project responsibilities.

    A platform-role edit never cascades into any project's membership; project
    responsibility is changed through the project member role endpoints.
    """

    data = await execute_platform_role(
        request, credentials, commands, "change_platform_role", user_id, payload.role
    )
    return data["user"]


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
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: DeletionCommandsPort = Depends(get_deletion_commands),
):
    """Soft-delete with current manager authority and the existing task handoff rules."""
    return await execute_deletion(
        request,
        credentials,
        commands,
        user_id,
        payload.transfer_to_user_id if payload else None,
    )


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: SuspensionCommandsPort = Depends(get_suspension_commands),
):
    """Suspend an account with current manager authority and retained reservations."""
    return await execute_suspension(
        request, credentials, commands, "suspend_account", user_id
    )


@router.get(
    "/{user_id}/offboarding-preview",
    response_model=OffboardingPreview,
)
async def offboarding_preview(
    user_id: UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: OffboardingCommandsPort = Depends(get_offboarding_commands),
):
    """Return a complete, versioned handoff snapshot without mutating data."""

    return await execute_offboarding(
        request, credentials, commands, "preview_offboarding", user_id
    )


@router.post(
    "/{user_id}/offboarding",
    response_model=OffboardingResult,
)
async def offboarding_commit(
    user_id: UUID,
    payload: OffboardingCommitRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: OffboardingCommandsPort = Depends(get_offboarding_commands),
):
    """Atomically transfer current responsibilities and suspend the account."""

    return await execute_offboarding(
        request,
        credentials,
        commands,
        "commit_offboarding",
        user_id,
        payload.model_dump(mode="json"),
    )


@router.post(
    "/{user_id}/reactivate",
    response_model=UserOut,
)
async def reactivate_user(
    user_id: UUID,
    request: Request,
    payload: ReactivateRequest | None = Body(None),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: SuspensionCommandsPort = Depends(get_suspension_commands),
):
    """Reactivate a suspended account and retire its previous credentials."""
    return await execute_suspension(
        request,
        credentials,
        commands,
        "reactivate_account",
        user_id,
        payload.reason if payload else None,
    )


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
