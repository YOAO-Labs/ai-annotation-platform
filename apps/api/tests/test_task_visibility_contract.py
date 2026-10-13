"""Pure SQL-contract tests for project-role task visibility.

No database: the scheduler predicates must fail closed on a missing or unknown
project role (never falling back to the global role), and the viewer scope must
stay batch-level rather than entering the annotator-only unbatched/rework arms.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.user import User
from app.services.scheduler import (
    batch_visibility_clause,
    task_visibility_clause,
    visible_batch_statuses_for,
    visible_batch_statuses_for_project_role,
)


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": False}))


def _user(role: str) -> User:
    return User(id=uuid.uuid4(), role=role, is_active=True)


@pytest.mark.parametrize(
    "project_role", [None, "", "unknown", "super_admin", "project_admin"]
)
def test_missing_or_unknown_project_role_fails_closed(project_role):
    user = _user("employee")
    assert visible_batch_statuses_for(user, project_role=project_role) == []
    assert visible_batch_statuses_for_project_role(project_role) == []
    assert _sql(task_visibility_clause(user, project_role=project_role)) == "false"
    assert _sql(batch_visibility_clause(user, project_role=project_role)) == "false"


def test_legacy_global_role_is_not_a_fallback():
    """A global annotator/reviewer value must not authorize without a role."""

    for global_role in ("annotator", "reviewer"):
        legacy = _user(global_role)
        assert visible_batch_statuses_for(legacy, project_role=None) == []
        assert _sql(task_visibility_clause(legacy, project_role=None)) == "false"


def test_viewer_scope_is_batch_level_and_excludes_annotator_arms():
    user = _user("employee")
    viewer_sql = _sql(task_visibility_clause(user, project_role="viewer"))
    annotator_sql = _sql(task_visibility_clause(user, project_role="annotator"))

    # Viewer keeps task-level assignee-override filtering (active/annotating and
    # rejected assigned arms) but must not enter the two annotator-only arms.
    assert "tasks.assignee_id" in viewer_sql
    assert "tasks.batch_id IS NULL" not in viewer_sql  # unbatched arm
    assert "tasks.status" not in viewer_sql  # reviewing rework arm

    # The annotator branch does include both annotator-only arms.
    assert "tasks.batch_id IS NULL" in annotator_sql
    assert "tasks.status" in annotator_sql
    assert viewer_sql != annotator_sql

    assert visible_batch_statuses_for(
        user, project_role="viewer"
    ) == visible_batch_statuses_for(user, project_role="annotator")


def test_reviewer_scope_includes_unbatched_reviewer():
    user = _user("employee")
    reviewer_sql = _sql(task_visibility_clause(user, project_role="reviewer"))
    viewer_sql = _sql(task_visibility_clause(user, project_role="viewer"))
    assert "tasks.reviewer_id" in reviewer_sql
    assert reviewer_sql != viewer_sql
