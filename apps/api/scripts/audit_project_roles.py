"""Read-only project-role discrepancy / reconciliation audit.

Increment A preparation for the project-scoped employee roles plan
(``docs/plans/1789807315_project-scoped-employee-roles.md``).  The CLI opens a
``REPEATABLE READ, READ ONLY`` transaction enforced by PostgreSQL and never
writes roles, memberships or assignments.

The audit also runs *before* revision 0173 exists.  It inspects
``information_schema`` for the additive preparation columns and substitutes
``NULL`` projections plus missing-evidence classifications, so reviewing a
production schema ahead of the migration is safe and reports unknown rather
than fabricating completeness.

Report shape (versioned JSON)::

    {
      "report_version": "project-role-audit/2",
      "run_id": "<uuid>",
      "generated_at": "<iso8601>",
      "read_only": true,
      "baseline": {...revisions, heads, available preparation columns...},
      "limits": {...},
      "summary": {...},
      "findings": {
        "role_discrepancies": {...},
        "assignment_membership_gaps": {...},
        "invitations": {...},
        "review_evidence": {...}
      },
      "reconciliation": {...}
    }

The report contains only IDs, roles, counts and timestamps.  It never emits
password hashes, invitation tokens, signed URLs or free-form reasons.

Usage::

    cd apps/api
    uv run python scripts/audit_project_roles.py --output /tmp/role-audit.json
    uv run python scripts/audit_project_roles.py --pretty

Exit codes:
    0  audit completed (findings never fail the command)
    2  the audit could not run (connection or schema error)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Keep the script runnable directly as `python scripts/audit_project_roles.py`
# from apps/api: sys.path[0] is the scripts directory, so add the API root
# before any `app.*` import (matching dump-openapi.py / cleanup scripts).
_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


AUDIT_REPORT_VERSION = "project-role-audit/2"
DEFAULT_MAX_ROWS = 1000

# Fixed role vocabularies.  ``employee`` is recognized for post-conversion
# reconciliation while the legacy annotator/reviewer values stay distinct so a
# pre-migration mismatch is not mistaken for project-role diversity.
PLATFORM_ROLES = (
    "super_admin",
    "project_admin",
    "employee",
    "annotator",
    "reviewer",
    "viewer",
)
EMPLOYEE_PLATFORM_ROLE = "employee"
LEGACY_EMPLOYEE_PLATFORM_ROLES = ("annotator", "reviewer")
PROJECT_ROLES = ("annotator", "reviewer", "viewer")
ADMIN_PLATFORM_ROLES = ("super_admin", "project_admin")

_PLATFORM_ROLES_SQL = (
    "('super_admin','project_admin','employee','annotator','reviewer','viewer')"
)
_LEGACY_EMPLOYEE_SQL = "('annotator','reviewer')"
_PROJECT_ROLES_SQL = "('annotator','reviewer','viewer')"
_ADMIN_PLATFORM_ROLES_SQL = "('super_admin','project_admin')"

PLAN_DOCUMENT = "docs/plans/1789807315_project-scoped-employee-roles.md"

# Columns introduced by revision 0173.  Their presence is inspected, never
# assumed, so the same report runs on a pre-0173 production schema.
PREPARATION_COLUMNS: dict[str, tuple[str, ...]] = {
    "project_members": ("version", "updated_at"),
    "user_invitations": ("project_role",),
    "tasks": (
        "annotation_contributor_ids",
        "review_contributor_ids",
        "review_submitter_id",
    ),
}

_REVIEW_ACTIVITY_SQL = (
    "review_round_id IS NOT NULL OR reviewer_claimed_at IS NOT NULL "
    "OR reviewed_at IS NOT NULL OR status IN ('review','completed')"
)

# Effective task assignment: an explicit task value wins over the inherited
# batch default, matching existing business semantics.
_EFFECTIVE_ASSIGNMENT_CTE = """
WITH effective AS (
    SELECT t.id AS task_id,
           t.project_id,
           t.status,
           COALESCE(t.assignee_id, b.annotator_id) AS effective_assignee_id,
           COALESCE(t.reviewer_id, b.reviewer_id) AS effective_reviewer_id
    FROM tasks t
    LEFT JOIN task_batches b ON b.id = t.batch_id
)
"""
_EFFECTIVE_ASSIGNMENT_JOINS = """
LEFT JOIN project_members ma
       ON ma.project_id = e.project_id AND ma.user_id = e.effective_assignee_id
LEFT JOIN users ua ON ua.id = e.effective_assignee_id
LEFT JOIN project_members mr
       ON mr.project_id = e.project_id AND mr.user_id = e.effective_reviewer_id
LEFT JOIN users ur ON ur.id = e.effective_reviewer_id
LEFT JOIN projects p ON p.id = e.project_id
"""
_EFFECTIVE_ASSIGNMENT_SELECT = (
    "SELECT e.task_id, e.project_id, e.status, "
    "e.effective_assignee_id, ma.role AS assignee_member_role, "
    "ua.is_active AS assignee_active, "
    "e.effective_reviewer_id, mr.role AS reviewer_member_role, "
    "ur.is_active AS reviewer_active "
    "FROM effective e " + _EFFECTIVE_ASSIGNMENT_JOINS
)


def active_manager_predicate(user_alias: str) -> str:
    """SQL mirror of :func:`app.services.scheduler.is_privileged_for_project`.

    Only an active account is a manager: a super administrator manages every
    project, and an administrative (``project_admin``) role manages the project
    it owns.  Managers are valid effective assignees/reviewers without a
    membership row.

    The predicate is kept strictly non-NULL (``p.id IS NOT NULL``, ``COALESCE``,
    ``IS NOT DISTINCT FROM``) so a missing project, missing user or NULL role
    fails closed instead of bypassing the gap through SQL three-valued logic.
    ``p`` is the ``projects`` alias from ``_EFFECTIVE_ASSIGNMENT_JOINS``.
    """

    return (
        "p.id IS NOT NULL AND "
        f"{user_alias}.is_active IS TRUE AND ("
        f"COALESCE({user_alias}.role = 'super_admin', false) "
        f"OR (COALESCE({user_alias}.role IN {_ADMIN_PLATFORM_ROLES_SQL}, false) "
        f"AND p.owner_id IS NOT DISTINCT FROM {user_alias}.id))"
    )


class AuditError(RuntimeError):
    """Raised when the audit cannot produce a trustworthy report."""


# ---------------------------------------------------------------------------
# Schema capability inspection
# ---------------------------------------------------------------------------


async def detect_schema_capabilities(db: AsyncSession) -> dict[str, bool]:
    """Report which 0173 preparation columns exist, without assuming any."""
    rows = await _fetch(
        db,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = ANY(current_schemas(false)) "
        "AND table_name IN ('project_members', 'user_invitations', 'tasks')",
    )
    available: dict[str, set[str]] = {table: set() for table in PREPARATION_COLUMNS}
    for row in rows:
        table = row["table_name"]
        if table in available:
            available[table].add(row["column_name"])
    return {
        f"{table}.{column}": column in available[table]
        for table, columns in PREPARATION_COLUMNS.items()
        for column in columns
    }


def project_role_projection(capabilities: dict[str, bool]) -> str:
    """Return the invitation project-role select expression, NULL before 0173."""
    if capabilities.get("user_invitations.project_role"):
        return "i.project_role"
    return "NULL::varchar"


def project_role_backfill_filter(capabilities: dict[str, bool]) -> str:
    """Legacy project invitations need a project_role backfill.

    Before 0173 every pending project invitation qualifies: its project role
    only exists in the platform ``role`` column.
    """
    if capabilities.get("user_invitations.project_role"):
        return "AND project_role IS NULL"
    return ""


# Canonical UUID text, case-insensitive; used to reject non-string or malformed
# contributor elements without echoing the raw array.
_UUID_TEXT_REGEX = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _uuid_array_valid(expression: str) -> str:
    """True only for a JSON array whose every element is a UUID string.

    The element extraction is guarded by a CASE so a non-array JSON value
    (object/scalar) never reaches ``jsonb_array_elements_text`` and raises.
    """
    guarded = (
        f"(CASE WHEN jsonb_typeof({expression}) = 'array' "
        f"THEN {expression} ELSE '[]'::jsonb END)"
    )
    return (
        f"COALESCE(jsonb_typeof({expression}) = 'array', false) "
        f"AND NOT EXISTS ("
        f"SELECT 1 FROM jsonb_array_elements_text({guarded}) AS element(value) "
        f"WHERE element.value IS NULL OR element.value !~* '{_UUID_TEXT_REGEX}')"
    )


def review_evidence_predicates(capabilities: dict[str, bool]) -> dict[str, str]:
    """Build mutually exclusive review-evidence predicates.

    Complete evidence requires a valid round, submitter, a frozen review array
    and an annotation accumulator that are both JSON arrays of UUID strings,
    the frozen set containing every accumulator ID and the submitter.  A
    non-NULL frozen array alone is not enough: a missing round/submitter,
    malformed or non-UUID array, sticky-unknown accumulator or a frozen set
    missing contributors stays incomplete.  Before 0173 every missing column is
    projected as NULL, so all review activity is classified incomplete.
    """
    submitter = (
        "review_submitter_id"
        if capabilities.get("tasks.review_submitter_id")
        else "NULL::uuid"
    )
    review_array = (
        "review_contributor_ids"
        if capabilities.get("tasks.review_contributor_ids")
        else "NULL::jsonb"
    )
    accumulator = (
        "annotation_contributor_ids"
        if capabilities.get("tasks.annotation_contributor_ids")
        else "NULL::jsonb"
    )
    activity = f"({_REVIEW_ACTIVITY_SQL})"
    has_round = "review_round_id IS NOT NULL"
    has_submitter = f"({submitter}) IS NOT NULL"
    # Valid = a JSON array whose every element is a non-NULL UUID string.
    review_valid = _uuid_array_valid(review_array)
    accumulator_valid = _uuid_array_valid(accumulator)
    contains_all = (
        f"({review_array}) @> to_jsonb(({submitter})::text) "
        f"AND ({review_array}) @> ({accumulator})"
    )
    complete = (
        f"{activity} AND {has_round} AND {has_submitter} "
        f"AND ({review_valid}) AND ({accumulator_valid}) AND ({contains_all})"
    )
    missing_round = f"{activity} AND NOT ({has_round})"
    missing_submitter = f"{activity} AND {has_round} AND NOT ({has_submitter})"
    malformed_review_array = (
        f"{activity} AND {has_round} AND {has_submitter} AND NOT ({review_valid})"
    )
    unknown_accumulator = (
        f"{activity} AND {has_round} AND {has_submitter} "
        f"AND ({review_valid}) AND NOT ({accumulator_valid})"
    )
    missing_contributors = (
        f"{activity} AND {has_round} AND {has_submitter} "
        f"AND ({review_valid}) AND ({accumulator_valid}) AND NOT ({contains_all})"
    )
    return {
        "complete": complete,
        "missing_round": missing_round,
        "missing_submitter": missing_submitter,
        "malformed_review_array": malformed_review_array,
        "unknown_accumulator": unknown_accumulator,
        "missing_contributors": missing_contributors,
        "incomplete": "("
        + " OR ".join(
            (
                missing_round,
                missing_submitter,
                malformed_review_array,
                unknown_accumulator,
                missing_contributors,
            )
        )
        + ")",
    }


def review_evidence_columns(capabilities: dict[str, bool]) -> dict[str, str]:
    """Return safe aliased evidence detail columns.

    The raw contributor arrays are deliberately never echoed: malformed
    payloads could contain arbitrary values, so only presence and JSON kind are
    reported.
    """
    return {
        "has_submitter": (
            "(review_submitter_id IS NOT NULL) AS has_submitter"
            if capabilities.get("tasks.review_submitter_id")
            else "false AS has_submitter"
        ),
        "review_contributor_kind": (
            "jsonb_typeof(review_contributor_ids) AS review_contributor_kind"
            if capabilities.get("tasks.review_contributor_ids")
            else "NULL::text AS review_contributor_kind"
        ),
        "annotation_contributor_kind": (
            "jsonb_typeof(annotation_contributor_ids) AS annotation_contributor_kind"
            if capabilities.get("tasks.annotation_contributor_ids")
            else "NULL::text AS annotation_contributor_kind"
        ),
    }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def _fetch(db: AsyncSession, sql: str, **params: Any) -> list[dict]:
    result = await db.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def _scalar(db: AsyncSession, sql: str, **params: Any) -> int:
    value = await db.scalar(text(sql), params)
    return int(value or 0)


def _limited(rows: list[dict], total: int, max_rows: int) -> dict:
    """Shape a bounded entity list plus its true count."""
    return {
        "total": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "items": rows,
    }


async def _count_and_rows(
    db: AsyncSession,
    *,
    count_sql: str,
    rows_sql: str,
    max_rows: int,
) -> dict:
    total = await _scalar(db, count_sql)
    if total == 0:
        return _limited([], 0, max_rows)
    rows = await _fetch(db, rows_sql, max_rows=max_rows)
    return _limited(rows, total, max_rows)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def script_heads() -> list[str]:
    """Return the Alembic head revisions declared by the checked-in graph."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    api_root = Path(__file__).resolve().parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


async def audit_schema_baseline(
    db: AsyncSession, capabilities: dict[str, bool]
) -> dict:
    database_revisions = [
        row["version_num"]
        for row in await _fetch(db, "SELECT version_num FROM alembic_version")
    ]
    heads = script_heads()
    return {
        "plan_document": PLAN_DOCUMENT,
        "increment": "A",
        "report_tool": "apps/api/scripts/audit_project_roles.py",
        "database_revisions": sorted(database_revisions),
        "script_heads": heads,
        "schema_in_sync": set(database_revisions) == set(heads),
        "schema_capabilities": capabilities,
    }


# ---------------------------------------------------------------------------
# Role discrepancies
# ---------------------------------------------------------------------------


async def audit_role_discrepancies(db: AsyncSession, *, max_rows: int) -> dict:
    unknown_platform_roles = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM users "
            f"WHERE role IS NULL OR role NOT IN {_PLATFORM_ROLES_SQL}"
        ),
        rows_sql=(
            "SELECT id, role FROM users "
            f"WHERE role IS NULL OR role NOT IN {_PLATFORM_ROLES_SQL} "
            "ORDER BY id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    unknown_member_roles = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM project_members "
            f"WHERE role IS NULL OR role NOT IN {_PROJECT_ROLES_SQL}"
        ),
        rows_sql=(
            "SELECT id, project_id, user_id, role FROM project_members "
            f"WHERE role IS NULL OR role NOT IN {_PROJECT_ROLES_SQL} "
            "ORDER BY id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # Legacy mismatch: the account's global annotator/reviewer role disagrees
    # with this membership.  Distinct from true cross-project diversity below.
    legacy_mismatches = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM project_members pm "
            "JOIN users u ON u.id = pm.user_id "
            f"WHERE u.role IN {_LEGACY_EMPLOYEE_SQL} "
            f"AND pm.role IN {_PROJECT_ROLES_SQL} AND pm.role <> u.role"
        ),
        rows_sql=(
            "SELECT pm.id, pm.project_id, pm.user_id, u.role AS platform_role, "
            "pm.role AS project_role, u.role AS expected_project_role "
            "FROM project_members pm JOIN users u ON u.id = pm.user_id "
            f"WHERE u.role IN {_LEGACY_EMPLOYEE_SQL} "
            f"AND pm.role IN {_PROJECT_ROLES_SQL} AND pm.role <> u.role "
            "ORDER BY pm.project_id, pm.user_id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # Post-conversion diversity: one account holding different project roles in
    # different projects.  Legitimate, reported separately.
    role_diversity = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM (SELECT pm.user_id FROM project_members pm "
            "GROUP BY pm.user_id HAVING count(DISTINCT pm.role) > 1) AS diverse"
        ),
        rows_sql=(
            "SELECT pm.user_id, count(DISTINCT pm.role) AS distinct_project_roles, "
            "array_agg(DISTINCT pm.role ORDER BY pm.role) AS project_roles "
            "FROM project_members pm GROUP BY pm.user_id "
            "HAVING count(DISTINCT pm.role) > 1 ORDER BY pm.user_id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # A platform viewer may only hold viewer memberships; anything else is a
    # genuine inconsistency rather than a legitimate mixed role.
    viewer_conflicts = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM project_members pm "
            "JOIN users u ON u.id = pm.user_id "
            "WHERE u.role = 'viewer' "
            "AND (pm.role IS NULL OR pm.role IS DISTINCT FROM 'viewer')"
        ),
        rows_sql=(
            "SELECT pm.id, pm.project_id, pm.user_id, u.role AS platform_role, "
            "pm.role AS project_role FROM project_members pm "
            "JOIN users u ON u.id = pm.user_id WHERE u.role = 'viewer' "
            "AND (pm.role IS NULL OR pm.role IS DISTINCT FROM 'viewer') "
            "ORDER BY pm.project_id, pm.user_id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # Administrators derive authority from ownership/platform role, not from an
    # ordinary employee membership.  Surface the rows for migration review.
    administrator_memberships = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM project_members pm "
            "JOIN users u ON u.id = pm.user_id "
            f"WHERE u.role IN {_ADMIN_PLATFORM_ROLES_SQL}"
        ),
        rows_sql=(
            "SELECT pm.id, pm.project_id, pm.user_id, u.role AS platform_role, "
            "pm.role AS project_role FROM project_members pm "
            "JOIN users u ON u.id = pm.user_id "
            f"WHERE u.role IN {_ADMIN_PLATFORM_ROLES_SQL} "
            "ORDER BY pm.project_id, pm.user_id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # An employee/unknown owner is a migration blocker: administrative
    # privilege requires a valid admin platform role plus ownership.
    invalid_owners = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM projects p LEFT JOIN users u ON u.id = p.owner_id "
            f"WHERE u.id IS NULL OR u.role IS NULL OR u.role NOT IN {_ADMIN_PLATFORM_ROLES_SQL}"
        ),
        rows_sql=(
            "SELECT p.id AS project_id, p.owner_id, u.role AS owner_role "
            "FROM projects p LEFT JOIN users u ON u.id = p.owner_id "
            f"WHERE u.id IS NULL OR u.role IS NULL OR u.role NOT IN {_ADMIN_PLATFORM_ROLES_SQL} "
            "ORDER BY p.id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    inactive_owners = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM projects p JOIN users u ON u.id = p.owner_id "
            "WHERE u.is_active IS NOT TRUE"
        ),
        rows_sql=(
            "SELECT p.id AS project_id, p.owner_id, u.role AS owner_role "
            "FROM projects p JOIN users u ON u.id = p.owner_id "
            "WHERE u.is_active IS NOT TRUE ORDER BY p.id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    return {
        "unknown_platform_roles": unknown_platform_roles,
        "unknown_member_roles": unknown_member_roles,
        "legacy_global_member_mismatches": legacy_mismatches,
        "cross_project_role_diversity": role_diversity,
        "viewer_membership_conflicts": viewer_conflicts,
        "administrator_memberships": administrator_memberships,
        "invalid_administrative_owners": invalid_owners,
        "inactive_administrative_owners": inactive_owners,
    }


# ---------------------------------------------------------------------------
# Assignment / membership gaps
# ---------------------------------------------------------------------------


async def audit_assignment_gaps(db: AsyncSession, *, max_rows: int) -> dict:
    def count_sql(predicate: str) -> str:
        return (
            _EFFECTIVE_ASSIGNMENT_CTE
            + "SELECT count(*) FROM effective e "
            + _EFFECTIVE_ASSIGNMENT_JOINS
            + f" WHERE {predicate}"
        )

    def rows_sql(predicate: str) -> str:
        # The rows query needs the same CTE as the count query, otherwise
        # ``FROM effective`` does not resolve on a nonempty result.
        return (
            _EFFECTIVE_ASSIGNMENT_CTE
            + _EFFECTIVE_ASSIGNMENT_SELECT
            + f" WHERE {predicate} ORDER BY e.task_id LIMIT :max_rows"
        )

    async def category(predicate: str) -> dict:
        return await _count_and_rows(
            db,
            count_sql=count_sql(predicate),
            rows_sql=rows_sql(predicate),
            max_rows=max_rows,
        )

    assignee_manager = active_manager_predicate("ua")
    reviewer_manager = active_manager_predicate("ur")

    assignee_missing = await category(
        "e.effective_assignee_id IS NOT NULL AND ma.id IS NULL "
        f"AND NOT ({assignee_manager})"
    )
    assignee_wrong = await category(
        "ma.id IS NOT NULL AND ma.role IS DISTINCT FROM 'annotator' "
        f"AND NOT ({assignee_manager})"
    )
    reviewer_missing = await category(
        "e.effective_reviewer_id IS NOT NULL AND mr.id IS NULL "
        f"AND NOT ({reviewer_manager})"
    )
    reviewer_wrong = await category(
        "mr.id IS NOT NULL AND mr.role IS DISTINCT FROM 'reviewer' "
        f"AND NOT ({reviewer_manager})"
    )
    inactive_assignments = await category(
        "(e.effective_assignee_id IS NOT NULL AND ua.is_active IS NOT TRUE) "
        "OR (e.effective_reviewer_id IS NOT NULL AND ur.is_active IS NOT TRUE)"
    )
    inactive_unfinished = await _count_and_rows(
        db,
        count_sql=(
            _EFFECTIVE_ASSIGNMENT_CTE + "SELECT count(DISTINCT u.id) FROM effective e "
            "JOIN users u ON u.is_active IS NOT TRUE "
            "AND u.id IN (e.effective_assignee_id, e.effective_reviewer_id) "
            "WHERE e.status <> 'completed'"
        ),
        rows_sql=(
            _EFFECTIVE_ASSIGNMENT_CTE
            + "SELECT u.id AS user_id, u.role, count(*) AS unfinished_tasks "
            "FROM effective e JOIN users u ON u.is_active IS NOT TRUE "
            "AND u.id IN (e.effective_assignee_id, e.effective_reviewer_id) "
            "WHERE e.status <> 'completed' "
            "GROUP BY u.id, u.role ORDER BY u.id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    return {
        "annotator_missing_membership": assignee_missing,
        "annotator_wrong_membership_role": assignee_wrong,
        "reviewer_missing_membership": reviewer_missing,
        "reviewer_wrong_membership_role": reviewer_wrong,
        "assignment_to_inactive_account": inactive_assignments,
        "inactive_accounts_with_unfinished_work": inactive_unfinished,
    }


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def audit_invitations(
    db: AsyncSession, *, capabilities: dict[str, bool], max_rows: int
) -> dict:
    status_counts = {
        "pending": await _scalar(
            db,
            "SELECT count(*) FROM user_invitations "
            "WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()",
        ),
        "accepted": await _scalar(
            db, "SELECT count(*) FROM user_invitations WHERE accepted_at IS NOT NULL"
        ),
        "revoked": await _scalar(
            db,
            "SELECT count(*) FROM user_invitations "
            "WHERE accepted_at IS NULL AND revoked_at IS NOT NULL",
        ),
        "expired": await _scalar(
            db,
            "SELECT count(*) FROM user_invitations "
            "WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at <= now()",
        ),
    }
    project_role = project_role_projection(capabilities)
    pending_rows_sql = (
        "SELECT i.id, i.project_id, i.role, "
        f"{project_role} AS project_role, i.expires_at, i.invited_by, "
        "(p.id IS NOT NULL) AS target_exists "
        "FROM user_invitations i LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE i.accepted_at IS NULL AND i.revoked_at IS NULL "
        "AND i.expires_at > now() ORDER BY i.project_id NULLS FIRST, i.id "
        "LIMIT :max_rows"
    )
    pending = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM user_invitations "
            "WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()"
        ),
        rows_sql=pending_rows_sql,
        max_rows=max_rows,
    )
    account_only_count = await _scalar(
        db,
        "SELECT count(*) FROM user_invitations WHERE project_id IS NULL "
        "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()",
    )
    project_target_count = await _scalar(
        db,
        "SELECT count(*) FROM user_invitations WHERE project_id IS NOT NULL "
        "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()",
    )
    deleted_targets = await _count_and_rows(
        db,
        count_sql=(
            "SELECT count(*) FROM user_invitations i "
            "LEFT JOIN projects p ON p.id = i.project_id "
            "WHERE i.project_id IS NOT NULL AND p.id IS NULL "
            "AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now()"
        ),
        rows_sql=(
            "SELECT i.id, i.project_id, i.role, "
            f"{project_role} AS project_role, i.expires_at "
            "FROM user_invitations i LEFT JOIN projects p ON p.id = i.project_id "
            "WHERE i.project_id IS NOT NULL AND p.id IS NULL "
            "AND i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now() "
            "ORDER BY i.id LIMIT :max_rows"
        ),
        max_rows=max_rows,
    )
    # Legacy pending project invitations keep their project role in the platform
    # ``role`` column; the migration must move it before converting.  Without
    # the 0173 column every pending project invitation needs the backfill.
    project_role_to_backfill = await _scalar(
        db,
        "SELECT count(*) FROM user_invitations WHERE project_id IS NOT NULL "
        f"{project_role_backfill_filter(capabilities)} "
        "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()",
    )
    if capabilities.get("user_invitations.project_role"):
        account_invitation_with_project_role = await _count_and_rows(
            db,
            count_sql=(
                "SELECT count(*) FROM user_invitations "
                "WHERE project_id IS NULL AND project_role IS NOT NULL "
                "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()"
            ),
            rows_sql=(
                "SELECT id, role, project_role FROM user_invitations "
                "WHERE project_id IS NULL AND project_role IS NOT NULL "
                "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now() "
                "ORDER BY id LIMIT :max_rows"
            ),
            max_rows=max_rows,
        )
    else:
        account_invitation_with_project_role = _limited([], 0, max_rows)
    return {
        "status_counts": status_counts,
        "pending": pending,
        "pending_account_only": {"total": account_only_count},
        "pending_project_target": {"total": project_target_count},
        "pending_deleted_project_target": deleted_targets,
        "pending_project_role_to_backfill": {"total": project_role_to_backfill},
        "account_invitation_with_project_role": account_invitation_with_project_role,
    }


# ---------------------------------------------------------------------------
# Review evidence
# ---------------------------------------------------------------------------


async def audit_review_evidence(
    db: AsyncSession, *, capabilities: dict[str, bool], max_rows: int
) -> dict:
    predicates = review_evidence_predicates(capabilities)
    columns = review_evidence_columns(capabilities)
    detail = (
        "SELECT id AS task_id, project_id, review_round_id, "
        f"{columns['has_submitter']}, "
        f"{columns['review_contributor_kind']}, "
        f"{columns['annotation_contributor_kind']} FROM tasks "
    )

    async def category(name: str) -> dict:
        return await _count_and_rows(
            db,
            count_sql="SELECT count(*) FROM tasks WHERE " + predicates[name],
            rows_sql=detail
            + "WHERE "
            + predicates[name]
            + " ORDER BY id LIMIT :max_rows",
            max_rows=max_rows,
        )

    complete = await category("complete")
    incomplete = await category("incomplete")
    incomplete_reasons = {
        name: {
            "total": await _scalar(
                db, "SELECT count(*) FROM tasks WHERE " + predicates[name]
            )
        }
        for name in (
            "missing_round",
            "missing_submitter",
            "malformed_review_array",
            "unknown_accumulator",
            "missing_contributors",
        )
    }
    # A task without review activity has no evidence to freeze yet; NULL there
    # is expected, not a defect (no default marks a legacy writer's row known).
    no_review_activity = await _scalar(
        db,
        "SELECT count(*) FROM tasks WHERE NOT (" + _REVIEW_ACTIVITY_SQL + ")",
    )
    return {
        "complete": complete,
        "incomplete": incomplete,
        "incomplete_reasons": incomplete_reasons,
        "no_review_activity": {"total": no_review_activity},
    }


# ---------------------------------------------------------------------------
# Reconciliation totals
# ---------------------------------------------------------------------------


async def audit_reconciliation(db: AsyncSession) -> dict:
    async def grouped(sql: str, key: str) -> dict:
        return {
            (row[key] if row[key] is not None else "unknown"): int(row["count"])
            for row in await _fetch(db, sql)
        }

    users_by_role = await grouped(
        "SELECT role, count(*) AS count FROM users GROUP BY role", "role"
    )
    memberships_by_role = await grouped(
        "SELECT role, count(*) AS count FROM project_members GROUP BY role", "role"
    )
    tasks_by_status = await grouped(
        "SELECT status, count(*) AS count FROM tasks GROUP BY status", "status"
    )
    projects_by_status = await grouped(
        "SELECT status, count(*) AS count FROM projects GROUP BY status", "status"
    )
    annotations_by_source = await grouped(
        "SELECT source, count(*) AS count FROM annotations GROUP BY source", "source"
    )
    first_review_by_result = await grouped(
        "SELECT first_review_result, count(*) AS count FROM tasks "
        "WHERE first_review_result IS NOT NULL GROUP BY first_review_result",
        "first_review_result",
    )
    annotations_by_author = await _fetch(
        db,
        "SELECT user_id, count(*) AS active_annotations FROM annotations "
        "WHERE is_active IS TRUE AND was_cancelled IS FALSE AND user_id IS NOT NULL "
        "GROUP BY user_id ORDER BY user_id",
    )
    return {
        "users": {
            "total": await _scalar(db, "SELECT count(*) FROM users"),
            "active": await _scalar(
                db, "SELECT count(*) FROM users WHERE is_active IS TRUE"
            ),
            "inactive": await _scalar(
                db, "SELECT count(*) FROM users WHERE is_active IS NOT TRUE"
            ),
            "by_role": users_by_role,
        },
        "memberships": {
            "total": await _scalar(db, "SELECT count(*) FROM project_members"),
            "by_role": memberships_by_role,
        },
        "projects": {
            "total": await _scalar(db, "SELECT count(*) FROM projects"),
            "by_status": projects_by_status,
        },
        "tasks": {
            "total": await _scalar(db, "SELECT count(*) FROM tasks"),
            "by_status": tasks_by_status,
        },
        "assignments": {
            "with_effective_assignee": await _scalar(
                db,
                _EFFECTIVE_ASSIGNMENT_CTE + "SELECT count(*) FROM effective e "
                "WHERE e.effective_assignee_id IS NOT NULL",
            ),
            "with_effective_reviewer": await _scalar(
                db,
                _EFFECTIVE_ASSIGNMENT_CTE + "SELECT count(*) FROM effective e "
                "WHERE e.effective_reviewer_id IS NOT NULL",
            ),
            "unassigned_not_completed": await _scalar(
                db,
                _EFFECTIVE_ASSIGNMENT_CTE + "SELECT count(*) FROM effective e "
                "WHERE e.effective_assignee_id IS NULL AND e.status <> 'completed'",
            ),
            "review_claimed_not_completed": await _scalar(
                db,
                "SELECT count(*) FROM tasks "
                "WHERE reviewer_claimed_at IS NOT NULL AND status = 'review'",
            ),
        },
        "locks": {
            "total": await _scalar(db, "SELECT count(*) FROM task_locks"),
            "unexpired": await _scalar(
                db, "SELECT count(*) FROM task_locks WHERE expire_at > now()"
            ),
        },
        "annotations": {
            "total": await _scalar(db, "SELECT count(*) FROM annotations"),
            "active": await _scalar(
                db,
                "SELECT count(*) FROM annotations "
                "WHERE is_active IS TRUE AND was_cancelled IS FALSE",
            ),
            "cancelled": await _scalar(
                db, "SELECT count(*) FROM annotations WHERE was_cancelled IS TRUE"
            ),
            "distinct_authors": await _scalar(
                db,
                "SELECT count(DISTINCT user_id) FROM annotations "
                "WHERE user_id IS NOT NULL",
            ),
            "by_source": annotations_by_source,
        },
        "historical_review": {
            "first_reviewed": await _scalar(
                db, "SELECT count(*) FROM tasks WHERE first_reviewed_at IS NOT NULL"
            ),
            "by_first_review_result": first_review_by_result,
        },
        "annotations_by_author": annotations_by_author,
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_summary(
    *,
    baseline: dict,
    roles: dict,
    assignments: dict,
    invitations: dict,
    evidence: dict,
) -> dict:
    """Derive the headline counts the deployer triages first (pure function)."""
    return {
        "schema_in_sync": baseline["schema_in_sync"],
        "preparation_columns_present": all(baseline["schema_capabilities"].values()),
        "unknown_platform_roles": roles["unknown_platform_roles"]["total"],
        "unknown_member_roles": roles["unknown_member_roles"]["total"],
        "legacy_global_member_mismatches": roles["legacy_global_member_mismatches"][
            "total"
        ],
        "cross_project_role_diversity": roles["cross_project_role_diversity"]["total"],
        "viewer_membership_conflicts": roles["viewer_membership_conflicts"]["total"],
        "administrator_memberships": roles["administrator_memberships"]["total"],
        "invalid_administrative_owners": roles["invalid_administrative_owners"][
            "total"
        ],
        "inactive_administrative_owners": roles["inactive_administrative_owners"][
            "total"
        ],
        "annotator_missing_membership": assignments["annotator_missing_membership"][
            "total"
        ],
        "reviewer_missing_membership": assignments["reviewer_missing_membership"][
            "total"
        ],
        "annotator_wrong_membership_role": assignments[
            "annotator_wrong_membership_role"
        ]["total"],
        "reviewer_wrong_membership_role": assignments["reviewer_wrong_membership_role"][
            "total"
        ],
        "assignment_to_inactive_account": assignments["assignment_to_inactive_account"][
            "total"
        ],
        "inactive_accounts_with_unfinished_work": assignments[
            "inactive_accounts_with_unfinished_work"
        ]["total"],
        "pending_invitations": invitations["status_counts"]["pending"],
        "pending_deleted_project_target": invitations["pending_deleted_project_target"][
            "total"
        ],
        "pending_project_role_to_backfill": invitations[
            "pending_project_role_to_backfill"
        ]["total"],
        "account_invitation_with_project_role": invitations[
            "account_invitation_with_project_role"
        ]["total"],
        "review_evidence_complete": evidence["complete"]["total"],
        "review_evidence_incomplete": evidence["incomplete"]["total"],
    }


def _walk_sections(section: dict) -> list[tuple[str, dict]]:
    found: list[tuple[str, dict]] = []
    for name, value in section.items():
        if isinstance(value, dict) and "truncated" in value and value["truncated"]:
            found.append((name, value))
    return found


def collect_limits(*sections: dict, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Return the ``limits`` block for any truncated entity list."""
    truncated = sorted(
        {name for section in sections for name, _ in _walk_sections(section)}
    )
    return {"max_rows_per_section": max_rows, "truncated_sections": truncated}


async def collect_report(
    db: AsyncSession,
    *,
    run_id: str | None = None,
    generated_at: datetime | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict:
    """Compose the report from read-only queries.

    This is a composable helper: it does not set transaction isolation itself.
    The CLI wraps it in :func:`read_only_audit_session`; fixture-based tests may
    call it directly after reading existing rows.
    """
    run_id = run_id or str(uuid.uuid4())
    generated_at = generated_at or datetime.now(timezone.utc)
    capabilities = await detect_schema_capabilities(db)
    baseline = await audit_schema_baseline(db, capabilities)
    roles = await audit_role_discrepancies(db, max_rows=max_rows)
    assignments = await audit_assignment_gaps(db, max_rows=max_rows)
    invitations = await audit_invitations(
        db, capabilities=capabilities, max_rows=max_rows
    )
    evidence = await audit_review_evidence(
        db, capabilities=capabilities, max_rows=max_rows
    )
    reconciliation = await audit_reconciliation(db)
    summary = build_summary(
        baseline=baseline,
        roles=roles,
        assignments=assignments,
        invitations=invitations,
        evidence=evidence,
    )
    return {
        "report_version": AUDIT_REPORT_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "read_only": True,
        "baseline": baseline,
        "limits": collect_limits(
            roles, assignments, invitations, evidence, max_rows=max_rows
        ),
        "summary": summary,
        "findings": {
            "role_discrepancies": roles,
            "assignment_membership_gaps": assignments,
            "invitations": invitations,
            "review_evidence": evidence,
        },
        "reconciliation": reconciliation,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# REPEATABLE READ gives totals and detail rows one stable audit snapshot;
# READ ONLY is enforced by PostgreSQL, not only by this module.
_READ_ONLY_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


@asynccontextmanager
async def read_only_audit_session(engine: AsyncEngine):
    """Yield a session inside a PostgreSQL-enforced read-only transaction."""
    sessionmaker = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    async with sessionmaker() as db:
        await db.execute(text(_READ_ONLY_TRANSACTION_SQL))
        try:
            yield db
        finally:
            await db.rollback()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


async def _run(
    database_url: str, *, max_rows: int, output: str | None, pretty: bool
) -> int:
    engine = create_async_engine(database_url, echo=False)
    try:
        async with read_only_audit_session(engine) as db:
            report = await collect_report(db, max_rows=max_rows)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"audit failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    rendered = json.dumps(
        report,
        default=_json_default,
        ensure_ascii=False,
        indent=2 if pretty else None,
    )
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="read-only audit target; defaults to settings.database_url",
    )
    parser.add_argument("--output", help="write the JSON report to this path")
    parser.add_argument(
        "--max-rows",
        type=_positive_int,
        default=DEFAULT_MAX_ROWS,
        help="positive cap on entity rows per finding (counts stay exact)",
    )
    parser.add_argument("--pretty", action="store_true", help="indent output")
    args = parser.parse_args()

    database_url = args.database_url
    if database_url is None:
        from app.config import settings

        database_url = settings.database_url

    sys.exit(
        asyncio.run(
            _run(
                database_url,
                max_rows=args.max_rows,
                output=args.output,
                pretty=args.pretty,
            )
        )
    )


if __name__ == "__main__":
    main()
