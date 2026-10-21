"""Negative coverage for project-access binding and task-state lock precedence.

Canonical service guards must reject a foreign account/project context before
a manager bypass, while completed tasks remain locked even for their manager.
These checks do not require a database.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import task_access
from aap_workspace_contract.access import ProjectAccess


def _access(*, user_id: uuid.UUID, project_id: uuid.UUID) -> ProjectAccess:
    return ProjectAccess(
        user_id=user_id,
        project_id=project_id,
        platform_role="employee",
        project_role="annotator",
        membership_id=uuid.uuid4(),
        membership_version=1,
        access_kind="member",
        capabilities=frozenset(),
    )


def test_access_binding_rejects_other_project():
    user_id = uuid.uuid4()
    access = _access(user_id=user_id, project_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        task_access.assert_access_binding(
            access,
            actor_id=user_id,
            project_id=uuid.uuid4(),
        )
    assert exc.value.status_code == 409


def test_access_binding_rejects_other_user():
    access = _access(user_id=uuid.uuid4(), project_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        task_access.assert_access_binding(
            access,
            actor_id=uuid.uuid4(),
            project_id=access.project_id,
        )
    assert exc.value.status_code == 409


def test_task_editability_rejects_foreign_manager_context():
    actor_id = uuid.uuid4()
    task = SimpleNamespace(project_id=uuid.uuid4(), status="review")
    access = replace(
        _access(user_id=actor_id, project_id=uuid.uuid4()),
        project_role=None,
        access_kind="owner",
        is_manager=True,
    )

    with pytest.raises(HTTPException) as exc:
        task_access.assert_task_editable(task, actor_id, access=access)

    assert exc.value.status_code == 409
    assert exc.value.detail == {"reason": "access_context_mismatch"}


def test_completed_task_remains_locked_for_its_manager():
    actor_id = uuid.uuid4()
    task = SimpleNamespace(project_id=uuid.uuid4(), status="completed")
    access = replace(
        _access(user_id=actor_id, project_id=task.project_id),
        project_role=None,
        access_kind="owner",
        is_manager=True,
    )

    with pytest.raises(HTTPException) as exc:
        task_access.assert_task_editable(task, actor_id, access=access)

    assert exc.value.status_code == 409
    assert exc.value.detail == {"reason": "task_locked", "status": "completed"}
