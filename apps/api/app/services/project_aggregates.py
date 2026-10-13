"""Project-role SQL scopes for aggregate, search and delivery reads (B3).

This module owns the *list / count / dashboard / job* side of the
project-scoped employee model.  It never reads ``User.role`` as project
authority: the project role always comes from ``project_members`` and a
missing/unknown membership fails closed.  Management is a super administrator
or the project owner whose *platform* role is administrative, matching
:func:`app.services.project_access.is_privileged_for_project` and
:class:`app.services.project_access.ProjectAccess`.

Every helper returns a SQL predicate so one query can restrict a whole page of
tasks, batches or jobs without an N+1 membership lookup per row.  Callers pass
explicit :class:`ProjectRole` values for the work they are aggregating; an empty
role set means "any membership", never "any account role".
"""

from __future__ import annotations

from sqlalchemy import Select, false, or_, select

from app.db.enums import (
    PLATFORM_ROLES,
    PROJECT_ROLES,
    PlatformRole,
    ProjectRole,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.services.project_access import platform_role_is_manager

#: Platform roles allowed to open the personal annotator/reviewer dashboards.
#: Legacy ``annotator`` / ``reviewer`` account values are intentionally absent;
#: they are historical facts and must not authorize a request.
DASHBOARD_PLATFORM_ROLES = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)

ANNOTATOR_ROLE = ProjectRole.ANNOTATOR.value
REVIEWER_ROLE = ProjectRole.REVIEWER.value
VIEWER_ROLE = ProjectRole.VIEWER.value


def _valid_membership_conditions():
    """SQL conditions mirroring the shared resolver's membership contract.

    A membership only authorizes when the account is active, its platform role
    is a current known value (never legacy ``annotator``/``reviewer``), the
    project role is a known current value, and the platform/project-role pairing
    is valid (a platform viewer may only hold a viewer membership).  Callers
    must join :class:`User` on the membership.
    """

    return (
        User.is_active.is_(True),
        User.role.in_(list(PLATFORM_ROLES)),
        ProjectMember.role.in_(list(PROJECT_ROLES)),
        or_(
            User.role != PlatformRole.VIEWER.value,
            ProjectMember.role == ProjectRole.VIEWER.value,
        ),
    )


def valid_membership_conditions():
    """Public alias for the shared valid-membership SQL contract.

    Other aggregate/delivery owners (for example notification delivery) reuse
    this so the active / known-platform-role / known-project-role / viewer
    pairing contract is defined once.
    """

    return _valid_membership_conditions()


def membership_project_ids(user: User, *project_roles: str) -> Select[tuple[object]]:
    """Project IDs where ``user`` holds a valid membership, optionally role-filtered."""

    stmt = (
        select(ProjectMember.project_id)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.user_id == user.id, *_valid_membership_conditions())
    )
    if project_roles:
        stmt = stmt.where(ProjectMember.role.in_(list(project_roles)))
    return stmt


def member_user_ids(project_id, *project_roles: str) -> Select[tuple[object]]:
    """User IDs holding a valid membership in ``project_id``, optionally role-filtered."""

    stmt = (
        select(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id, *_valid_membership_conditions())
    )
    if project_roles:
        stmt = stmt.where(ProjectMember.role.in_(list(project_roles)))
    return stmt


def _managed_project_ids(user: User) -> Select[tuple[object]]:
    return select(Project.id).where(Project.owner_id == user.id)


def project_scope_clause(user: User, project_column, *, project_roles=()):
    """SQL predicate restricting ``project_column`` to the account's projects.

    * super administrator → ``None`` (no restriction)
    * administrative platform role → owned projects *plus* matching memberships
    * everyone else → matching memberships only

    ``project_roles`` are explicit :class:`ProjectRole` value strings.  A
    foreign-project platform administrator gets only the membership arm, never
    management over another owner's project.  ``None`` means the caller must not
    add a predicate (global scope).
    """

    # Fail closed for an inactive account or a legacy/unknown platform role;
    # the resolver would reject the request, and aggregate SQL must not open a
    # wider scope than the request can ever pass.
    if not getattr(user, "is_active", False) or user.role not in PLATFORM_ROLES:
        return false()
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return None
    arms = [project_column.in_(membership_project_ids(user, *project_roles))]
    if platform_role_is_manager(user.role):
        arms.append(project_column.in_(_managed_project_ids(user)))
    return or_(*arms)


def task_project_scope(user: User, *, project_roles=()):
    """``project_scope_clause`` bound to ``Task.project_id``."""

    return project_scope_clause(user, Task.project_id, project_roles=project_roles)


def is_platform_manager(user: User) -> bool:
    """True for an administrative *platform* role (ownership still required)."""

    return platform_role_is_manager(user.role)
