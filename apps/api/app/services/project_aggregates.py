"""Project-role SQL scopes for aggregate, search and delivery reads (B3).

This module owns the *list / count / dashboard / job* side of the
project-scoped employee model.  It never treats platform role as project
authority: the project role always comes from ``project_members`` and a
missing/unknown membership fails closed.  Management is a super administrator
or the project owner whose *platform* role is administrative, matching
:func:`app.services.project_access.is_privileged_for_project` and
:class:`yoao_workspace_contract.access.ProjectAccess`.

Every helper returns a SQL predicate so one query can restrict a whole page of
tasks, batches or jobs without an N+1 membership lookup per row.  Callers pass
explicit :class:`ProjectRole` values for the work they are aggregating; an empty
role set means "any membership", never "any account role".
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.enums import (
    PLATFORM_ROLES,
    PROJECT_ROLES,
    PlatformRole,
    ProjectRole,
)
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from yoao_identity.read_relation_contract import AccountProfile
from yoao_workspace_contract.access import platform_role_is_manager
from app.services.identity_contracts import IdentityActorFacts, IdentityActorStatus
from app.services.identity_read_relations import account_profiles
from app.services.workspace_read_relations import (
    actor_project_memberships,
    project_facts,
    project_member_facts,
    project_search_scope,
)

DASHBOARD_PLATFORM_ROLES = (
    PlatformRole.SUPER_ADMIN,
    PlatformRole.PROJECT_ADMIN,
    PlatformRole.EMPLOYEE,
)

ANNOTATOR_ROLE = ProjectRole.ANNOTATOR.value
REVIEWER_ROLE = ProjectRole.REVIEWER.value
VIEWER_ROLE = ProjectRole.VIEWER.value


def _valid_membership_conditions(project_role_column):
    """SQL conditions mirroring the shared resolver's membership contract.

    A membership only authorizes when the account is active, its platform role
    is a current known value (never legacy ``annotator``/``reviewer``), the
    project role is a known current value, and the platform/project-role pairing
    is valid (a platform viewer may only hold a viewer membership).  Callers
    must join the Identity profile for the actor represented by the membership.
    """

    return (
        account_profiles.c.is_active.is_(True),
        account_profiles.c.role.in_(list(PLATFORM_ROLES)),
        project_role_column.in_(list(PROJECT_ROLES)),
        or_(
            account_profiles.c.role != PlatformRole.VIEWER.value,
            project_role_column == ProjectRole.VIEWER.value,
        ),
    )


def valid_membership_conditions(project_role_column):
    """Public alias for the shared valid-membership SQL contract.

    Other aggregate/delivery owners (for example notification delivery) reuse
    this so the active / known-platform-role / known-project-role / viewer
    pairing contract is defined once.
    """

    return _valid_membership_conditions(project_role_column)


def membership_project_ids(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus,
    *project_roles: str,
) -> Select[tuple[object]]:
    """Project IDs where ``user`` holds a valid membership, optionally role-filtered."""

    project_scope = actor_project_memberships(user.id)
    facts = select(
        project_scope.c.project_id,
        project_scope.c.member_role.label("role"),
    ).subquery("workspace_member_facts")
    membership = aliased(ProjectMember, facts, adapt_on_names=True)
    stmt = (
        select(membership.project_id.label("project_id"))
        .select_from(facts.join(account_profiles, account_profiles.c.id == user.id))
        .where(*_valid_membership_conditions(membership.role))
    )
    if project_roles:
        stmt = stmt.where(membership.role.in_(list(project_roles)))
    return stmt


def member_user_ids(project_id, *project_roles: str) -> Select[tuple[object]]:
    """User IDs holding a valid membership in ``project_id``, optionally role-filtered."""

    member_facts = project_member_facts()
    membership = aliased(ProjectMember, member_facts, adapt_on_names=True)
    stmt = (
        select(membership.user_id)
        .select_from(membership)
        .join(account_profiles, account_profiles.c.id == membership.user_id)
        .where(
            membership.project_id == project_id,
            *_valid_membership_conditions(membership.role),
        )
    )
    if project_roles:
        stmt = stmt.where(membership.role.in_(list(project_roles)))
    return stmt


def _managed_project_ids(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus,
) -> Select[tuple[object]]:
    return owned_project_ids(user)


def owned_project_ids(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus,
) -> Select[tuple[object]]:
    """Owned IDs in the caller's candidate scope; callers retain manager gates."""

    project_scope = project_search_scope(user)
    facts = select(
        project_scope.c.project_id.label("id"), project_scope.c.owner_id
    ).subquery("workspace_owned_project_facts")
    project = aliased(Project, facts, adapt_on_names=True)
    return select(project.id.label("project_id")).where(project.owner_id == user.id)


def managed_project_scope_clause(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus, project_column
):
    """Management-only aggregate scope, mirroring the canonical access resolver."""
    if not getattr(user, "is_active", False) or user.role not in PLATFORM_ROLES:
        return false()
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return None
    if platform_role_is_manager(user.role):
        return project_column.in_(_managed_project_ids(user))
    return false()


def project_scope_clause(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus,
    project_column,
    *,
    project_roles=(),
):
    """SQL predicate restricting ``project_column`` to the account's projects.

    * super administrator → ``None`` (no restriction)
    * administrative platform role → owned projects *plus* matching memberships
    * everyone else → matching memberships only

    ``project_roles`` are explicit :class:`ProjectRole` value strings.  A
    foreign-project platform administrator gets only the membership arm, never
    management over another owner's project.  ``None`` means the caller must not
    add a predicate (global scope).
    """

    if not getattr(user, "is_active", False) or user.role not in PLATFORM_ROLES:
        return false()
    if user.role == PlatformRole.SUPER_ADMIN.value:
        return None
    arms = [project_column.in_(membership_project_ids(user, *project_roles))]
    if platform_role_is_manager(user.role):
        arms.append(project_column.in_(_managed_project_ids(user)))
    return or_(*arms)


async def count_visible_projects_by_primary_pool(
    db: AsyncSession, actor: IdentityActorFacts
) -> dict[UUID, int]:
    projects = project_facts()
    pool_id = projects.c.ml_backend_pool_id
    query = select(pool_id, func.count()).where(pool_id.is_not(None)).group_by(pool_id)
    scope = project_scope_clause(actor, projects.c.id)
    if scope is not None:
        query = query.where(scope)
    return {pool: count for pool, count in await db.execute(query)}


def task_project_scope(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus, *, project_roles=()
):
    """``project_scope_clause`` bound to ``Task.project_id``."""

    return project_scope_clause(user, Task.project_id, project_roles=project_roles)


def is_platform_manager(
    user: AccountProfile | IdentityActorFacts | IdentityActorStatus,
) -> bool:
    """True for an administrative *platform* role (ownership still required)."""

    return platform_role_is_manager(user.role)
