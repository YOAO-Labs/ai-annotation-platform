"""Account administration workflows, preserving legacy disable/delete semantics."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.services.audit import AuditAction, AuditService
from app.services.batch import BatchService
from app.services.user_lifecycle import (
    UserLifecycleService,
    retire_account_credentials,
    set_disabled_metadata,
)

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
_PA_ASSIGNABLE_ROLES = {UserRole.EMPLOYEE.value}
_PENDING_TASK_STATUSES = ("pending", "in_progress", "review", "rejected")


async def project_admin_project_member(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    query = (
        select(func.count(ProjectMember.id))
        .join(Project, Project.id == ProjectMember.project_id)
        .where(
            Project.owner_id == actor.id,
            ProjectMember.user_id == target.id,
        )
    )
    return (await db.execute(query)).scalar_one() > 0


async def project_admin_manages_target(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    if target.is_active and target.role in _PA_ASSIGNABLE_ROLES:
        return True
    return await project_admin_project_member(db, actor=actor, target=target)


async def _target_only_in_actor_projects(
    db: AsyncSession, *, actor: User, target: User
) -> bool:
    project_ids = await UserLifecycleService.derived_project_ids(db, target.id)
    if not project_ids:
        return True
    owned_count = (
        await db.execute(
            select(func.count(Project.id)).where(
                Project.id.in_(sorted(project_ids)),
                Project.owner_id == actor.id,
            )
        )
    ).scalar_one()
    return owned_count == len(project_ids)


async def _count_pending_tasks(
    db: AsyncSession, *, target_id: UUID
) -> tuple[int, list[str]]:
    rows = (
        (
            await db.execute(
                select(Task.id)
                .where(
                    Task.assignee_id == target_id,
                    Task.status.in_(_PENDING_TASK_STATUSES),
                )
                .order_by(Task.created_at.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    count = (
        await db.execute(
            select(func.count(Task.id)).where(
                Task.assignee_id == target_id,
                Task.status.in_(_PENDING_TASK_STATUSES),
            )
        )
    ).scalar_one()
    return count, [str(task_id) for task_id in rows]


async def _count_task_locks(db: AsyncSession, *, target_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count(TaskLock.id)).where(TaskLock.user_id == target_id)
        )
    ).scalar_one()


async def _lock_legacy_lifecycle_target(
    db: AsyncSession,
    user_id: str,
    actor: User,
    receiver_id: UUID | None = None,
    *,
    protect_super_admin: bool = False,
) -> tuple[User, User]:
    try:
        target_id = UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="用户不存在") from exc
    if protect_super_admin:
        await UserLifecycleService.lock_super_admin_invariant(db)
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


async def _lock_lifecycle_projects(db: AsyncSession, target_id: UUID) -> None:
    project_ids = await UserLifecycleService.derived_project_ids(db, target_id)
    if not project_ids:
        return
    try:
        await db.execute(
            select(Project)
            .where(Project.id.in_(sorted(project_ids)))
            .order_by(Project.id)
            .with_for_update(read=True, key_share=True, nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) in {"55P03", "40001", "40P01"}:
            raise HTTPException(
                status_code=409, detail={"reason": "project_busy"}
            ) from exc
        raise


async def delete_user(
    db: AsyncSession,
    *,
    user_id: str,
    actor: User,
    request: Request,
    transfer_to_user_id: UUID | None,
) -> User:
    user, actor = await _lock_legacy_lifecycle_target(
        db, user_id, actor, transfer_to_user_id, protect_super_admin=True
    )
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if not user.is_active:
        return user
    await _lock_lifecycle_projects(db, user.id)

    if actor.role == UserRole.PROJECT_ADMIN.value:
        if user.role not in _PA_ASSIGNABLE_ROLES:
            raise HTTPException(status_code=403, detail="项目管理员仅能删除员工账号")
        if not await project_admin_manages_target(db, actor=actor, target=user):
            raise HTTPException(status_code=403, detail="该用户不在你管理的项目内")
        if not await _target_only_in_actor_projects(db, actor=actor, target=user):
            raise HTTPException(
                status_code=403,
                detail="该用户跨多个项目，须由超级管理员处理",
            )

    if user.role == UserRole.SUPER_ADMIN.value:
        if (await UserLifecycleService.super_admin_status(db, user.id))[
            "other_unreserved"
        ] == 0:
            raise HTTPException(status_code=400, detail="不能删除最后一名超级管理员")

    pending_count, sample_ids = await _count_pending_tasks(db, target_id=user.id)
    lock_count = await _count_task_locks(db, target_id=user.id)
    if (pending_count > 0 or lock_count > 0) and transfer_to_user_id is None:
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
    if transfer_to_user_id is not None:
        receiver = await db.get(User, transfer_to_user_id)
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
                covered_count = (
                    await db.execute(
                        select(
                            func.count(func.distinct(ProjectMember.project_id))
                        ).where(
                            ProjectMember.user_id == receiver.id,
                            ProjectMember.project_id.in_(sorted(affected_project_ids)),
                        )
                    )
                ).scalar_one()
                if covered_count < len(affected_project_ids):
                    raise HTTPException(
                        status_code=403,
                        detail="转交目标必须是待转交任务所在项目的成员",
                    )

        labeling = BatchService(db)
        transferred_count = await labeling.legacy_transfer_task_assignments(
            from_user_id=user.id,
            to_user_id=receiver.id,
            statuses=_PENDING_TASK_STATUSES,
        )
        await labeling.release_user_task_locks(user.id)

    set_disabled_metadata(
        user,
        kind="deleted",
        actor_id=actor.id,
        reason="管理员删除账号",
    )
    deletion_entry = await AuditService.log(
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
            "transferred_to": str(transfer_to_user_id) if transfer_to_user_id else None,
            "transferred_count": transferred_count,
            "released_locks": lock_count if transfer_to_user_id else 0,
        },
    )

    await AuditService.redact_actor_history(db, user.id, deletion_entry=deletion_entry)

    await db.commit()
    await db.refresh(user)
    return user


async def deactivate_user(
    db: AsyncSession, *, user_id: str, actor: User, request: Request
) -> User:
    user, actor = await _lock_legacy_lifecycle_target(
        db, user_id, actor, protect_super_admin=True
    )
    if user.id == actor.id:
        raise HTTPException(status_code=400, detail="不能停用自己")
    if not user.is_active:
        return user
    await _lock_lifecycle_projects(db, user.id)

    if actor.role == UserRole.PROJECT_ADMIN.value:
        if user.role not in _PA_ASSIGNABLE_ROLES:
            raise HTTPException(
                status_code=403, detail="项目管理员仅能停用标注员/审核员账号"
            )
        if not await project_admin_manages_target(db, actor=actor, target=user):
            raise HTTPException(status_code=403, detail="该用户不在你管理的项目内")
        if not await _target_only_in_actor_projects(db, actor=actor, target=user):
            raise HTTPException(
                status_code=403, detail="该用户跨多个项目，须由超级管理员处理"
            )

    if user.role == UserRole.SUPER_ADMIN.value:
        if (await UserLifecycleService.super_admin_status(db, user.id))[
            "other_unreserved"
        ] == 0:
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


def bulk_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("reason") or detail)
        return str(detail)
    if isinstance(exc, ValidationError):
        return "; ".join(error["msg"] for error in exc.errors())
    return "处理失败，请稍后重试"
