from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hash_password
from app.db.enums import (
    LEGACY_PLATFORM_STAFF_ROLES,
    MANAGER_PLATFORM_ROLES,
    PLATFORM_ROLES,
    PROJECT_ROLES,
    PlatformRole,
    UserRole,
)
from app.db.models.group import Group
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.services.project_access import (
    assert_membership_role_compatible,
    membership_role_compatible,
)
from app.services.system_settings_service import SystemSettingsService


#: Account platform roles accepted as invitation input.  Legacy global staff
#: values are rejected here; only historical readers may surface them.
_ALLOWED_ROLES = PLATFORM_ROLES
_PROJECT_ADMIN_INVITABLE_ROLES = {
    PlatformRole.EMPLOYEE.value,
    PlatformRole.VIEWER.value,
}
_PRIVILEGED_ROLES = {
    PlatformRole.SUPER_ADMIN.value,
    PlatformRole.PROJECT_ADMIN.value,
}
_PROJECT_MEMBER_ROLES = PROJECT_ROLES
_MANAGER_ROLES = MANAGER_PLATFORM_ROLES
_ROLE_LABELS = {
    PlatformRole.SUPER_ADMIN.value: "超级管理员",
    PlatformRole.PROJECT_ADMIN.value: "项目管理员",
    PlatformRole.EMPLOYEE.value: "员工",
    UserRole.REVIEWER.value: "审核员",
    UserRole.ANNOTATOR.value: "标注员",
    PlatformRole.VIEWER.value: "观察者",
}


def invitation_project_role(invitation: UserInvitation) -> str | None:
    """Project role of an invitation, with a legacy read-only fallback.

    New invitations always store ``project_role``.  Unbackfilled historical
    project invitations stored the project role in ``role``; migration 0174
    backfills them, but readers stay tolerant until then.
    """

    if invitation.project_id is None:
        return None
    if invitation.project_role is not None:
        return invitation.project_role
    if invitation.role in _PROJECT_MEMBER_ROLES:
        return invitation.role
    return None


async def _lock_invitation_email(db: AsyncSession, email: str) -> None:
    """Serialize account creation and invitation replacement for one email."""

    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(email, 0)))
    )


async def _resolve_or_create_group(db: AsyncSession, name: str) -> Group:
    query = select(Group).where(Group.name == name).with_for_update(read=True)
    group = await db.scalar(query)
    if group is not None:
        return group

    await db.execute(
        pg_insert(Group)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=[Group.name])
    )
    group = await db.scalar(query)
    if group is None:  # pragma: no cover - INSERT 后的行在同事务内应始终可见
        raise RuntimeError("failed to resolve invitation group")
    return group


async def _get_manageable_invitation(
    db: AsyncSession, invitation_id: uuid.UUID, actor: User
) -> UserInvitation:
    query = select(UserInvitation).where(UserInvitation.id == invitation_id)
    if actor.role != UserRole.SUPER_ADMIN.value:
        query = query.where(UserInvitation.invited_by == actor.id)
    invitation = await db.scalar(query.with_for_update())
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请不存在")
    return invitation


async def _lock_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def _project_for_invitation(
    db: AsyncSession, invitation: UserInvitation, *, for_update: bool = False
) -> Project | None:
    if invitation.project_id is None:
        return None
    query = select(Project).where(Project.id == invitation.project_id)
    if for_update:
        query = query.with_for_update()
    project = await db.scalar(query.execution_options(populate_existing=True))
    if project is None:
        raise HTTPException(status_code=410, detail="目标项目已删除，无法接受邀请")
    return project


async def _validate_inviter_scope(
    db: AsyncSession,
    invitation: UserInvitation,
    project: Project | None,
    *,
    inviter: User | None = None,
) -> User:
    """Re-check issuer and project authority on every resolve/accept path."""

    if inviter is None:
        inviter = await db.scalar(
            select(User)
            .where(User.id == invitation.invited_by)
            .execution_options(populate_existing=True)
        )
    if inviter is None or not inviter.is_active or inviter.role not in _MANAGER_ROLES:
        raise HTTPException(status_code=410, detail="邀请人已停用或不再具备管理权限")

    # Legacy privileged invitations have always required a live super admin.
    if (
        invitation.role in _PRIVILEGED_ROLES
        and inviter.role != UserRole.SUPER_ADMIN.value
    ):
        raise HTTPException(status_code=410, detail="该邀请权限已失效")

    if project is not None:
        owner = await db.scalar(select(User).where(User.id == project.owner_id))
        if owner is None or not owner.is_active or owner.role not in _MANAGER_ROLES:
            raise HTTPException(status_code=410, detail="目标项目当前没有可用的负责人")
        if (
            inviter.role == UserRole.PROJECT_ADMIN.value
            and project.owner_id != inviter.id
        ):
            raise HTTPException(status_code=410, detail="邀请人已不再管理目标项目")
    return inviter


def _assert_project_role_compatible(project_role: str | None) -> None:
    if project_role not in _PROJECT_MEMBER_ROLES:
        raise HTTPException(
            status_code=400,
            detail="目标项目只能分配标注员、审核员或观察者角色；超级管理员和项目管理员是全局角色",
        )


def _assert_existing_role_compatible(user: User, project_role: str) -> None:
    """An existing account may accept a project invitation only when its
    platform role is compatible with the project responsibility.

    Employee accepts any project role; a platform viewer may only accept a
    viewer membership.  This never mutates the account's platform role.
    """

    if membership_role_compatible(user.role, project_role):
        return
    current = _ROLE_LABELS.get(user.role, user.role)
    invited = _ROLE_LABELS.get(project_role, project_role)
    raise HTTPException(
        status_code=409,
        detail=(
            f"当前账号是{current}，无法持有{invited}项目职责。为避免静默修改平台角色，"
            "请联系管理员调整账号角色后再接受邀请"
        ),
    )


async def _add_project_membership(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    assigned_by: uuid.UUID,
) -> None:
    """Insert membership atomically and turn a concurrent duplicate into 409."""

    result = await db.execute(
        pg_insert(ProjectMember)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            user_id=user_id,
            role=role,
            assigned_by=assigned_by,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
        .returning(ProjectMember.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=409, detail="该账号已是目标项目成员")


async def _acceptance_summary(
    db: AsyncSession,
    *,
    user: User,
    project: Project | None,
    member_role: str | None,
) -> dict[str, object]:
    if project is None:
        return {
            "project_id": None,
            "project_name": None,
            "project_member_role": None,
            "next_action": "wait_for_allocation",
            "next_action_label": "等待管理员分配项目",
            "responsible_person_name": None,
            "active_batch_count": 0,
        }

    owner = await db.scalar(select(User).where(User.id == project.owner_id))
    active_batch_count = 0
    if member_role == UserRole.ANNOTATOR.value:
        active_batch_count = int(
            await db.scalar(
                select(func.count(TaskBatch.id)).where(
                    TaskBatch.project_id == project.id,
                    TaskBatch.status.in_(["active", "annotating", "rejected"]),
                    TaskBatch.annotator_id == user.id,
                )
            )
            or 0
        )
    elif member_role == UserRole.REVIEWER.value:
        active_batch_count = int(
            await db.scalar(
                select(func.count(TaskBatch.id)).where(
                    TaskBatch.project_id == project.id,
                    TaskBatch.status.in_(["active", "annotating", "reviewing"]),
                    TaskBatch.reviewer_id == user.id,
                )
            )
            or 0
        )

    has_work = active_batch_count > 0
    return {
        "project_id": project.id,
        "project_name": project.name,
        "project_member_role": member_role,
        "next_action": "start_work" if has_work else "wait_for_allocation",
        "next_action_label": "开始工作" if has_work else "等待分派",
        "responsible_person_name": owner.name if owner else None,
        "active_batch_count": active_batch_count,
    }


class InvitationService:
    @staticmethod
    async def check_daily_limit(db: AsyncSession, actor_id: uuid.UUID) -> None:
        # Lock the inviter row so concurrent API workers cannot all pass the
        # count check before their invitation rows become visible.
        await db.scalar(select(User.id).where(User.id == actor_id).with_for_update())
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await db.execute(
            select(func.count())
            .select_from(UserInvitation)
            .where(
                UserInvitation.invited_by == actor_id,
                UserInvitation.created_at >= since,
            )
        )
        count = result.scalar_one()
        limit = await SystemSettingsService.get(db, "max_invitations_per_day")
        if type(limit) is not int:
            raise ValueError("invalid invitation quota")
        # A zero/invalid deployment value is fail-closed.  System settings
        # validation normally prevents it, but a bad historical override must
        # never turn the quota into an unlimited path.
        if count >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"每位管理员滚动 24 小时最多邀请 {limit} 人，请稍后再试",
            )

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        email: str,
        role: str,
        group_name: str | None,
        project_id: uuid.UUID | None = None,
        project_member_role: str | None = None,
        actor: User,
    ) -> UserInvitation:
        locked_actor = await _lock_user(db, actor.id)
        if (
            locked_actor is None
            or not locked_actor.is_active
            or locked_actor.role not in _MANAGER_ROLES
        ):
            raise HTTPException(status_code=403, detail="当前账号不再具备邀请权限")
        actor = locked_actor
        # Platform role and project role are independent inputs.  Legacy global
        # staff values are rejected; staff invitations use ``employee`` plus an
        # explicit ``project_member_role`` for a project target.
        platform_role = role
        project_role = project_member_role
        if platform_role not in _ALLOWED_ROLES:
            raise HTTPException(status_code=400, detail=f"非法角色: {role}")
        if project_id is not None:
            _assert_project_role_compatible(project_role)
            assert_membership_role_compatible(platform_role, project_role)
        elif project_role is not None:
            raise HTTPException(status_code=400, detail="账号级邀请不得包含项目职责")

        if (
            actor.role != UserRole.SUPER_ADMIN.value
            and platform_role not in _PROJECT_ADMIN_INVITABLE_ROLES
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="项目管理员仅能邀请员工或观察者",
            )

        # Creation and acceptance both lock issuer -> email -> project ->
        # invitation, so replacement cannot deadlock with target acceptance.
        await InvitationService.check_daily_limit(db, actor.id)
        await _lock_invitation_email(db, email)

        project: Project | None = None
        if project_id is not None:
            project = await db.scalar(
                select(Project)
                .where(Project.id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if project is None:
                raise HTTPException(status_code=404, detail="目标项目不存在")
            await _validate_inviter_scope(
                db,
                UserInvitation(invited_by=actor.id, role=platform_role),
                project,
                inviter=actor,
            )

        # Existing accounts explicitly accept a project invitation after login.
        # Legacy account invitations must still reject an already used email.
        active = await db.execute(
            select(User).where(User.email == email, User.is_active.is_(True))
        )
        existing = active.scalar_one_or_none()
        if existing is not None and project is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"邮箱 {email} 已注册，请选择目标项目后邀请该用户加入",
            )
        if existing is not None:
            _assert_existing_role_compatible(existing, project_role)
            member = await db.scalar(
                select(ProjectMember.id).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == existing.id,
                )
            )
            if member is not None:
                raise HTTPException(status_code=409, detail="该账号已是目标项目成员")

        # 作废同 email 仍 pending 的旧邀请（accepted_at IS NULL）
        now = datetime.now(timezone.utc)
        expire_old = update(UserInvitation).where(
            UserInvitation.email == email,
            UserInvitation.accepted_at.is_(None),
        )
        if actor.role != UserRole.SUPER_ADMIN.value:
            expire_old = expire_old.where(UserInvitation.invited_by == actor.id)
        await db.execute(expire_old.values(expires_at=now))

        token = secrets.token_urlsafe(32)
        ttl_raw = await SystemSettingsService.get(db, "invitation_ttl_days")
        try:
            ttl_days = int(settings.invitation_ttl_days if ttl_raw is None else ttl_raw)
        except (TypeError, ValueError):
            ttl_days = int(settings.invitation_ttl_days)
        inv = UserInvitation(
            email=email,
            role=platform_role,
            project_role=project_role,
            group_name=group_name,
            project_id=project_id,
            token=token,
            expires_at=now + timedelta(days=ttl_days),
            invited_by=actor.id,
        )
        db.add(inv)
        await db.flush()
        return inv

    @staticmethod
    async def resolve(
        db: AsyncSession, token: str, *, for_update: bool = False
    ) -> UserInvitation:
        query = select(UserInvitation).where(UserInvitation.token == token)
        if for_update:
            query = query.with_for_update()
        result = await db.execute(query.execution_options(populate_existing=True))
        inv = result.scalar_one_or_none()
        if inv is None:
            raise HTTPException(status_code=404, detail="邀请链接无效")
        if inv.accepted_at is not None:
            raise HTTPException(status_code=410, detail="该邀请已被使用")
        if inv.revoked_at is not None:
            raise HTTPException(status_code=410, detail="该邀请已撤销")
        if inv.expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="该邀请已过期")
        project = await _project_for_invitation(db, inv, for_update=False)
        await _validate_inviter_scope(db, inv, project)
        if project is not None:
            _assert_project_role_compatible(invitation_project_role(inv))
        return inv

    @staticmethod
    async def revoke(
        db: AsyncSession, invitation_id: uuid.UUID, *, actor: User
    ) -> UserInvitation:
        inv = await _get_manageable_invitation(db, invitation_id, actor)
        if inv.accepted_at is not None:
            raise HTTPException(status_code=400, detail="该邀请已被接受，无法撤销")
        if inv.revoked_at is not None:
            return inv
        now = datetime.now(timezone.utc)
        inv.revoked_at = now
        inv.expires_at = now
        await db.flush()
        return inv

    @staticmethod
    async def resend(
        db: AsyncSession, invitation_id: uuid.UUID, *, actor: User
    ) -> UserInvitation:
        inv = await _get_manageable_invitation(db, invitation_id, actor)
        if (
            actor.role == UserRole.PROJECT_ADMIN.value
            and inv.role not in _PROJECT_ADMIN_INVITABLE_ROLES
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="项目管理员不能重发高权限邀请",
            )
        if inv.accepted_at is not None:
            raise HTTPException(status_code=400, detail="该邀请已被接受，无法重发")
        # Resending revives a historical row (expired/revoked invitations are
        # excluded from migration 0174's conversion, and this call clears
        # revoked_at and extends expires_at).  Normalize pre-cutover staff
        # roles exactly the way 0174 normalizes pending invitations, so
        # acceptance can never copy a rejected legacy value into users.role.
        if inv.role in LEGACY_PLATFORM_STAFF_ROLES:
            if inv.project_id is not None and inv.project_role is None:
                inv.project_role = inv.role
            inv.role = PlatformRole.EMPLOYEE.value
        inv.token = secrets.token_urlsafe(32)
        ttl_raw = await SystemSettingsService.get(db, "invitation_ttl_days")
        try:
            ttl_days = int(settings.invitation_ttl_days if ttl_raw is None else ttl_raw)
        except (TypeError, ValueError):
            ttl_days = int(settings.invitation_ttl_days)
        inv.expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
        inv.revoked_at = None
        await db.flush()
        return inv

    @staticmethod
    async def accept(
        db: AsyncSession,
        *,
        token: str,
        name: str,
        password: str,
    ) -> tuple[User, UserInvitation, dict[str, object]]:
        # First resolve gives a useful public error before taking locks.  The
        # second resolve is the authoritative, row-locked check.
        initial = await InvitationService.resolve(db, token)
        await _lock_user(db, initial.invited_by)
        await _lock_invitation_email(db, initial.email)
        await _project_for_invitation(db, initial, for_update=True)
        inv = await InvitationService.resolve(db, token, for_update=True)
        project = await _project_for_invitation(db, inv, for_update=True)
        inviter = await _validate_inviter_scope(db, inv, project)

        # 二次防御：注册期间该 email 是否被抢占
        existing = await db.execute(
            select(User).where(User.email == inv.email, User.is_active.is_(True))
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail="该邮箱已注册，请让该用户登录后确认项目邀请",
            )

        group_name = (inv.group_name or "").strip() or None
        if group_name is not None and len(group_name) > 100:
            raise HTTPException(
                status_code=409,
                detail="邀请中的数据组名称无效，请联系管理员重发邀请",
            )
        group = (
            await _resolve_or_create_group(db, group_name)
            if group_name is not None
            else None
        )
        inv.group_name = group.name if group else None

        # Defense in depth: a legacy staff role must never reach account
        # creation.  Rows are normalized on resend (and were converted by
        # migration 0174), so an unconverted value here means an unmanaged
        # historical row — fail closed instead of creating an account the
        # authorization model rejects.
        if inv.role not in PLATFORM_ROLES:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="该邀请包含已失效的账号角色，请联系管理员重发",
            )

        user = User(
            email=inv.email,
            name=name,
            password_hash=hash_password(password),
            role=inv.role,
            group_name=group.name if group else None,
            group_id=group.id if group else None,
            status="online",
            is_active=True,
            # 邀请 token 本身即身份证明，邀请注册恒视为已验证
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()

        member_role = invitation_project_role(inv)
        if project is not None:
            _assert_project_role_compatible(member_role)
            await _add_project_membership(
                db,
                project_id=project.id,
                user_id=user.id,
                role=member_role,
                assigned_by=inviter.id,
            )

        inv.accepted_at = datetime.now(timezone.utc)
        inv.accepted_user_id = user.id
        await db.flush()
        acceptance = await _acceptance_summary(
            db,
            user=user,
            project=project,
            member_role=member_role,
        )
        return user, inv, acceptance

    @staticmethod
    async def accept_existing(
        db: AsyncSession, *, token: str, user: User
    ) -> tuple[User, UserInvitation, dict[str, object]]:
        initial = await InvitationService.resolve(db, token)
        await _lock_user(db, initial.invited_by)
        await _lock_invitation_email(db, initial.email)
        await _project_for_invitation(db, initial, for_update=True)
        inv = await InvitationService.resolve(db, token, for_update=True)
        project = await _project_for_invitation(db, inv, for_update=True)
        if project is None:
            raise HTTPException(
                status_code=409,
                detail="该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请",
            )
        inviter = await _validate_inviter_scope(db, inv, project)

        locked_user = await _lock_user(db, user.id)
        if locked_user is None or not locked_user.is_active:
            raise HTTPException(status_code=401, detail="账号已停用，请重新登录")
        if locked_user.email.strip().lower() != inv.email.strip().lower():
            raise HTTPException(
                status_code=403,
                detail="当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录",
            )

        member_role = invitation_project_role(inv)
        if project is not None:
            _assert_project_role_compatible(member_role)
            _assert_existing_role_compatible(locked_user, member_role)
            await _add_project_membership(
                db,
                project_id=project.id,
                user_id=locked_user.id,
                role=member_role,
                assigned_by=inviter.id,
            )

        inv.accepted_at = datetime.now(timezone.utc)
        inv.accepted_user_id = locked_user.id
        await db.flush()
        acceptance = await _acceptance_summary(
            db,
            user=locked_user,
            project=project,
            member_role=member_role,
        )
        return locked_user, inv, acceptance
