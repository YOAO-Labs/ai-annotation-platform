"""Portable project-access values and capability policy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

__all__ = [
    "ACCESS_KIND_MEMBER",
    "ACCESS_KIND_OWNER",
    "ACCESS_KIND_SUPER_ADMIN",
    "ANNOTATOR_ROLE",
    "MANAGER_CAPABILITIES",
    "MANAGER_PLATFORM_ROLES",
    "PLATFORM_ROLE_EMPLOYEE",
    "PLATFORM_ROLE_VIEWER",
    "PROJECT_ROLES",
    "ProjectAccess",
    "ProjectCapability",
    "ProjectAccessPolicyFailure",
    "ProjectWritePolicyFailure",
    "ProjectWritePolicyRequest",
    "ProjectWritePolicyResolver",
    "REVIEWER_ROLE",
    "VIEWER_ROLE",
    "membership_role_compatible",
    "platform_role_is_manager",
    "project_manager_access",
    "project_member_access",
    "project_role_capabilities",
]


class ProjectCapability(str, Enum):
    """Fixed capability set for a project request context."""

    PROJECT_READ = "project.read"
    PROJECT_MANAGE = "project.manage"
    MEMBER_READ = "member.read"
    MEMBER_MANAGE = "member.manage"
    TASK_READ = "task.read"
    ANNOTATION_WRITE = "annotation.write"
    REVIEW_WRITE = "review.write"
    #: Full project / batch / selected-task annotation export. Excludes annotators and viewers.
    EXPORT_ANNOTATIONS = "export.annotations"
    #: Project performance details and CSV; annotation export does not grant this.
    PERFORMANCE_READ = "performance.read"


ACCESS_KIND_SUPER_ADMIN = "super_admin"
ACCESS_KIND_OWNER = "owner"
ACCESS_KIND_MEMBER = "member"

PLATFORM_ROLE_EMPLOYEE = "employee"
PLATFORM_ROLE_VIEWER = "viewer"
MANAGER_PLATFORM_ROLES = frozenset({"super_admin", "project_admin"})

ANNOTATOR_ROLE = "annotator"
REVIEWER_ROLE = "reviewer"
VIEWER_ROLE = "viewer"
PROJECT_ROLES = frozenset({ANNOTATOR_ROLE, REVIEWER_ROLE, VIEWER_ROLE})

_READ_CAPABILITIES = frozenset(
    {
        ProjectCapability.PROJECT_READ.value,
        ProjectCapability.MEMBER_READ.value,
        ProjectCapability.TASK_READ.value,
    }
)
MANAGER_CAPABILITIES = frozenset(capability.value for capability in ProjectCapability)

_PROJECT_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ANNOTATOR_ROLE: _READ_CAPABILITIES | {ProjectCapability.ANNOTATION_WRITE.value},
    REVIEWER_ROLE: _READ_CAPABILITIES
    | {
        ProjectCapability.REVIEW_WRITE.value,
        ProjectCapability.EXPORT_ANNOTATIONS.value,
    },
    VIEWER_ROLE: _READ_CAPABILITIES,
}


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """Current, immutable access facts for one account in one project."""

    user_id: uuid.UUID
    project_id: uuid.UUID
    platform_role: str
    project_role: str | None
    membership_id: uuid.UUID | None
    membership_version: int | None
    access_kind: str
    capabilities: frozenset[str]
    is_super_admin: bool = False
    is_owner: bool = False
    is_manager: bool = False

    def has(self, *capabilities: str) -> bool:
        return all(capability in self.capabilities for capability in capabilities)


@dataclass(frozen=True, slots=True)
class ProjectWritePolicyRequest:
    """The identifiers needed to resolve current write authority locally."""

    actor_id: uuid.UUID
    project_id: uuid.UUID


class ProjectWritePolicyFailure(Exception):
    """Expected resolver failure facts, independent of HTTP and persistence."""

    def __init__(
        self,
        status_code: int,
        detail: object,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


class ProjectAccessPolicyFailure(Exception):
    """Expected authority-policy failure facts, independent of HTTP and persistence."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def project_manager_access(
    *,
    actor_id: uuid.UUID,
    platform_role: str,
    project_id: uuid.UUID,
    owner_id: uuid.UUID | None,
) -> ProjectAccess | None:
    """Return manager authority for a current actor/project pair, if applicable."""

    if platform_role not in MANAGER_PLATFORM_ROLES | {
        PLATFORM_ROLE_EMPLOYEE,
        PLATFORM_ROLE_VIEWER,
    }:
        raise ProjectAccessPolicyFailure(403, "账号角色无效，访问被拒绝")
    if platform_role == "super_admin":
        return ProjectAccess(
            user_id=actor_id,
            project_id=project_id,
            platform_role=platform_role,
            project_role=None,
            membership_id=None,
            membership_version=None,
            access_kind=ACCESS_KIND_SUPER_ADMIN,
            capabilities=MANAGER_CAPABILITIES,
            is_super_admin=True,
            is_owner=owner_id == actor_id,
            is_manager=True,
        )
    if platform_role in MANAGER_PLATFORM_ROLES and owner_id == actor_id:
        return ProjectAccess(
            user_id=actor_id,
            project_id=project_id,
            platform_role=platform_role,
            project_role=None,
            membership_id=None,
            membership_version=None,
            access_kind=ACCESS_KIND_OWNER,
            capabilities=MANAGER_CAPABILITIES,
            is_super_admin=False,
            is_owner=True,
            is_manager=True,
        )
    return None


def project_member_access(
    *,
    actor_id: uuid.UUID,
    platform_role: str,
    project_id: uuid.UUID,
    membership_id: uuid.UUID | None,
    project_role: str | None,
    membership_version: int | None = None,
) -> ProjectAccess:
    """Resolve membership authority with the legacy visibility/error ordering."""

    if membership_id is None:
        raise ProjectAccessPolicyFailure(404, "项目不存在")
    if project_role not in PROJECT_ROLES:
        raise ProjectAccessPolicyFailure(403, "项目职责无效，访问被拒绝")
    if platform_role == PLATFORM_ROLE_VIEWER and project_role != VIEWER_ROLE:
        raise ProjectAccessPolicyFailure(403, "账号平台角色与项目职责不兼容")
    return ProjectAccess(
        user_id=actor_id,
        project_id=project_id,
        platform_role=platform_role,
        project_role=project_role,
        membership_id=membership_id,
        membership_version=membership_version,
        access_kind=ACCESS_KIND_MEMBER,
        capabilities=project_role_capabilities(project_role),
        is_super_admin=False,
        is_owner=False,
        is_manager=False,
    )


class ProjectWritePolicyResolver(Protocol):
    """Session-free local port; an implementation may bind the caller's session."""

    async def resolve(self, request: ProjectWritePolicyRequest) -> ProjectAccess: ...


def project_role_capabilities(project_role: str) -> frozenset[str]:
    """Return the capability set for a project role; unknown roles fail closed."""

    return _PROJECT_ROLE_CAPABILITIES.get(project_role, frozenset())


def platform_role_is_manager(platform_role: str | None) -> bool:
    return platform_role in MANAGER_PLATFORM_ROLES


def membership_role_compatible(platform_role: str | None, project_role: str) -> bool:
    """Whether an account role may hold the requested project membership role."""

    if project_role not in PROJECT_ROLES:
        return False
    if platform_role == PLATFORM_ROLE_VIEWER:
        return project_role == VIEWER_ROLE
    return platform_role == PLATFORM_ROLE_EMPLOYEE
