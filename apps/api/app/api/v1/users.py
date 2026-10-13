import csv
import io
import json
import secrets
import string
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, update, delete, text
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.core.ratelimit import limiter
from app.core.security import hash_password
from app.deps import get_db, require_roles
from app.db.models.user import User
from app.db.models.group import Group
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
    BulkGroupAssignmentResultItem,
    BulkInviteRequest,
    BulkInviteResponse,
    BulkInviteResultItem,
    GroupAssignmentPreviewItem,
    RoleImpactPreview,
    UserPage,
    UserPageItem,
)
from app.services.invitation import InvitationService, invitation_project_role
from app.services.csv_export import csv_literal
from app.services.management import (
    build_user_query,
    fetch_lifecycle_blocked_ids,
    fetch_managed_user_ids,
    fetch_user_page,
    platform_role_change_blockers,
    role_impact_preview,
    user_scope_clause,
    validate_platform_role_filter,
    user_stats as management_user_stats,
)
from app.services.audit import (
    AuditService,
    AuditAction,
    export_detail,
    export_metadata_header,
)
from app.services.system_settings_service import SystemSettingsService
from app.services.user_lifecycle import (
    UserLifecycleService,
    retire_account_credentials,
    set_disabled_metadata,
)
from sqlalchemy.exc import DBAPIError

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)

# Accounts a project administrator manages.  Post-cutover staff are employees;
# legacy global staff values are history only and are never eligible here.
_PA_ASSIGNABLE_ROLES = {UserRole.EMPLOYEE.value}


async def _count_active_super_admins(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count(User.id)).where(
                User.role == UserRole.SUPER_ADMIN.value,
                User.is_active.is_(True),
            )
        )
    ).scalar_one()


async def _project_admin_project_member(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    """严格项目边界：target 是否为 actor 所拥有项目的成员。

    不含「放行启用标注员/审核员」的平台级过渡分支；``_project_admin_manages_target``
    的项目边界分支由它实现。
    """
    from app.db.models.project import Project
    from app.db.models.project_member import ProjectMember

    q = (
        select(func.count(ProjectMember.id))
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            Project.owner_id == actor.id,
            ProjectMember.user_id == target.id,
        )
    )
    cnt = (await db.execute(q)).scalar_one()
    return cnt > 0


async def _project_admin_manages_target(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    """project_admin 能否管理 target。

    项目内 (project.owner_id == actor) 成员，以及所有「启用」的标注员/审核员
    账号（含未分配与其他项目的成员；标注/质检身份转为项目驱动前的过渡规则）。
    停用账号、其他项目管理员、观察员、超管均不可管理。
    """
    if target.is_active and target.role in _PA_ASSIGNABLE_ROLES:
        return True

    return await _project_admin_project_member(db, actor=actor, target=target)


async def _target_only_in_actor_projects(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    """target 派生项目是否全部落在 actor 拥有的项目内（生命周期写入门槛）。

    派生范围与 ``UserLifecycleService._project_ids_for_target`` 一致：
    拥有的项目 ∪ 成员关系 ∪ 批次分派 ∪ 任务/锁。只查成员表会漏掉
    「已被移出 ProjectMember、但仍持有他人项目任务/批次」的用户，导致
    停用/删除影响到项目管理员看不到的项目。
    """
    from app.db.models.project import Project

    project_ids = await UserLifecycleService.derived_project_ids(db, target.id)
    if not project_ids:
        # 未分配账号没有派生项目，可直接处理。
        return True

    owned_cnt = (
        await db.execute(
            select(func.count(Project.id)).where(
                Project.id.in_(sorted(project_ids)),
                Project.owner_id == actor.id,
            )
        )
    ).scalar_one()
    return owned_cnt == len(project_ids)


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
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    inv = await InvitationService.create(
        db,
        email=payload.email,
        role=payload.role,
        group_name=payload.group_name,
        project_id=payload.project_id,
        project_member_role=payload.project_member_role,
        actor=actor,
    )
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_INVITE,
        target_type="user",
        target_id=inv.email,
        request=request,
        status_code=201,
        detail={
            "role": inv.role,
            "group_name": inv.group_name,
            "project_id": str(inv.project_id) if inv.project_id else None,
        },
    )
    await db.commit()

    base_url = (
        await SystemSettingsService.get(db, "frontend_base_url")
        or settings.frontend_base_url
    )
    invite_url = f"{str(base_url).rstrip('/')}/register?token={inv.token}"
    project_name = None
    if inv.project_id:
        from app.db.models.project import Project

        project_name = await db.scalar(
            select(Project.name).where(Project.id == inv.project_id)
        )
    return InvitationCreated(
        invite_url=invite_url,
        token=inv.token,
        expires_at=inv.expires_at,
        project_id=inv.project_id,
        project_name=project_name,
        project_member_role=invitation_project_role(inv),
    )


def _bulk_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("reason") or detail)
        return str(detail)
    if isinstance(exc, ValidationError):
        return "; ".join(error["msg"] for error in exc.errors())
    return "处理失败，请稍后重试"


async def _bulk_invite(
    payload: BulkInviteRequest,
    *,
    preview: bool,
    request: Request,
    db: AsyncSession,
    actor: User,
) -> BulkInviteResponse:
    """Run each invitation in its own savepoint.

    Preview uses one enclosing savepoint so quota and duplicate checks see the
    earlier rows in the same request; the enclosing savepoint is rolled back at
    the end. Apply keeps successful item savepoints and commits once after all
    rows have been attempted, allowing failed rows to be retried alone.
    """

    from app.db.models.project import Project

    preview_tx = await db.begin_nested() if preview else None
    results: list[BulkInviteResultItem] = []
    seen_emails: set[str] = set()
    base_url = (
        await SystemSettingsService.get(db, "frontend_base_url")
        or settings.frontend_base_url
    )
    try:
        for index, raw_item in enumerate(payload.items):
            try:
                item = InvitationCreate.model_validate(raw_item.model_dump())
            except ValidationError as exc:
                results.append(
                    BulkInviteResultItem(
                        index=index,
                        email=raw_item.email,
                        ok=False,
                        retryable=True,
                        error=_bulk_error(exc),
                    )
                )
                continue
            if item.email in seen_emails:
                results.append(
                    BulkInviteResultItem(
                        index=index,
                        email=item.email,
                        ok=False,
                        retryable=True,
                        error="本批次中邮箱重复",
                    )
                )
                continue
            item_tx = await db.begin_nested()
            try:
                inv = await InvitationService.create(
                    db,
                    email=item.email,
                    role=item.role,
                    group_name=item.group_name,
                    project_id=item.project_id,
                    project_member_role=item.project_member_role,
                    actor=actor,
                )
                await AuditService.log(
                    db,
                    actor=actor,
                    action="user.bulk_invite",
                    target_type="invitation",
                    target_id=str(inv.id),
                    request=request,
                    status_code=200,
                    detail={
                        "email": inv.email,
                        "role": inv.role,
                        "project_id": str(inv.project_id) if inv.project_id else None,
                        "preview": preview,
                    },
                )
                project_name = None
                if inv.project_id:
                    project_name = await db.scalar(
                        select(Project.name).where(Project.id == inv.project_id)
                    )
                await item_tx.commit()
                seen_emails.add(item.email)
                results.append(
                    BulkInviteResultItem(
                        index=index,
                        email=item.email,
                        ok=True,
                        invitation_id=inv.id,
                        token=inv.token if not preview else None,
                        invite_url=(
                            f"{str(base_url).rstrip('/')}/register?token={inv.token}"
                            if not preview
                            else None
                        ),
                        project_id=inv.project_id,
                        project_name=project_name,
                    )
                )
            except Exception as exc:
                await item_tx.rollback()
                results.append(
                    BulkInviteResultItem(
                        index=index,
                        email=item.email,
                        ok=False,
                        retryable=True,
                        error=_bulk_error(exc),
                    )
                )
        if preview:
            await preview_tx.rollback()  # type: ignore[union-attr]
        else:
            await db.commit()
    except Exception:
        if preview_tx is not None and preview_tx.is_active:
            await preview_tx.rollback()
        raise

    return BulkInviteResponse(
        items=results,
        succeeded=sum(item.ok for item in results),
        failed=sum(not item.ok for item in results),
        preview=preview,
    )


@router.post("/bulk-invite/preview", response_model=BulkInviteResponse)
async def preview_bulk_invites(
    payload: BulkInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    return await _bulk_invite(
        payload, preview=True, request=request, db=db, actor=actor
    )


@router.post("/bulk-invite", response_model=BulkInviteResponse)
async def apply_bulk_invites(
    payload: BulkInviteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    return await _bulk_invite(
        payload, preview=False, request=request, db=db, actor=actor
    )


async def _group_assignment_item(
    db: AsyncSession,
    *,
    actor: User,
    user_id: UUID,
    group_id: UUID | None,
) -> GroupAssignmentPreviewItem:
    target = await db.scalar(
        select(User).where(User.id == user_id, user_scope_clause(actor))
    )
    if target is None:
        return GroupAssignmentPreviewItem(
            user_id=user_id,
            ok=False,
            error="用户不存在或不在管理范围内",
        )
    group_name = None
    if group_id is not None:
        group_name = await db.scalar(select(Group.name).where(Group.id == group_id))
        if group_name is None:
            return GroupAssignmentPreviewItem(
                user_id=user_id,
                email=target.email,
                name=target.name,
                ok=False,
                error="数据组不存在",
            )
    return GroupAssignmentPreviewItem(
        user_id=user_id,
        email=target.email,
        name=target.name,
        ok=True,
        current_group_id=target.group_id,
        current_group_name=target.group_name,
        next_group_id=group_id,
        next_group_name=group_name,
    )


@router.post("/groups/bulk/preview", response_model=BulkGroupAssignmentPreview)
async def preview_bulk_group_assignment(
    payload: BulkGroupAssignmentRequest,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    items = [
        await _group_assignment_item(
            db, actor=actor, user_id=user_id, group_id=payload.group_id
        )
        for user_id in payload.user_ids
    ]
    return BulkGroupAssignmentPreview(
        group_id=payload.group_id,
        group_name=next((item.next_group_name for item in items if item.ok), None),
        items=items,
        applicable=sum(item.ok for item in items),
        blocked=sum(not item.ok for item in items),
    )


@router.post("/groups/bulk", response_model=BulkGroupAssignmentResponse)
async def apply_bulk_group_assignment(
    payload: BulkGroupAssignmentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    if (
        payload.group_id is not None
        and await db.scalar(select(Group.id).where(Group.id == payload.group_id))
        is None
    ):
        raise HTTPException(status_code=404, detail="数据组不存在")
    results: list[BulkGroupAssignmentResultItem] = []
    for user_id in payload.user_ids:
        item_tx = await db.begin_nested()
        try:
            target = await db.scalar(
                select(User)
                .where(User.id == user_id, user_scope_clause(actor))
                .with_for_update()
            )
            if target is None:
                raise HTTPException(
                    status_code=404, detail="用户不存在或不在管理范围内"
                )
            group = (
                await db.scalar(select(Group).where(Group.id == payload.group_id))
                if payload.group_id is not None
                else None
            )
            target.group_id = group.id if group else None
            target.group_name = group.name if group else None
            await AuditService.log(
                db,
                actor=actor,
                action="user.bulk_group_assign",
                target_type="user",
                target_id=str(target.id),
                request=request,
                status_code=200,
                detail={
                    "group_id": str(group.id) if group else None,
                    "group_name": group.name if group else None,
                },
            )
            await item_tx.commit()
            results.append(BulkGroupAssignmentResultItem(user_id=user_id, ok=True))
        except Exception as exc:
            await item_tx.rollback()
            results.append(
                BulkGroupAssignmentResultItem(
                    user_id=user_id,
                    ok=False,
                    retryable=True,
                    error=_bulk_error(exc),
                )
            )
    await db.commit()
    return BulkGroupAssignmentResponse(
        items=results,
        succeeded=sum(item.ok for item in results),
        failed=sum(not item.ok for item in results),
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

    if payload.role not in PLATFORM_ROLES:
        raise HTTPException(status_code=400, detail=f"非法平台角色: {payload.role}")

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")

    # Revalidate the actor and target under account locks.  Recompute the
    # current role and the no-op case only after the fresh reload so a stale
    # ORM identity cannot decide the mutation.
    from app.services.user_lifecycle import UserLifecycleService

    await UserLifecycleService.lock_accounts(db, [actor.id, user.id])
    fresh_actor = await db.get(User, actor.id, populate_existing=True)
    if (
        fresh_actor is None
        or not fresh_actor.is_active
        or fresh_actor.role != UserRole.SUPER_ADMIN.value
    ):
        raise HTTPException(status_code=403, detail="当前账号已无管理权限")
    fresh_user = await db.get(User, user.id, populate_existing=True)
    if fresh_user is None or not fresh_user.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    user = fresh_user

    old_role = user.role
    new_role = payload.role
    if old_role == new_role:
        return user

    # Serialize against ownership transfers: hold the target's owned project
    # rows so a concurrent transfer cannot interleave between the ownership
    # blocker count and the role write (accounts are already locked above, and
    # the transfer endpoint takes account -> project in the same order).
    from app.db.models.project import Project as ProjectModel

    await db.execute(
        select(ProjectModel.id)
        .where(ProjectModel.owner_id == user.id)
        .order_by(ProjectModel.id)
        .with_for_update()
    )

    blockers = await platform_role_change_blockers(db, target=user, new_role=new_role)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "platform_role_change_blocked",
                "blockers": blockers,
            },
        )

    # —— super_admin 兜底：最后一名 super_admin 不可被降级 ——
    if (
        old_role == UserRole.SUPER_ADMIN.value
        and new_role != UserRole.SUPER_ADMIN.value
    ):
        if await _count_active_super_admins(db) <= 1:
            raise HTTPException(status_code=400, detail="不能降级最后一名超级管理员")

    user.role = new_role
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_ROLE_CHANGE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"email": user.email, "old": old_role, "new": new_role},
    )
    await db.commit()
    await db.refresh(user)
    return user


# —— v0.8.1 · 管理员重置低等级用户密码 ——
_ROLE_LEVEL = {
    UserRole.SUPER_ADMIN.value: 0,
    UserRole.PROJECT_ADMIN.value: 1,
    UserRole.REVIEWER.value: 2,
    UserRole.EMPLOYEE.value: 3,
    UserRole.ANNOTATOR.value: 3,
    UserRole.VIEWER.value: 4,
}


def _generate_temp_password(length: int = 16) -> str:
    """生成 16 位强临时密码：≥1 大写 / 小写 / 数字 / 符号。"""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    # 限制符号集合，避免 shell 转义麻烦：!@#$%^&*?
    symbols = "!@#$%^&*?"
    pool = upper + lower + digits + symbols
    while True:
        pwd = "".join(secrets.choice(pool) for _ in range(length))
        if (
            any(c in upper for c in pwd)
            and any(c in lower for c in pwd)
            and any(c in digits for c in pwd)
            and any(c in symbols for c in pwd)
        ):
            return pwd


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
    target = await db.get(User, user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == actor.id:
        raise HTTPException(
            status_code=400, detail="不能给自己重置密码（请用「修改密码」）"
        )

    actor_lvl = _ROLE_LEVEL.get(actor.role, 99)
    target_lvl = _ROLE_LEVEL.get(target.role, 99)
    # 只能重置严格低等级（数值更大）的用户
    if target_lvl <= actor_lvl:
        raise HTTPException(status_code=403, detail="只能重置等级低于你的用户的密码")

    if actor.role == UserRole.PROJECT_ADMIN.value:
        # project_admin 可重置启用标注员/审核员（含未分配）的密码；同级与超管除外
        if target.role == UserRole.PROJECT_ADMIN.value:
            raise HTTPException(status_code=403, detail="项目管理员之间不可互重置")
        if not await _project_admin_manages_target(db, actor=actor, target=target):
            raise HTTPException(status_code=403, detail="该用户不在你管理的项目内")

    temp_password = _generate_temp_password()
    target.password_hash = hash_password(temp_password)
    target.password_admin_reset_at = datetime.now(timezone.utc)

    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_PASSWORD_ADMIN_RESET,
        target_type="user",
        target_id=str(target.id),
        request=request,
        status_code=200,
        # 注意：detail 不记录密码本身
        detail={"target_email": target.email, "target_role": target.role},
    )
    await db.commit()

    return AdminResetPasswordResponse(
        temp_password=temp_password,
        message="请通过安全渠道告知用户首次登录后立即修改密码",
        target_email=target.email,
    )


class DeleteUserPayload(BaseModel):
    """可选转交目标 —— 当 target 仍持有未完成任务时必填。"""

    transfer_to_user_id: UUID | None = None


_PENDING_TASK_STATUSES = (
    "pending",
    "in_progress",
    "review",
    "rejected",
)


async def _count_pending_tasks(
    db: AsyncSession, *, target_id: UUID
) -> tuple[int, list[str]]:
    """返回 (pending_count, sample_task_ids[5])。"""
    from app.db.models.task import Task

    q = (
        select(Task.id)
        .where(Task.assignee_id == target_id)
        .where(Task.status.in_(_PENDING_TASK_STATUSES))
        .order_by(Task.created_at.desc())
    )
    rows = (await db.execute(q.limit(5))).scalars().all()
    cnt = (
        await db.execute(
            select(func.count(Task.id))
            .where(Task.assignee_id == target_id)
            .where(Task.status.in_(_PENDING_TASK_STATUSES))
        )
    ).scalar_one()
    return cnt, [str(r) for r in rows]


async def _count_task_locks(db: AsyncSession, *, target_id: UUID) -> int:
    from app.db.models.task_lock import TaskLock

    return (
        await db.execute(
            select(func.count(TaskLock.id)).where(TaskLock.user_id == target_id)
        )
    ).scalar_one()


async def _lock_legacy_lifecycle_target(
    db: AsyncSession, user_id: str, actor: User, receiver_id: UUID | None = None
) -> tuple[User, User]:
    try:
        target_id = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    ids = [target_id, actor.id]
    if receiver_id:
        ids.append(receiver_id)
    accounts = await UserLifecycleService.lock_accounts(db, ids)
    fresh_actor = accounts.get(actor.id)
    if (
        fresh_actor is None
        or not fresh_actor.is_active
        or fresh_actor.role not in _MANAGERS
    ):
        raise HTTPException(status_code=403, detail="当前账号已无管理权限")
    target = accounts.get(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return target, fresh_actor


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
    from app.db.models.project_member import ProjectMember
    from app.db.models.task import Task
    from app.db.models.task_lock import TaskLock

    user, actor = await _lock_legacy_lifecycle_target(
        db, user_id, actor, payload.transfer_to_user_id if payload else None
    )
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if not user.is_active:
        return user

    if actor.role == UserRole.PROJECT_ADMIN.value:
        if user.role not in _PA_ASSIGNABLE_ROLES:
            raise HTTPException(status_code=403, detail="项目管理员仅能删除员工账号")
        if not await _project_admin_manages_target(db, actor=actor, target=user):
            raise HTTPException(status_code=403, detail="该用户不在你管理的项目内")
        if not await _target_only_in_actor_projects(db, actor=actor, target=user):
            raise HTTPException(
                status_code=403,
                detail="该用户跨多个项目，须由超级管理员处理",
            )

    if user.role == UserRole.SUPER_ADMIN.value:
        if await _count_active_super_admins(db) <= 1:
            raise HTTPException(status_code=400, detail="不能删除最后一名超级管理员")

    # —— 关联任务检查 + 可选转交 ——
    pending_count, sample_ids = await _count_pending_tasks(db, target_id=user.id)
    lock_count = await _count_task_locks(db, target_id=user.id)
    transfer_to: UUID | None = payload.transfer_to_user_id if payload else None

    if (pending_count > 0 or lock_count > 0) and transfer_to is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "has_pending_tasks",
                "pending_task_count": pending_count,
                "locked_task_count": lock_count,
                "sample_task_ids": sample_ids,
                "message": f"该用户当前有 {pending_count} 个未完成任务、{lock_count} 个锁定任务，需先转交",
            },
        )

    transferred_count = 0
    if transfer_to is not None:
        receiver = await db.get(User, transfer_to)
        if receiver is None or not receiver.is_active:
            raise HTTPException(status_code=400, detail="转交目标用户不存在或已禁用")
        if receiver.id == user.id:
            raise HTTPException(status_code=400, detail="转交目标不能与被删用户相同")
        if (
            receiver.role not in _PA_ASSIGNABLE_ROLES
            and receiver.role != UserRole.PROJECT_ADMIN.value
        ):
            raise HTTPException(status_code=400, detail="转交目标角色不合法")
        if actor.role == UserRole.PROJECT_ADMIN.value:
            # project_admin 的转交严格限定在自己项目内：接收人必须能访问每个
            # 持有待转交任务的项目；平台级管理分支不适用于转交接收人，
            # 否则任务会被转给对目标项目无任何访问权的人。
            affected_project_ids = set(
                (
                    await db.execute(
                        select(Task.project_id)
                        .where(
                            Task.assignee_id == user.id,
                            Task.status.in_(_PENDING_TASK_STATUSES),
                            Task.project_id.is_not(None),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            if affected_project_ids and receiver.id != actor.id:
                covered_cnt = (
                    await db.execute(
                        select(
                            func.count(func.distinct(ProjectMember.project_id))
                        ).where(
                            ProjectMember.user_id == receiver.id,
                            ProjectMember.project_id.in_(sorted(affected_project_ids)),
                        )
                    )
                ).scalar_one()
                if covered_cnt < len(affected_project_ids):
                    raise HTTPException(
                        status_code=403,
                        detail="转交目标必须是待转交任务所在项目的成员",
                    )

        # 转交未完成任务
        result = await db.execute(
            update(Task)
            .where(Task.assignee_id == user.id)
            .where(Task.status.in_(_PENDING_TASK_STATUSES))
            .values(assignee_id=receiver.id, assigned_at=func.now())
        )
        transferred_count = result.rowcount or 0
        # 清除原 user 持有的所有 task_lock（释放锁，不转给 receiver）
        await db.execute(delete(TaskLock).where(TaskLock.user_id == user.id))

    set_disabled_metadata(
        user,
        kind="deleted",
        actor_id=actor.id,
        reason="管理员删除账号",
    )
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_DELETE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={
            "email": user.email,
            "role": user.role,
            "transferred_to": str(transfer_to) if transfer_to else None,
            "transferred_count": transferred_count,
            "released_locks": lock_count if transfer_to else 0,
        },
    )

    # v0.6.6 · GDPR：被删用户在 audit_logs 历史行中的 actor_email / actor_role 脱敏
    # 保留 actor_id（FK 仍指向软删后的用户行；用户行真正 DELETE 时 ON DELETE SET NULL 兜底）
    # v0.7.8: 审计不可�� trigger 豁免 — SET LOCAL 仅在当前事务内有效
    from app.db.models.audit_log import AuditLog

    await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
    redact_result = await db.execute(
        update(AuditLog)
        .where(AuditLog.actor_id == user.id)
        .values(actor_email=None, actor_role=None)
    )
    redacted_rows = redact_result.rowcount or 0
    # 把脱敏行数追加到刚才的 USER_DELETE detail 里，方便审计
    if redacted_rows:
        last_audit = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.actor_id == actor.id)
                .where(AuditLog.action == AuditAction.USER_DELETE.value)
                .where(AuditLog.target_id == str(user.id))
                .order_by(AuditLog.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_audit is not None:
            detail = dict(last_audit.detail_json or {})
            detail["redacted_audit_rows"] = redacted_rows
            last_audit.detail_json = detail

    await db.commit()
    await db.refresh(user)
    return user


@router.post("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    user, actor = await _lock_legacy_lifecycle_target(db, user_id, actor)
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能停用自己")
    if not user.is_active:
        return user

    if actor.role == UserRole.PROJECT_ADMIN.value:
        if user.role not in _PA_ASSIGNABLE_ROLES:
            raise HTTPException(
                status_code=403, detail="项目管理员仅能停用标注员/审核员账号"
            )
        if not await _project_admin_manages_target(db, actor=actor, target=user):
            raise HTTPException(status_code=403, detail="该用户不在你管理的项目内")
        if not await _target_only_in_actor_projects(db, actor=actor, target=user):
            raise HTTPException(
                status_code=403, detail="该用户跨多个项目，须由超级管理员处理"
            )

    if user.role == UserRole.SUPER_ADMIN.value:
        if await _count_active_super_admins(db) <= 1:
            raise HTTPException(status_code=400, detail="不能停用最后一名超级管理员")

    set_disabled_metadata(
        user,
        kind="suspended",
        actor_id=actor.id,
        reason="管理员停用账号",
    )
    await retire_account_credentials(db, user.id)
    await AuditService.log(
        db,
        actor=actor,
        action=AuditAction.USER_DEACTIVATE,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={"email": user.email},
    )
    await db.commit()
    await db.refresh(user)
    return user


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

    return await UserLifecycleService.preview(db, target_id=user_id, actor=actor)


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
        result = await UserLifecycleService.offboard(
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
        user = await UserLifecycleService.reactivate(
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
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    from app.db.models.group import Group

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="用户不存在")

    new_group: Group | None = None
    if payload.group_id:
        new_group = await db.get(Group, payload.group_id)
        if new_group is None:
            raise HTTPException(status_code=404, detail="数据组不存在")

    old_group_id = user.group_id
    old_group_name = user.group_name
    user.group_id = new_group.id if new_group else None
    user.group_name = new_group.name if new_group else None

    await AuditService.log(
        db,
        actor=actor,
        action="user.group_change",
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=200,
        detail={
            "email": user.email,
            "old_group_id": str(old_group_id) if old_group_id else None,
            "old_group_name": old_group_name,
            "new_group_id": str(new_group.id) if new_group else None,
            "new_group_name": new_group.name if new_group else None,
        },
    )
    await db.commit()
    await db.refresh(user)
    return user
