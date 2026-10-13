"""Pure regression tests for the canonical project authority predicates.

These run without a database: ``platform_role_is_manager`` and
``is_privileged_for_project`` operate on already-loaded ORM rows and are the
single source reused by the scheduler, workers, batch state machine and
aggregate reads.  The request-path counterpart is
``resolve_project_access`` (covered by ``test_project_access.py``).
"""

from __future__ import annotations

import uuid

from app.db.models.project import Project
from app.db.models.user import User
from app.services.project_access import (
    is_privileged_for_project,
    platform_role_is_manager,
)


def _user(role: str, *, active: bool = True) -> User:
    return User(id=uuid.uuid4(), role=role, is_active=active)


def _project(owner_id: uuid.UUID) -> Project:
    return Project(owner_id=owner_id)


def test_platform_role_is_manager_matches_administrative_roles():
    assert platform_role_is_manager("super_admin")
    assert platform_role_is_manager("project_admin")
    # Employee/viewer are never management; legacy staff values and unknown
    # values fail closed instead of being treated as historical authority.
    assert not platform_role_is_manager("employee")
    assert not platform_role_is_manager("viewer")
    assert not platform_role_is_manager("annotator")
    assert not platform_role_is_manager("reviewer")
    assert not platform_role_is_manager(None)


def test_active_super_admin_is_privileged_anywhere():
    user = _user("super_admin")
    assert is_privileged_for_project(user, _project(uuid.uuid4())) is True


def test_inactive_super_admin_is_not_privileged():
    user = _user("super_admin", active=False)
    assert is_privileged_for_project(user, _project(uuid.uuid4())) is False


def test_administrative_owner_is_privileged():
    user = _user("project_admin")
    assert is_privileged_for_project(user, _project(user.id)) is True


def test_administrative_non_owner_is_not_privileged():
    user = _user("project_admin")
    assert is_privileged_for_project(user, _project(uuid.uuid4())) is False


def test_ownership_alone_never_grants_authority():
    user = _user("employee")
    assert is_privileged_for_project(user, _project(user.id)) is False


def test_legacy_staff_owner_is_not_privileged():
    user = _user("annotator")
    assert is_privileged_for_project(user, _project(user.id)) is False
