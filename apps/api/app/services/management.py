"""Shared query and impact helpers for the administrator management APIs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, true, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PLATFORM_ROLES, TaskStatus, UserRole
from app.db.models.group import Group
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.schemas.group import GroupOut
from app.schemas.invitation import InvitationOut
from app.schemas.management import (
    BatchDistributionPreview,
    BatchDistributionPreviewItem,
    BatchDistributionRecipient,
    InvitationStats,
    RoleImpactPreview,
    RoleImpactProject,
    UserStats,
)
from app.services.invitation import invitation_project_role


USER_STATUS = Literal["active", "inactive", "all"]
INVITATION_STATUS = Literal["pending", "accepted", "expired", "revoked", "all"]

# Project admins manage every *enabled* annotator/reviewer account
# (unassigned or in other projects' memberships) — interim until
# annotator/reviewer identity becomes project-driven; matches the widened
# member-assignment candidate list. Super admins stay read-only-visible but
# never manageable, and deactivated accounts stay out of scope.
#: Live platform roles a project administrator may manage/assign.  Legacy
#: global staff values are history only and never an eligible receiver.
_PA_OPERABLE_ROLES = (UserRole.EMPLOYEE.value,)


def _managed_user_ids(actor: User):
    """Return a correlated-independent subquery of a project admin's users."""

    return (
        select(ProjectMember.user_id)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(Project.owner_id == actor.id)
    )


async def fetch_managed_user_ids(db: AsyncSession, actor: User) -> set[UUID]:
    """Own-project member ids of a project admin (one concrete SELECT).

    Note this is only the project-bounded part of the manage scope; the
    platform-wide enabled annotator/reviewer arm is composed per row by
    callers (see ``query_users``) because the row itself already carries
    ``role`` / ``is_active``.
    """

    rows = (await db.execute(_managed_user_ids(actor))).scalars().all()
    return set(rows)


async def fetch_lifecycle_blocked_ids(
    db: AsyncSession, *, actor: User, user_ids: list[UUID]
) -> set[UUID]:
    """Ids among ``user_ids`` whose derived projects leave the actor's scope.

    Mirrors the project arms of ``UserLifecycleService._project_ids_for_target``
    (owned projects, memberships, batch assignments, tasks/locks) and keeps
    the users touching at least one project NOT owned by ``actor``. The
    lifecycle endpoints reject exactly these targets for project admins even
    though account-level writes use the wider manage scope, so the users page
    can hide offboarding / delete / reactivate up front instead of surfacing
    buttons that always end in 403.
    """

    if not user_ids:
        return set()

    ids = sorted(set(user_ids))
    foreign_arms = (
        # Projects the user owns.
        select(Project.owner_id.label("user_id")).where(
            Project.owner_id.in_(ids),
            Project.owner_id != actor.id,
        ),
        # Project memberships.
        select(ProjectMember.user_id.label("user_id"))
        .join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.user_id.in_(ids), Project.owner_id != actor.id),
        # Batch assignments (annotator / reviewer).
        select(TaskBatch.annotator_id.label("user_id"))
        .join(Project, Project.id == TaskBatch.project_id)
        .where(TaskBatch.annotator_id.in_(ids), Project.owner_id != actor.id),
        select(TaskBatch.reviewer_id.label("user_id"))
        .join(Project, Project.id == TaskBatch.project_id)
        .where(TaskBatch.reviewer_id.in_(ids), Project.owner_id != actor.id),
        # Task assignments (assignee / reviewer).
        select(Task.assignee_id.label("user_id"))
        .join(Project, Project.id == Task.project_id)
        .where(Task.assignee_id.in_(ids), Project.owner_id != actor.id),
        select(Task.reviewer_id.label("user_id"))
        .join(Project, Project.id == Task.project_id)
        .where(Task.reviewer_id.in_(ids), Project.owner_id != actor.id),
        # Task locks.
        select(TaskLock.user_id.label("user_id"))
        .join(Task, Task.id == TaskLock.task_id)
        .join(Project, Project.id == Task.project_id)
        .where(TaskLock.user_id.in_(ids), Project.owner_id != actor.id),
    )
    rows = (await db.execute(union(*foreign_arms))).scalars().all()
    return {row for row in rows if row is not None}


def user_scope_clause(
    actor: User,
    *,
    project_id: UUID | None = None,
):
    """Scope **write/management** guards to the actor's manageable user set.

    Management *writes* (bulk group assignment, and previews that must not
    disclose out-of-scope accounts) use this rule: super admins manage all
    users; project admins manage themselves, users belonging to projects
    they own, plus — until annotator/reviewer identity becomes
    project-driven — every *enabled* annotator/reviewer account (unassigned
    or in other projects' memberships).  Deactivated accounts outside their
    projects, other project admins, viewers, and super admins stay out of a
    project admin's manage scope.

    When ``project_id`` is given the clause stays strictly project-bounded
    (self + own-project members, intersected with that project's members) so
    the platform-wide arm can neither disclose nor mutate foreign-project
    membership lists.
    """

    if actor.role == UserRole.SUPER_ADMIN.value:
        clause = true()
    elif project_id is not None:
        clause = or_(User.id == actor.id, User.id.in_(_managed_user_ids(actor)))
    else:
        clause = or_(
            User.id == actor.id,
            User.id.in_(_managed_user_ids(actor)),
            and_(
                User.is_active.is_(True),
                User.role.in_(_PA_OPERABLE_ROLES),
            ),
        )

    if project_id is not None:
        project_users = select(ProjectMember.user_id).where(
            ProjectMember.project_id == project_id
        )
        clause = and_(clause, User.id.in_(project_users))
    return clause


def user_visibility_clause(
    actor: User,
    *,
    project_id: UUID | None = None,
):
    """Scope read-only management queries (list / stats / export).

    Super admins see all users.  Project admins see their manage scope
    (``user_scope_clause``: self, own-project members, every enabled
    annotator/reviewer) plus every *enabled* super admin as read-only
    contact/lookup — that extra arm never grants management, and super-admin
    rows stay non-manageable for project admins.  Deactivated accounts
    outside the admin's projects, other project admins, and viewers stay
    hidden.

    When ``project_id`` is given the clause stays project-bounded exactly
    like ``user_scope_clause``: a foreign project yields nothing, so the
    platform-wide arms cannot disclose cross-project membership.
    """

    if project_id is not None:
        # Delegates to the strict project-bounded branch of the manage scope
        # (self + own-project members ∩ that project); the platform-wide arms
        # never apply to project-filtered queries.
        return user_scope_clause(actor, project_id=project_id)

    if actor.role == UserRole.SUPER_ADMIN.value:
        return true()

    return or_(
        user_scope_clause(actor),
        and_(
            User.is_active.is_(True),
            User.role == UserRole.SUPER_ADMIN.value,
        ),
    )


def validate_platform_role_filter(role: str | None) -> str | None:
    """Reject legacy/unknown staff-role filters instead of returning nothing.

    ``role=annotator``/``reviewer`` was the old global staff query; after the
    employee cutover it must be a validation error, never a silently empty set.
    """

    if role is None:
        return None
    if role not in PLATFORM_ROLES:
        raise HTTPException(status_code=400, detail=f"非法平台角色: {role}")
    return role


def build_user_query(
    actor: User,
    *,
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    role: str | None = None,
    status_filter: USER_STATUS = "active",
    search: str | None = None,
):
    role = validate_platform_role_filter(role)
    query = select(User).where(user_visibility_clause(actor, project_id=project_id))
    if status_filter == "active":
        query = query.where(User.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.where(User.is_active.is_(False))
    if group_id is not None:
        query = query.where(User.group_id == group_id)
    if role:
        query = query.where(User.role == role)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(User.email.ilike(term), User.name.ilike(term)))
    return query


async def fetch_user_page(
    db: AsyncSession,
    actor: User,
    *,
    page: int,
    page_size: int,
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    role: str | None = None,
    status_filter: USER_STATUS = "active",
    search: str | None = None,
) -> tuple[list[User], int]:
    base = build_user_query(
        actor,
        project_id=project_id,
        group_id=group_id,
        role=role,
        status_filter=status_filter,
        search=search,
    )
    total = int(
        await db.scalar(
            select(func.count()).select_from(base.order_by(None).subquery())
        )
        or 0
    )
    rows = (
        (
            await db.execute(
                base.order_by(User.created_at.desc(), User.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return rows, total


async def user_stats(
    db: AsyncSession,
    actor: User,
    *,
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    role: str | None = None,
    status_filter: USER_STATUS = "active",
    search: str | None = None,
) -> UserStats:
    from datetime import timedelta

    base = build_user_query(
        actor,
        project_id=project_id,
        group_id=group_id,
        role=role,
        status_filter=status_filter,
        search=search,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    total = int(
        await db.scalar(
            select(func.count()).select_from(base.order_by(None).subquery())
        )
        or 0
    )
    online = int(
        await db.scalar(
            select(func.count()).select_from(
                base.where(User.status == "online").order_by(None).subquery()
            )
        )
        or 0
    )
    weekly_active = int(
        await db.scalar(
            select(func.count()).select_from(
                base.where(User.last_seen_at >= cutoff).order_by(None).subquery()
            )
        )
        or 0
    )
    return UserStats(total=total, online=online, weekly_active=weekly_active)


async def fetch_group_page(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    search: str | None = None,
) -> tuple[list[GroupOut], int]:
    query = select(Group)
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(Group.name.ilike(term), Group.description.ilike(term)))
    total = int(
        await db.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        )
        or 0
    )
    rows = (
        (
            await db.execute(
                query.order_by(Group.name.asc(), Group.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return [], total
    ids = [group.id for group in rows]
    counts = dict(
        (
            row[0],
            int(row[1] or 0),
        )
        for row in (
            await db.execute(
                select(Group.id, func.count(User.id))
                .outerjoin(
                    User,
                    (User.group_id == Group.id) & User.is_active.is_(True),
                )
                .where(Group.id.in_(ids))
                .group_by(Group.id)
            )
        ).all()
    )
    return [
        GroupOut(
            id=group.id,
            name=group.name,
            description=group.description,
            member_count=counts.get(group.id, 0),
            created_at=group.created_at,
        )
        for group in rows
    ], total


def _invitation_status_clause(status: str, now: datetime):
    if status == "accepted":
        return UserInvitation.accepted_at.is_not(None)
    if status == "revoked":
        return and_(
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_not(None),
        )
    if status == "expired":
        return and_(
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
            UserInvitation.expires_at <= now,
        )
    if status == "pending":
        return and_(
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
            UserInvitation.expires_at > now,
        )
    return true()


def build_invitation_query(
    actor: User,
    *,
    status_filter: INVITATION_STATUS = "all",
    scope: Literal["me", "all"] = "me",
    project_id: UUID | None = None,
    role: str | None = None,
    email: str | None = None,
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    now: datetime | None = None,
):
    if scope == "me" or actor.role != UserRole.SUPER_ADMIN.value:
        query = select(UserInvitation).where(UserInvitation.invited_by == actor.id)
    else:
        query = select(UserInvitation)
    if project_id is not None:
        query = query.where(UserInvitation.project_id == project_id)
    if role:
        query = query.where(UserInvitation.role == role)
    if email and email.strip():
        query = query.where(UserInvitation.email.ilike(f"%{email.strip()}%"))
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(
            or_(
                UserInvitation.email.ilike(term),
                UserInvitation.group_name.ilike(term),
            )
        )
    if created_from is not None:
        query = query.where(UserInvitation.created_at >= created_from)
    if created_to is not None:
        query = query.where(UserInvitation.created_at <= created_to)
    query = query.where(
        _invitation_status_clause(status_filter, now or datetime.now(timezone.utc))
    )
    return query


async def _invitation_outputs(
    db: AsyncSession, rows: list[UserInvitation]
) -> list[InvitationOut]:
    if not rows:
        return []
    inviter_ids = {row.invited_by for row in rows}
    inviter_rows = (
        (await db.execute(select(User).where(User.id.in_(inviter_ids)))).scalars().all()
    )
    inviters = {user.id: user for user in inviter_rows}
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    if project_ids:
        project_rows = await db.execute(
            select(Project).where(Project.id.in_(project_ids))
        )
        projects = {
            project.id: project.name for project in project_rows.scalars().all()
        }
    else:
        projects = {}
    return [
        InvitationOut(
            id=row.id,
            email=row.email,
            role=row.role,
            group_name=row.group_name,
            project_id=row.project_id,
            project_name=projects.get(row.project_id),
            project_member_role=invitation_project_role(row),
            status=row.status,
            expires_at=row.expires_at,
            invited_by=row.invited_by,
            invited_by_name=(
                inviters.get(row.invited_by).name
                if inviters.get(row.invited_by)
                else None
            ),
            accepted_at=row.accepted_at,
            revoked_at=row.revoked_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def fetch_invitation_page(
    db: AsyncSession,
    actor: User,
    *,
    page: int,
    page_size: int | None,
    status_filter: INVITATION_STATUS = "all",
    scope: Literal["me", "all"] = "me",
    project_id: UUID | None = None,
    role: str | None = None,
    email: str | None = None,
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> tuple[list[InvitationOut], int]:
    base = build_invitation_query(
        actor,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )
    total = int(
        await db.scalar(
            select(func.count()).select_from(base.order_by(None).subquery())
        )
        or 0
    )
    ordered = base.order_by(UserInvitation.created_at.desc(), UserInvitation.id.desc())
    if page_size is not None:
        ordered = ordered.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(ordered)).scalars().all()
    return await _invitation_outputs(db, rows), total


async def invitation_stats(
    db: AsyncSession,
    actor: User,
    *,
    status_filter: INVITATION_STATUS = "all",
    scope: Literal["me", "all"] = "me",
    project_id: UUID | None = None,
    role: str | None = None,
    email: str | None = None,
    search: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> InvitationStats:
    now = datetime.now(timezone.utc)
    base = build_invitation_query(
        actor,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
        now=now,
    )
    subquery = base.order_by(None).subquery()
    accepted = and_(subquery.c.accepted_at.is_not(None))
    revoked = and_(subquery.c.accepted_at.is_(None), subquery.c.revoked_at.is_not(None))
    expired = and_(
        subquery.c.accepted_at.is_(None),
        subquery.c.revoked_at.is_(None),
        subquery.c.expires_at <= now,
    )
    pending = and_(
        subquery.c.accepted_at.is_(None),
        subquery.c.revoked_at.is_(None),
        subquery.c.expires_at > now,
    )
    row = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(pending),
                func.count().filter(accepted),
                func.count().filter(expired),
                func.count().filter(revoked),
            ).select_from(subquery)
        )
    ).one()
    return InvitationStats(
        total=int(row[0] or 0),
        pending=int(row[1] or 0),
        accepted=int(row[2] or 0),
        expired=int(row[3] or 0),
        revoked=int(row[4] or 0),
    )


async def platform_role_change_blockers(
    db: AsyncSession, *, target: User, new_role: str
) -> list[str]:
    """Blockers shared by platform-role preview and mutation.

    Leaving an administrative identity (including a demotion to ``employee``)
    must not create an anomalous non-administrative project owner, and a
    platform-role edit never rewrites project memberships.
    """

    from app.db.enums import MANAGER_PLATFORM_ROLES, PlatformRole, ProjectRole
    from app.services.scheduler import (
        effective_task_assignee_expr,
        effective_task_reviewer_expr,
    )

    blockers: list[str] = []
    is_manager_role = new_role in MANAGER_PLATFORM_ROLES

    if is_manager_role or new_role == PlatformRole.VIEWER.value:
        work_memberships = int(
            await db.scalar(
                select(func.count(ProjectMember.id)).where(
                    ProjectMember.user_id == target.id,
                    ProjectMember.role != ProjectRole.VIEWER.value,
                )
            )
            or 0
        )
        if work_memberships:
            blockers.append("账号仍持有标注/质检项目职责，请先在项目中变更职责")

    # Ownership blocks every non-administrative platform role, including
    # employee: an employee owner would be an authorization anomaly.
    if not is_manager_role:
        owned = int(
            await db.scalar(
                select(func.count(Project.id)).where(Project.owner_id == target.id)
            )
            or 0
        )
        if owned:
            blockers.append("账号仍是项目负责人，请先转移项目后变更平台角色")

    if new_role == PlatformRole.VIEWER.value:
        unfinished_statuses = (
            TaskStatus.PENDING.value,
            TaskStatus.IN_PROGRESS.value,
            TaskStatus.REJECTED.value,
            TaskStatus.REVIEW.value,
        )
        unfinished = int(
            await db.scalar(
                select(func.count(Task.id))
                .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
                .where(
                    Task.status.in_(unfinished_statuses),
                    or_(
                        effective_task_assignee_expr() == target.id,
                        effective_task_reviewer_expr() == target.id,
                    ),
                )
            )
            or 0
        )
        if unfinished:
            blockers.append("账号仍有未完成的标注/质检工作，请先交接")

    return blockers


async def role_impact_preview(
    db: AsyncSession,
    *,
    actor: User,
    target: User,
    requested_role: str,
    manager_target_check: bool,
    assignable_roles: set[str],
) -> RoleImpactPreview:
    blockers: list[str] = []
    if target.id == actor.id:
        blockers.append("不能修改自己的角色")
    if not target.is_active:
        blockers.append("用户已停用")
    if actor.role == UserRole.PROJECT_ADMIN.value:
        if (
            target.role not in assignable_roles
            or requested_role not in assignable_roles
        ):
            blockers.append("项目管理员仅能在审核员 / 标注员之间切换角色")
        if not manager_target_check:
            blockers.append("该用户不在你管理的项目内")
    if requested_role == target.role:
        blockers.append("目标角色与当前角色相同")
    if (
        target.role == UserRole.SUPER_ADMIN.value
        and requested_role != UserRole.SUPER_ADMIN.value
    ):
        active_super_admins = int(
            await db.scalar(
                select(func.count(User.id)).where(
                    User.role == UserRole.SUPER_ADMIN.value,
                    User.is_active.is_(True),
                )
            )
            or 0
        )
        if active_super_admins <= 1:
            blockers.append("不能降级最后一名超级管理员")

    if requested_role != target.role:
        # Preview must report the same blockers the mutation enforces.
        blockers.extend(
            await platform_role_change_blockers(
                db, target=target, new_role=requested_role
            )
        )

    project_query = (
        select(ProjectMember, Project)
        .join(Project, Project.id == ProjectMember.project_id)
        .where(ProjectMember.user_id == target.id)
    )
    other_project_count = 0
    if actor.role == UserRole.PROJECT_ADMIN.value:
        project_query = project_query.where(Project.owner_id == actor.id)
        other_project_count = int(
            await db.scalar(
                select(func.count(ProjectMember.id))
                .join(Project, Project.id == ProjectMember.project_id)
                .where(ProjectMember.user_id == target.id, Project.owner_id != actor.id)
            )
            or 0
        )
    project_rows = (
        await db.execute(project_query.order_by(Project.name.asc(), Project.id))
    ).all()
    projects: list[RoleImpactProject] = []
    assigned_batch_count = 0
    assigned_task_count = 0
    review_task_count = 0
    for member, project in project_rows:
        annotator_batch_count = int(
            await db.scalar(
                select(func.count(TaskBatch.id)).where(
                    TaskBatch.project_id == project.id,
                    TaskBatch.annotator_id == target.id,
                )
            )
            or 0
        )
        reviewer_batch_count = int(
            await db.scalar(
                select(func.count(TaskBatch.id)).where(
                    TaskBatch.project_id == project.id,
                    TaskBatch.reviewer_id == target.id,
                )
            )
            or 0
        )
        assigned = int(
            await db.scalar(
                select(func.count(Task.id)).where(
                    Task.project_id == project.id,
                    Task.assignee_id == target.id,
                )
            )
            or 0
        )
        reviewed = int(
            await db.scalar(
                select(func.count(Task.id)).where(
                    Task.project_id == project.id,
                    Task.reviewer_id == target.id,
                )
            )
            or 0
        )
        assigned_batch_count += annotator_batch_count + reviewer_batch_count
        assigned_task_count += assigned
        review_task_count += reviewed
        projects.append(
            RoleImpactProject(
                project_id=project.id,
                project_name=project.name,
                membership_role=member.role,
                annotator_batch_count=annotator_batch_count,
                reviewer_batch_count=reviewer_batch_count,
                assigned_task_count=assigned,
                review_task_count=reviewed,
            )
        )
    return RoleImpactPreview(
        user_id=target.id,
        email=target.email,
        current_role=target.role,
        requested_role=requested_role,
        can_change=not blockers,
        blockers=blockers,
        projects=projects,
        assigned_batch_count=assigned_batch_count,
        assigned_task_count=assigned_task_count,
        review_task_count=review_task_count,
        other_project_count=other_project_count,
        warnings=[
            "平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。",
            "若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。",
        ]
        + (
            [
                f"另有 {other_project_count} 个你无权查看的项目受到影响，请联系超级管理员核对。"
            ]
            if other_project_count
            else []
        ),
    )


async def preview_batch_distribution(
    db: AsyncSession,
    *,
    project_id: UUID,
    annotator_ids: list[UUID],
    reviewer_ids: list[UUID],
    only_unassigned: bool,
    validate_targets,
    lock_batches: bool = False,
    batch_ids: list[UUID] | None = None,
    single_batch_id: UUID | None = None,
    clear_unselected: bool = False,
) -> BatchDistributionPreview:
    """Calculate distribution changes without changing batches or tasks."""

    if (
        not annotator_ids
        and not reviewer_ids
        and not (single_batch_id and clear_unselected)
    ):
        raise HTTPException(
            status_code=400, detail="annotator_ids or reviewer_ids required"
        )
    await validate_targets(
        project_id,
        [("annotator", user_id) for user_id in annotator_ids]
        + [("reviewer", user_id) for user_id in reviewer_ids],
    )
    batch_query = (
        select(TaskBatch)
        .where(
            TaskBatch.project_id == project_id,
            TaskBatch.status != "archived",
            TaskBatch.id.in_(batch_ids) if batch_ids is not None else True,
            TaskBatch.id == single_batch_id if single_batch_id is not None else True,
        )
        .order_by(TaskBatch.priority.desc(), TaskBatch.created_at, TaskBatch.id)
        .execution_options(populate_existing=True)
    )
    if lock_batches:
        batch_query = batch_query.with_for_update()
    batches = (await db.execute(batch_query)).scalars().all()
    if batch_ids is not None and len(batches) != len(set(batch_ids)):
        raise HTTPException(
            status_code=409, detail="选中批次已被移除或归档，请刷新后重新选择"
        )
    if not batches:
        raise HTTPException(status_code=400, detail="没有可分派的批次")
    # Aggregate persisted tasks rather than trusting cached batch counters.
    task_groups = (
        await db.execute(
            select(
                Task.batch_id,
                Task.assignee_id,
                Task.reviewer_id,
                Task.status,
                func.count(),
            )
            .where(Task.batch_id.in_([batch.id for batch in batches]))
            .group_by(Task.batch_id, Task.assignee_id, Task.reviewer_id, Task.status)
        )
    ).all()
    by_batch: dict[UUID, list] = {}
    for row in task_groups:
        by_batch.setdefault(row.batch_id, []).append(row)
    pending_statuses = {
        "annotator": {"pending", "in_progress", "rejected"},
        "reviewer": {"review"},
    }
    recipients: dict[tuple[str, UUID], BatchDistributionRecipient] = {}
    for role, user_ids in (("annotator", annotator_ids), ("reviewer", reviewer_ids)):
        if not user_ids:
            continue
        owner_column = Task.assignee_id if role == "annotator" else Task.reviewer_id
        backlog = dict(
            (
                await db.execute(
                    select(owner_column, func.count())
                    .where(
                        owner_column.in_(user_ids),
                        Task.status.in_(pending_statuses[role]),
                    )
                    .group_by(owner_column)
                )
            ).all()
        )
        for user_id in dict.fromkeys(user_ids):
            recipients[role, user_id] = BatchDistributionRecipient(
                user_id=user_id,
                role=role,
                new_task_count=0,
                existing_backlog_count=backlog.get(user_id, 0),
            )
    items: list[BatchDistributionPreviewItem] = []
    a_idx = 0
    r_idx = 0
    candidate = 0
    changed = 0
    skipped = 0
    for batch in batches:
        before_a = batch.annotator_id
        before_r = batch.reviewer_id
        after_a = before_a
        after_r = before_r
        eligible = False
        if clear_unselected and single_batch_id:
            eligible = True
            if not annotator_ids:
                after_a = None
            if not reviewer_ids:
                after_r = None
        if annotator_ids and (not only_unassigned or before_a is None):
            eligible = True
            after_a = annotator_ids[a_idx % len(annotator_ids)]
            a_idx += 1
        if reviewer_ids and (not only_unassigned or before_r is None):
            eligible = True
            after_r = reviewer_ids[r_idx % len(reviewer_ids)]
            r_idx += 1
        if eligible:
            candidate += 1
        will_change = after_a != before_a or after_r != before_r
        rows = by_batch.get(batch.id, [])
        for role, before, after, index in (
            ("annotator", before_a, after_a, 1),
            ("reviewer", before_r, after_r, 2),
        ):
            if after is not None and after != before:
                recipients[role, after].new_task_count += sum(
                    row[4]
                    for row in rows
                    if row[index] != after and row.status in pending_statuses[role]
                )
        reason = None
        if not eligible:
            skipped += 1
            reason = (
                "已分派，only_unassigned 未覆盖"
                if only_unassigned
                else "没有可更新的职责"
            )
        elif not will_change:
            skipped += 1
            reason = "目标与当前分派相同"
        else:
            changed += 1
        items.append(
            BatchDistributionPreviewItem(
                batch_id=batch.id,
                display_id=batch.display_id,
                name=batch.name,
                status=batch.status,
                task_count=sum(row[4] for row in rows),
                before_annotator_id=before_a,
                after_annotator_id=after_a,
                before_reviewer_id=before_r,
                after_reviewer_id=after_r,
                will_change=will_change,
                skipped_reason=reason,
            )
        )
    version = hashlib.sha256(
        json.dumps(
            {
                "project_id": str(project_id),
                "annotator_ids": [str(value) for value in annotator_ids],
                "reviewer_ids": [str(value) for value in reviewer_ids],
                "only_unassigned": only_unassigned,
                "items": [item.model_dump(mode="json") for item in items],
                "recipients": [
                    item.model_dump(mode="json") for item in recipients.values()
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return BatchDistributionPreview(
        project_id=project_id,
        only_unassigned=only_unassigned,
        total_batches=len(batches),
        candidate_batches=candidate,
        changed_batches=changed,
        skipped_batches=skipped,
        items=items,
        recipient_summary=list(recipients.values()),
        preview_version=version,
    )
