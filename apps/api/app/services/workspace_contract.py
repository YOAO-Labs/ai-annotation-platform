"""Pure workspace access values and rules shared by local application modules."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

from app.db.enums import (
    MANAGER_PLATFORM_ROLES as _MANAGER_PLATFORM_ROLES,
    PROJECT_ROLES as _PROJECT_ROLES,
    PlatformRole as _PlatformRole,
    ProjectRole as _ProjectRole,
)

__all__ = [
    "ACCESS_KIND_MEMBER",
    "ACCESS_KIND_OWNER",
    "ACCESS_KIND_SUPER_ADMIN",
    "MANAGER_CAPABILITIES",
    "ProjectAccess",
    "ProjectCapability",
    "ProjectWritePolicyFailure",
    "ProjectWritePolicyRequest",
    "ProjectWritePolicyResolver",
    "membership_role_compatible",
    "platform_role_is_manager",
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

_READ_CAPABILITIES = frozenset(
    {
        ProjectCapability.PROJECT_READ.value,
        ProjectCapability.MEMBER_READ.value,
        ProjectCapability.TASK_READ.value,
    }
)
MANAGER_CAPABILITIES = frozenset(capability.value for capability in ProjectCapability)

_PROJECT_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    _ProjectRole.ANNOTATOR.value: _READ_CAPABILITIES
    | {ProjectCapability.ANNOTATION_WRITE.value},
    _ProjectRole.REVIEWER.value: _READ_CAPABILITIES
    | {
        ProjectCapability.REVIEW_WRITE.value,
        ProjectCapability.EXPORT_ANNOTATIONS.value,
    },
    _ProjectRole.VIEWER.value: _READ_CAPABILITIES,
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


class ProjectWritePolicyResolver(Protocol):
    """Session-free local port; an implementation may bind the caller's session."""

    async def resolve(self, request: ProjectWritePolicyRequest) -> ProjectAccess: ...


def project_role_capabilities(project_role: str) -> frozenset[str]:
    """Return the capability set for a project role; unknown roles fail closed."""

    return _PROJECT_ROLE_CAPABILITIES.get(project_role, frozenset())


def platform_role_is_manager(platform_role: str | None) -> bool:
    return platform_role in _MANAGER_PLATFORM_ROLES


def membership_role_compatible(platform_role: str, project_role: str) -> bool:
    """Whether an account role may hold the requested project membership role."""

    if project_role not in _PROJECT_ROLES:
        return False
    if platform_role == _PlatformRole.VIEWER.value:
        return project_role == _ProjectRole.VIEWER.value
    return platform_role == _PlatformRole.EMPLOYEE.value
