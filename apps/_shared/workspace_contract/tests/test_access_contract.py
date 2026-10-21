from __future__ import annotations

import pytest

from aap_workspace_contract.access import (
    ANNOTATOR_ROLE,
    PLATFORM_ROLE_EMPLOYEE,
    PLATFORM_ROLE_VIEWER,
    REVIEWER_ROLE,
    VIEWER_ROLE,
    ProjectCapability,
    membership_role_compatible,
    platform_role_is_manager,
    project_role_capabilities,
)


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
        ("unknown", ANNOTATOR_ROLE, False),
        (None, VIEWER_ROLE, False),
        (PLATFORM_ROLE_EMPLOYEE, "unknown", False),
    ],
)
def test_membership_compatibility_preserves_platform_and_project_roles(
    platform_role, project_role, expected
):
    assert membership_role_compatible(platform_role, project_role) is expected


def test_platform_managers_are_recognized_and_other_roles_fail_closed():
    assert platform_role_is_manager("super_admin")
    assert platform_role_is_manager("project_admin")
    assert not platform_role_is_manager("employee")
    assert not platform_role_is_manager("annotator")
    assert not platform_role_is_manager("reviewer")
    assert not platform_role_is_manager("unknown")
    assert not platform_role_is_manager(None)
