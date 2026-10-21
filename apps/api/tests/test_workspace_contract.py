"""Workspace value contracts stay cold-importable and fail closed."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from aap_workspace_contract.access import (
    MANAGER_CAPABILITIES,
    ProjectAccess,
    ProjectCapability,
    membership_role_compatible,
    platform_role_is_manager,
    project_role_capabilities,
)


def test_shared_workspace_contract_cold_imports_without_workspace_runtime():
    api_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(
        api_root.parent / "_shared" / "workspace_contract" / "src"
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from aap_workspace_contract.access import ProjectAccess, ProjectCapability, "
                "ProjectWritePolicyFailure, ProjectWritePolicyRequest, ProjectWritePolicyResolver; "
                "from aap_workspace_contract.project_query_contract import "
                "ProjectQueryRequest, ProjectQuerySuccess; "
                "from aap_workspace_contract.project_query_values import decode_project_row; "
                "from aap_workspace_contract.project_read_authority import "
                "accessible_project_row; "
                "from aap_workspace_contract.member_values import "
                "decode_project_member_rows; "
                "assert ProjectAccess and ProjectCapability and ProjectWritePolicyFailure; "
                "assert ProjectWritePolicyRequest and ProjectWritePolicyResolver; "
                "assert ProjectQueryRequest and ProjectQuerySuccess; "
                "assert decode_project_row and accessible_project_row and "
                "decode_project_member_rows; "
                "assert not any(name == 'app' or name.startswith('app.') for name in sys.modules); "
                "assert not any(name == 'aap_workspace' or name.startswith('aap_workspace.') for name in sys.modules); "
                "assert 'fastapi' not in sys.modules; "
                "assert 'sqlalchemy' not in sys.modules; "
                "assert 'httpx' not in sys.modules; "
                "assert 'asyncpg' not in sys.modules; "
                "assert not any(name == 'aap_inference' or "
                "name.startswith('aap_inference.') for name in sys.modules)"
            ),
        ],
        cwd=api_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_role_capabilities_and_compatibility_preserve_the_authority_matrix():
    read = {
        ProjectCapability.PROJECT_READ.value,
        ProjectCapability.MEMBER_READ.value,
        ProjectCapability.TASK_READ.value,
    }
    assert project_role_capabilities("annotator") == read | {
        ProjectCapability.ANNOTATION_WRITE.value
    }
    assert project_role_capabilities("reviewer") == read | {
        ProjectCapability.REVIEW_WRITE.value,
        ProjectCapability.EXPORT_ANNOTATIONS.value,
    }
    assert project_role_capabilities("viewer") == read
    assert project_role_capabilities("unknown") == frozenset()
    assert MANAGER_CAPABILITIES == frozenset(
        capability.value for capability in ProjectCapability
    )

    assert membership_role_compatible("employee", "reviewer")
    assert membership_role_compatible("viewer", "viewer")
    assert not membership_role_compatible("viewer", "annotator")
    assert not membership_role_compatible("project_admin", "reviewer")
    assert not membership_role_compatible("annotator", "annotator")
    assert platform_role_is_manager("super_admin")
    assert platform_role_is_manager("project_admin")
    assert not platform_role_is_manager("employee")
    assert not platform_role_is_manager("annotator")


def test_project_access_value_checks_all_requested_capabilities():
    access = ProjectAccess(
        user_id=uuid4(),
        project_id=uuid4(),
        platform_role="employee",
        project_role="reviewer",
        membership_id=uuid4(),
        membership_version=3,
        access_kind="member",
        capabilities=project_role_capabilities("reviewer"),
    )
    assert access.has(ProjectCapability.REVIEW_WRITE.value)
    assert not access.has(
        ProjectCapability.REVIEW_WRITE.value,
        ProjectCapability.ANNOTATION_WRITE.value,
    )
