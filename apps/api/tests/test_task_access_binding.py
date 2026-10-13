"""Focused negative coverage for supplied project-access context binding.

``_assert_task_visible`` / ``_visible_task_ids`` / ``_assert_review_owner``
accept an already-resolved :class:`ProjectAccess` and must reject a context that
does not belong to the same account and resource project *before* any manager
privileged return.  These are pure checks: no database is required.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.tasks._shared import _assert_access_binding
from app.services.project_access import ProjectAccess


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


def test_access_binding_accepts_matching_context():
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    access = _access(user_id=user_id, project_id=project_id)

    _assert_access_binding(
        access, user=SimpleNamespace(id=user_id), project_id=project_id
    )


def test_access_binding_rejects_other_project():
    user_id = uuid.uuid4()
    access = _access(user_id=user_id, project_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        _assert_access_binding(
            access,
            user=SimpleNamespace(id=user_id),
            project_id=uuid.uuid4(),
        )
    assert exc.value.status_code == 409


def test_access_binding_rejects_other_user():
    access = _access(user_id=uuid.uuid4(), project_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        _assert_access_binding(
            access,
            user=SimpleNamespace(id=uuid.uuid4()),
            project_id=access.project_id,
        )
    assert exc.value.status_code == 409
