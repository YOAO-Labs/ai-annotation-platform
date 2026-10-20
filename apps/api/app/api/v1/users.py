import csv
import io
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from pydantic import BaseModel

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
    UserPageItem,
)
from app.services.csv_export import csv_literal
from app.services.management import (
    build_user_query,
    fetch_lifecycle_blocked_ids,
    fetch_managed_user_ids,
    fetch_user_page,
    role_impact_preview,
    validate_platform_role_filter,
    user_stats as management_user_stats,
)
from app.services.audit import (
    AuditService,
    export_detail,
    export_metadata_header,
)
from app.api.v1.group_commands import execute_group, get_group_commands
from app.api.v1.invitation_commands import (
    execute_invitation,
    get_invitation_manager_commands,
)
from app.services.identity_invitation_commands import InvitationCommandsPort
from app.services.identity_group_commands import GroupCommandsPort
from app.application.legacy_user_lifecycle import (
    admin_reset_password as admin_reset_password_workflow,
    change_user_role as change_user_role_workflow,
    deactivate_user as deactivate_user_workflow,
    delete_user as delete_user_workflow,
)
from app.application.user_lifecycle import UserLifecycleWorkflow
from sqlalchemy.exc import DBAPIError

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
_PA_ASSIGNABLE_ROLES = {UserRole.EMPLOYEE.value}


class UsersStats(BaseModel):
    total: int
    online: int
    weekly_active: int


@router.get("/stats", response_model=UsersStats)
async def users_stats(
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """v0.8.3 · UsersPage 顶部 4 卡之「本周活跃」与状态聚合。

    weekly_active 基于 last_seen_at >= now-7d，比旧的 status==online 更准确
    （旧逻辑只反映瞬时在线状态）。
    """
    stats = await management_user_stats(
        db,
        actor,
        project_id=project_id,
        group_id=group_id,
        role=role,
        status_filter=status_filter,
        search=search,
    )
    return UsersStats(
        total=stats.total,
        online=stats.online,
        weekly_active=stats.weekly_active,
    )


@router.get("/query", response_model=UserPage)
async def query_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    rows, total = await fetch_user_page(
        db,
        actor,
        page=page,
        page_size=page_size,
        project_id=project_id,
        group_id=group_id,
        role=role,
        status_filter=status_filter,
        search=search,
    )
    # Rows outside the project-bounded manage scope are still listed for
    # lookup; flag manageability per row so the UI can disable actions only
    # where writes would truly be denied. Manage scope = own-project members
    # plus every enabled annotator/reviewer (interim platform-wide arm);
    # enabled super admins stay visible but read-only. Lifecycle writes are
    # gated separately: they stay project-bounded to the actor's own
    # projects even for rows the wider arm makes manageable.
    managed_ids: set[UUID] | None = None
    lifecycle_blocked: set[UUID] = set()
    if actor.role == UserRole.PROJECT_ADMIN.value:
        managed_ids = await fetch_managed_user_ids(db, actor)
        lifecycle_blocked = await fetch_lifecycle_blocked_ids(
            db, actor=actor, user_ids=[user.id for user in rows]
        )

    def _page_item(user: User) -> UserPageItem:
        item = UserPageItem.model_validate(user)
        item.is_managed = (
            True
            if managed_ids is None or user.id == actor.id
            else (
                user.id in managed_ids
                or (user.is_active and user.role in _PA_ASSIGNABLE_ROLES)
            )
        )
        item.is_lifecycle_managed = (
            True
            if managed_ids is None
            else item.is_managed and user.id not in lifecycle_blocked
        )
        return item

    items = [_page_item(user) for user in rows]
    pages = (total + page_size - 1) // page_size if total else 0
    return UserPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    role: str | None = None,
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(
        None, description="可选项目过滤；project_admin 入参被忽略，强制限定到其管理项目"
    ),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """用户列表。
    - super_admin：默认全量；可选 `project_id` 过滤到该项目成员。
    - project_admin：
      * `role=employee`：放开限制返回全量候选（用于指派 modal 选人）。
      * 其他场景：限定到 `Project.owner_id == actor.id` 的项目成员（actor 自身始终可见）。
    """
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember

    # Legacy/unknown role filters are rejected, never silently empty.
    role = validate_platform_role_filter(role)

    q = select(User)
    if status_filter == "active":
        q = q.where(User.is_active.is_(True))
    elif status_filter == "inactive":
        q = q.where(User.is_active.is_(False))
    if role:
        q = q.where(User.role == role)

    if actor.role == UserRole.PROJECT_ADMIN.value:
        if role in _PA_ASSIGNABLE_ROLES and status_filter == "active":
            # 指派候选人场景：必须看到全量员工，否则永远没有可指派对象
            pass
        else:
            members_subq = (
                select(ProjectMember.user_id)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(Project.owner_id == actor.id)
            )
            q = q.where(or_(User.id.in_(members_subq), User.id == actor.id))
    elif project_id is not None:
        # super_admin 显式按项目过滤
        members_subq = select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id
        )
        q = q.where(User.id.in_(members_subq))

    result = await db.execute(q.order_by(User.created_at.desc()))
    return result.scalars().all()


_ExportFormat = Literal["csv", "json"]


@router.get("/export")
async def export_users(
    format: _ExportFormat = Query("csv"),
    status_filter: Literal["active", "inactive", "all"] = Query(
        "active", alias="status"
    ),
    project_id: UUID | None = Query(None),
    group_id: UUID | None = Query(None),
    role: str | None = Query(None),
    search: str | None = Query(None, max_length=255),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    rows = (
        (
            await db.execute(
                build_user_query(
                    actor,
                    project_id=project_id,
                    group_id=group_id,
                    role=role,
                    status_filter=status_filter,
                    search=search,
                ).order_by(User.created_at.desc(), User.id.desc())
            )
        )
        .scalars()
        .all()
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "json":
        payload_rows = [
            {
                "id": str(u.id),
                "email": u.email,
                "name": u.name,
                "role": u.role,
                "group_name": u.group_name,
                "group_id": str(u.group_id) if u.group_id else None,
                "status": u.status,
                "created_at": u.created_at.isoformat(),
            }
            for u in rows
        ]
        # v0.8.1 · 文件首部 _export_meta 字段（审计可追溯）
        from app.middleware.request_id import request_id_var

        wrapped = {
            "_export_meta": {
                "exported_by": actor.email,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id_var.get() or None,
                "count": len(rows),
            },
            "users": payload_rows,
        }
        body = json.dumps(wrapped, ensure_ascii=False, indent=2)
        await AuditService.log(
            db,
            actor=actor,
            action="user.export",
            target_type="user",
            target_id=None,
            request=request,
            status_code=200,
            detail=export_detail(
                actor=actor,
                request=request,
                base={"format": "json", "count": len(rows)},
            ),
        )
        await db.commit()
        return StreamingResponse(
            iter([body]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="users_{ts}.json"'},
        )

    # CSV
    buf = io.StringIO()
    # Excel UTF-8 BOM 兼容
    buf.write("﻿")
    # v0.8.1 · 首部审计 metadata 注释行（pandas/Excel 可 comment='#' 跳过）
    buf.write(export_metadata_header(actor=actor, fmt="csv", request=request))
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
    for u in rows:
        writer.writerow(
            [
                str(u.id),
                csv_literal(u.email),
                csv_literal(u.name),
                u.role,
                csv_literal(u.group_name or ""),
                str(u.group_id) if u.group_id else "",
                u.status,
                u.created_at.isoformat(),
            ]
        )
    body = buf.getvalue()

    await AuditService.log(
        db,
        actor=actor,
        action="user.export",
        target_type="user",
        target_id=None,
        request=request,
        status_code=200,
        detail=export_detail(
            actor=actor,
            request=request,
            base={"format": "csv", "count": len(rows)},
        ),
    )
    await db.commit()
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="users_{ts}.csv"'},
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


# —— v0.8.1 · 管理员重置低等级用户密码 ——


class AdminResetPasswordResponse(BaseModel):
    temp_password: str
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
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    target, temp_password = await admin_reset_password_workflow(
        db, user_id=user_id, actor=actor, request=request
    )

    return AdminResetPasswordResponse(
        temp_password=temp_password,
        message="请通过安全渠道告知用户首次登录后立即修改密码",
        target_email=target.email,
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
