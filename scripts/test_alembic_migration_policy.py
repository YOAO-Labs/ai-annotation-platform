"""Unit tests for the migration-chain policy and the ownership guards.

These are pure-logic tests (no database, no Alembic environment): they pin the
fail-closed behaviour of ``alembic_reversible_floor.classify_chain`` and the
refusals in ``validate_migrations.resolve_anchor``.  Run with the API venv::

    apps/api/.venv/bin/python scripts/test_alembic_migration_policy.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alembic_reversible_floor import (  # noqa: E402
    Revision,
    UnsupportedChain,
    classify_chain,
    require_single_head,
)
from validate_migrations import (  # noqa: E402
    ValidationError,
    check_restored_history,
    create_action,
    derived_database,
    drop_action,
    resolve_anchor,
)


def rev(revision: str, down, irreversible: bool = False) -> Revision:
    return Revision(revision=revision, down_revision=down, irreversible=irreversible)


class ClassifyChainTests(unittest.TestCase):
    def test_single_irreversible_at_head_returns_its_parent(self):
        revisions = {
            "0174": rev("0174", "0173", irreversible=True),
            "0173": rev("0173", "0172"),
            "0172": rev("0172", None),
        }
        policy = classify_chain(revisions, "0174")
        self.assertEqual(policy.reversible_floor, "0173")
        self.assertEqual(policy.irreversible, ("0174",))
        self.assertEqual(policy.reversible_segment, ("0173", "0172"))

    def test_fully_reversible_chain_returns_head(self):
        revisions = {"0002": rev("0002", "0001"), "0001": rev("0001", None)}
        self.assertEqual(classify_chain(revisions, "0002").reversible_floor, "0002")

    def test_reversible_suffix_above_irreversible_fails_closed(self):
        # head -> irreversible is a valid segment, but the runner does not
        # implement suffix validation yet, so it refuses the shape.
        revisions = {
            "0175": rev("0175", "0174"),
            "0174": rev("0174", "0173", irreversible=True),
            "0173": rev("0173", None),
        }
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0175")

    def test_multiple_irreversible_revisions_fail_closed(self):
        revisions = {
            "0175": rev("0175", "0174", irreversible=True),
            "0174": rev("0174", "0173", irreversible=True),
            "0173": rev("0173", None),
        }
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0175")

    def test_irreversible_at_base_fails_closed(self):
        revisions = {"0001": rev("0001", None, irreversible=True)}
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0001")

    def test_merge_revision_fails_closed(self):
        revisions = {
            "0174": rev("0174", "0173"),
            "0173": rev("0173", ("0172", "0172b")),
            "0172": rev("0172", None),
            "0172b": rev("0172b", None),
        }
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0174")

    def test_branch_orphan_fails_closed(self):
        revisions = {
            "0174": rev("0174", "0173"),
            "0173": rev("0173", None),
            "9999": rev("9999", None),
        }
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0174")

    def test_unknown_parent_fails_closed(self):
        revisions = {"0174": rev("0174", "0173")}
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0174")

    def test_cycle_fails_closed(self):
        revisions = {"a": rev("a", "b"), "b": rev("b", "a")}
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "a")


class HeadGuardTests(unittest.TestCase):
    def test_single_head_is_returned(self):
        self.assertEqual(require_single_head(["0174"]), "0174")

    def test_no_head_fails_closed(self):
        with self.assertRaises(UnsupportedChain):
            require_single_head([])

    def test_multiple_heads_fail_closed(self):
        with self.assertRaises(UnsupportedChain):
            require_single_head(["0174", "0174b"])


class AnchorGuardTests(unittest.TestCase):
    base = "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_test"

    def test_ci_target_requires_unique_owner(self):
        with self.assertRaises(ValidationError):
            resolve_anchor({"MIGRATION_DATABASE_URL": self.base})

    def test_ci_target_accepts_explicit_owner(self):
        anchor = resolve_anchor(
            {
                "MIGRATION_DATABASE_URL": self.base,
                "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
            }
        )
        self.assertEqual(anchor.base_database, "annotation_test")
        self.assertEqual(anchor.owner, "ci:123")
        self.assertIsNone(anchor.mode)

    def test_dev_database_is_refused(self):
        with self.assertRaises(ValidationError):
            resolve_anchor(
                {
                    "MIGRATION_DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation",
                    "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
                }
            )

    def test_non_local_host_is_refused(self):
        with self.assertRaises(ValidationError):
            resolve_anchor(
                {
                    "MIGRATION_DATABASE_URL": "postgresql+asyncpg://user:pass@db.example:5432/annotation_test",
                    "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
                }
            )

    def test_production_name_is_refused(self):
        with self.assertRaises(ValidationError):
            resolve_anchor(
                {
                    "MIGRATION_DATABASE_URL": "postgresql+asyncpg://user:pass@127.0.0.1:5432/annotation_prod",
                    "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
                }
            )

    def test_non_postgres_driver_is_refused(self):
        with self.assertRaises(ValidationError):
            resolve_anchor(
                {
                    "MIGRATION_DATABASE_URL": "sqlite:////tmp/x_test.db",
                    "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
                }
            )

    def test_derived_name_and_owner(self):
        anchor = resolve_anchor(
            {
                "MIGRATION_DATABASE_URL": self.base,
                "AAP_MIGRATION_VALIDATION_OWNER": "ci:123",
            }
        )
        self.assertEqual(derived_database(anchor, "fresh"), "annotation_test__mv_fresh")


class DatabaseOwnershipActionTests(unittest.TestCase):
    def test_absent_database_is_created(self):
        self.assertEqual(create_action(False, None, "owner"), "create")

    def test_owned_leftover_is_recreated(self):
        self.assertEqual(create_action(True, "owner", "owner"), "recreate")

    def test_uncommented_existing_database_is_refused(self):
        self.assertEqual(create_action(True, None, "owner"), "refuse")

    def test_foreign_owner_is_refused(self):
        self.assertEqual(create_action(True, "someone-else", "owner"), "refuse")

    def test_drop_absent_is_noop(self):
        self.assertEqual(
            drop_action(False, None, "owner", created_by_run=True), "absent"
        )

    def test_drop_owned(self):
        self.assertEqual(
            drop_action(True, "owner", "owner", created_by_run=True), "drop"
        )

    def test_drop_partial_create_from_this_run(self):
        self.assertEqual(drop_action(True, None, "owner", created_by_run=True), "drop")

    def test_drop_uncommented_foreign_database_is_refused(self):
        self.assertEqual(
            drop_action(True, None, "owner", created_by_run=False), "refuse"
        )

    def test_drop_foreign_owner_is_refused(self):
        self.assertEqual(
            drop_action(True, "someone-else", "owner", created_by_run=True), "refuse"
        )


class RestoredHistoryTests(unittest.TestCase):
    def test_missing_history_row_is_rejected(self):
        with self.assertRaises(ValidationError):
            check_restored_history([])

    def test_single_correct_history_row_is_accepted(self):
        check_restored_history([{"role": "annotator", "project_role": None}])

    def test_wrong_role_is_rejected(self):
        with self.assertRaises(ValidationError):
            check_restored_history([{"role": "employee", "project_role": None}])

    def test_converted_project_role_is_rejected(self):
        with self.assertRaises(ValidationError):
            check_restored_history([{"role": "annotator", "project_role": "annotator"}])

    def test_duplicate_history_rows_are_rejected(self):
        with self.assertRaises(ValidationError):
            check_restored_history(
                [
                    {"role": "annotator", "project_role": None},
                    {"role": "annotator", "project_role": None},
                ]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
