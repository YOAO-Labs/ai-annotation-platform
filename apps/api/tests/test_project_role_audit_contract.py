"""Pure contract checks for the read-only project-role audit.

No database is required; these tests pin the versioned report contract, the
pre-migration NULL projections and the Alembic graph invariants so later
increments cannot silently drop preparation handling or change the version.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.audit_project_roles import (
    AUDIT_REPORT_VERSION,
    PREPARATION_COLUMNS,
    _UUID_TEXT_REGEX,
    _json_default,
    _limited,
    build_summary,
    collect_limits,
    project_role_backfill_filter,
    project_role_projection,
    review_evidence_columns,
    review_evidence_predicates,
    script_heads,
)

_ALL_CAPABILITIES = tuple(
    f"{table}.{column}"
    for table, columns in PREPARATION_COLUMNS.items()
    for column in columns
)

_MEMBERSHIP_KEYS = (
    "unknown_platform_roles",
    "unknown_member_roles",
    "legacy_global_member_mismatches",
    "cross_project_role_diversity",
    "viewer_membership_conflicts",
    "administrator_memberships",
    "invalid_administrative_owners",
    "inactive_administrative_owners",
)
_ASSIGNMENT_KEYS = (
    "annotator_missing_membership",
    "annotator_wrong_membership_role",
    "reviewer_missing_membership",
    "reviewer_wrong_membership_role",
    "assignment_to_inactive_account",
    "inactive_accounts_with_unfinished_work",
)


def test_report_version_is_explicit() -> None:
    assert AUDIT_REPORT_VERSION == "project-role-audit/2"


def test_script_graph_has_single_head_and_preparation_revision() -> None:
    heads = script_heads()
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"

    api_root = Path(__file__).resolve().parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    revisions = {
        revision.revision
        for revision in ScriptDirectory.from_config(config).walk_revisions()
    }
    # Later increments may extend the chain; 0173 must never be dropped.
    assert "0173" in revisions


def test_pre_migration_projections_are_null_not_columns() -> None:
    none_present = {key: False for key in _ALL_CAPABILITIES}
    assert project_role_projection(none_present) == "NULL::varchar"
    assert project_role_backfill_filter(none_present) == ""

    predicates = review_evidence_predicates(none_present)
    for name in ("complete", "missing_submitter", "malformed_review_array"):
        assert "NULL::uuid" in predicates[name] or "NULL::jsonb" in predicates[name]
    for absent in (
        "review_submitter_id",
        "review_contributor_ids",
        "annotation_contributor_ids",
    ):
        assert absent not in predicates["complete"]

    columns = review_evidence_columns(none_present)
    assert columns["has_submitter"] == "false AS has_submitter"
    assert columns["review_contributor_kind"] == "NULL::text AS review_contributor_kind"
    assert (
        columns["annotation_contributor_kind"]
        == "NULL::text AS annotation_contributor_kind"
    )


def test_post_migration_predicates_require_round_submitter_and_accummulator() -> None:
    present = {key: True for key in _ALL_CAPABILITIES}
    assert project_role_projection(present) == "i.project_role"
    assert project_role_backfill_filter(present) == "AND project_role IS NULL"

    predicates = review_evidence_predicates(present)
    assert "review_submitter_id) IS NOT NULL" in predicates["complete"]
    assert "jsonb_typeof(review_contributor_ids) = 'array'" in predicates["complete"]
    assert (
        "jsonb_typeof(annotation_contributor_ids) = 'array'" in predicates["complete"]
    )
    # The frozen set must include the submitter and every accumulator ID.
    assert "@>" in predicates["missing_contributors"]
    assert "to_jsonb((review_submitter_id)::text)" in predicates["missing_contributors"]
    assert "(annotation_contributor_ids)" in predicates["missing_contributors"]
    # A non-NULL frozen array alone is not complete: every incomplete reason
    # participates in the incomplete union.
    assert predicates["missing_submitter"] in predicates["incomplete"]
    assert predicates["unknown_accumulator"] in predicates["incomplete"]
    assert predicates["missing_contributors"] in predicates["incomplete"]


def test_review_predicates_validate_uuid_elements() -> None:
    present = {key: True for key in _ALL_CAPABILITIES}
    predicates = review_evidence_predicates(present)
    malformed = predicates["malformed_review_array"]
    assert "jsonb_array_elements_text(" in malformed
    assert "jsonb_typeof(review_contributor_ids)" in malformed
    assert "element.value IS NULL" in malformed
    assert _UUID_TEXT_REGEX in malformed
    unknown = predicates["unknown_accumulator"]
    assert "jsonb_array_elements_text(" in unknown
    assert "jsonb_typeof(annotation_contributor_ids)" in unknown


def test_limited_marks_truncation() -> None:
    rows = [{"id": 1}, {"id": 2}]
    assert _limited(rows, 5, 10) == {
        "total": 5,
        "returned": 2,
        "truncated": True,
        "items": rows,
    }
    assert _limited(rows, 2, 10)["truncated"] is False


def test_collect_limits_reports_only_truncated_sections() -> None:
    section = {"pending": _limited([{"id": 1}], 3, 1), "summary": {"total": 0}}
    limits = collect_limits(section, max_rows=1)
    assert limits == {"max_rows_per_section": 1, "truncated_sections": ["pending"]}


def test_json_default_serializes_uuid_and_datetime() -> None:
    value = uuid.uuid4()
    moment = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert json.loads(json.dumps({"id": value}, default=_json_default)) == {
        "id": str(value)
    }
    assert _json_default(moment) == "2026-09-19T12:00:00+00:00"


def test_build_summary_projects_each_finding() -> None:
    baseline = {
        "schema_in_sync": True,
        "schema_capabilities": {key: True for key in _ALL_CAPABILITIES},
    }
    roles = {key: {"total": index} for index, key in enumerate(_MEMBERSHIP_KEYS)}
    assignments = {key: {"total": 100 + i} for i, key in enumerate(_ASSIGNMENT_KEYS)}
    invitations = {
        "status_counts": {"pending": 7},
        "pending_deleted_project_target": {"total": 2},
        "pending_project_role_to_backfill": {"total": 3},
        "account_invitation_with_project_role": {"total": 4},
    }
    evidence = {"complete": {"total": 4}, "incomplete": {"total": 11}}

    summary = build_summary(
        baseline=baseline,
        roles=roles,
        assignments=assignments,
        invitations=invitations,
        evidence=evidence,
    )

    assert summary["schema_in_sync"] is True
    assert summary["preparation_columns_present"] is True
    assert summary["unknown_platform_roles"] == 0
    assert summary["legacy_global_member_mismatches"] == 2
    assert summary["cross_project_role_diversity"] == 3
    assert summary["inactive_administrative_owners"] == 7
    assert summary["annotator_missing_membership"] == 100
    assert summary["inactive_accounts_with_unfinished_work"] == 105
    assert summary["pending_invitations"] == 7
    assert summary["pending_deleted_project_target"] == 2
    assert summary["pending_project_role_to_backfill"] == 3
    assert summary["account_invitation_with_project_role"] == 4
    assert summary["review_evidence_complete"] == 4
    assert summary["review_evidence_incomplete"] == 11
    json.dumps(summary)
