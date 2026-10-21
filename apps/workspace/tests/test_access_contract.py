from __future__ import annotations

import uuid

import pytest

from aap_workspace.access_contract import (
    ACCESS_KIND_MEMBER,
    ACCESS_KIND_OWNER,
    ACCESS_KIND_SUPER_ADMIN,
    ANNOTATOR_ROLE,
    MANAGER_CAPABILITIES,
    MANAGER_PLATFORM_ROLES,
    PLATFORM_ROLE_EMPLOYEE,
    PLATFORM_ROLE_VIEWER,
    PROJECT_ROLES,
    REVIEWER_ROLE,
    VIEWER_ROLE,
    ProjectAccess,
    ProjectCapability,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
    membership_role_compatible,
    platform_role_is_manager,
    project_role_capabilities,
)

_USER_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")


def test_capability_enum_names_order_values_and_manager_set_are_frozen():
    assert [capability.name for capability in ProjectCapability] == [
        "PROJECT_READ",
        "PROJECT_MANAGE",
        "MEMBER_READ",
        "MEMBER_MANAGE",
        "TASK_READ",
        "ANNOTATION_WRITE",
        "REVIEW_WRITE",
        "EXPORT_ANNOTATIONS",
        "PERFORMANCE_READ",
    ]
    assert [capability.value for capability in ProjectCapability] == [
        "project.read",
        "project.manage",
        "member.read",
        "member.manage",
        "task.read",
        "annotation.write",
        "review.write",
        "export.annotations",
        "performance.read",
    ]
    assert "Fixed capability set" in ProjectCapability.__doc__
    assert MANAGER_CAPABILITIES == frozenset(
        capability.value for capability in ProjectCapability
    )
    assert ACCESS_KIND_SUPER_ADMIN == "super_admin"
    assert ACCESS_KIND_OWNER == "owner"
    assert ACCESS_KIND_MEMBER == "member"


def test_access_values_preserve_the_immutable_fields_and_capability_check():
    access = ProjectAccess(
        user_id=_USER_ID,
        project_id=_PROJECT_ID,
        platform_role=PLATFORM_ROLE_EMPLOYEE,
        project_role=ANNOTATOR_ROLE,
        membership_id=None,
        membership_version=None,
        access_kind=ACCESS_KIND_MEMBER,
        capabilities=project_role_capabilities(ANNOTATOR_ROLE),
    )

    assert access.user_id == _USER_ID
    assert access.project_id == _PROJECT_ID
    assert access.has(ProjectCapability.PROJECT_READ.value)
    assert access.has(
        ProjectCapability.PROJECT_READ.value,
        ProjectCapability.ANNOTATION_WRITE.value,
    )
    assert not access.has(ProjectCapability.REVIEW_WRITE.value)
    with pytest.raises((AttributeError, TypeError)):
        access.project_role = REVIEWER_ROLE


def test_role_capability_sets_and_unknown_roles_fail_closed():
    read = {
        ProjectCapability.PROJECT_READ.value,
        ProjectCapability.MEMBER_READ.value,
        ProjectCapability.TASK_READ.value,
    }
    assert project_role_capabilities(ANNOTATOR_ROLE) == read | {
        ProjectCapability.ANNOTATION_WRITE.value
    }
    assert project_role_capabilities(REVIEWER_ROLE) == read | {
        ProjectCapability.REVIEW_WRITE.value,
        ProjectCapability.EXPORT_ANNOTATIONS.value,
    }
    assert project_role_capabilities(VIEWER_ROLE) == read
    assert project_role_capabilities("unknown") == frozenset()
    assert PROJECT_ROLES == frozenset({ANNOTATOR_ROLE, REVIEWER_ROLE, VIEWER_ROLE})


@pytest.mark.parametrize(
    ("platform_role", "project_role", "expected"),
    [
        (PLATFORM_ROLE_EMPLOYEE, ANNOTATOR_ROLE, True),
        (PLATFORM_ROLE_EMPLOYEE, REVIEWER_ROLE, True),
        (PLATFORM_ROLE_EMPLOYEE, VIEWER_ROLE, True),
        (PLATFORM_ROLE_VIEWER, VIEWER_ROLE, True),
        (PLATFORM_ROLE_VIEWER, ANNOTATOR_ROLE, False),
        (PLATFORM_ROLE_VIEWER, REVIEWER_ROLE, False),
        ("super_admin", VIEWER_ROLE, False),
        ("project_admin", ANNOTATOR_ROLE, False),
        ("annotator", ANNOTATOR_ROLE, False),
        ("reviewer", REVIEWER_ROLE, False),
        (None, VIEWER_ROLE, False),
        (PLATFORM_ROLE_EMPLOYEE, "unknown", False),
    ],
)
def test_membership_compatibility_preserves_platform_and_project_roles(
    platform_role, project_role, expected
):
    assert membership_role_compatible(platform_role, project_role) is expected


def test_manager_roles_and_write_policy_values_remain_framework_free():
    assert MANAGER_PLATFORM_ROLES == frozenset({"super_admin", "project_admin"})
    assert platform_role_is_manager("super_admin")
    assert platform_role_is_manager("project_admin")
    assert not platform_role_is_manager("employee")
    assert not platform_role_is_manager(None)

    request = ProjectWritePolicyRequest(actor_id=_USER_ID, project_id=_PROJECT_ID)
    failure = ProjectWritePolicyFailure(403, "denied", {"X-Policy": "project"})
    assert request.actor_id == _USER_ID
    assert request.project_id == _PROJECT_ID
    assert failure.status_code == 403
    assert failure.detail == "denied"
    assert failure.headers == {"X-Policy": "project"}
