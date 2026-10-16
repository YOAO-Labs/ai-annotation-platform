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
from unittest.mock import AsyncMock, call, patch

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
    phase_forward,
    phase_reversible,
    resolve_anchor,
    RunState,
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

    def test_reversible_suffix_is_separated_from_prefix_at_barrier(self):
        revisions = {
            "0176": rev("0176", "0175"),
            "0175": rev("0175", "0174"),
            "0174": rev("0174", "0173", irreversible=True),
            "0173": rev("0173", None),
        }
        policy = classify_chain(revisions, "0176")
        self.assertEqual(policy.reversible_floor, "0173")
        self.assertEqual(policy.reversible_segment, ("0173",))
        self.assertEqual(policy.post_irreversible_segment, ("0176", "0175"))
        self.assertEqual(policy.irreversible, ("0174",))

    def test_irreversible_at_base_with_suffix_still_fails_closed(self):
        revisions = {
            "0002": rev("0002", "0001"),
            "0001": rev("0001", None, irreversible=True),
        }
        with self.assertRaises(UnsupportedChain):
            classify_chain(revisions, "0002")

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


class SegmentValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = classify_chain(
            {
                "0176": rev("0176", "0175"),
                "0175": rev("0175", "0174"),
                "0174": rev("0174", "0173", irreversible=True),
                "0173": rev("0173", "0172"),
                "0172": rev("0172", None),
            },
            "0176",
        )
        self.anchor = resolve_anchor(
            {
                "MIGRATION_DATABASE_URL": AnchorGuardTests.base,
                "AAP_MIGRATION_VALIDATION_OWNER": "ci:segment-test",
            }
        )

    async def run_reversible(self, *, suffix_output=None, suffix_revision="0174"):
        prefix = "Running downgrade 0173 -> 0172\nRunning downgrade 0172 -> base"
        suffix = (
            "Running downgrade 0176 -> 0175\nRunning downgrade 0175 -> 0174"
            if suffix_output is None
            else suffix_output
        )
        with (
            patch("validate_migrations.graph_policy", return_value=self.policy),
            patch("validate_migrations.create_owned_database", new_callable=AsyncMock),
            patch(
                "validate_migrations.assert_sentinel_columns", new_callable=AsyncMock
            ),
            patch(
                "validate_migrations.column_default", new_callable=AsyncMock
            ) as default,
            patch(
                "validate_migrations.current_revision", new_callable=AsyncMock
            ) as current,
            patch("validate_migrations.run_alembic") as alembic,
        ):
            default.side_effect = ["'annotator'", "'employee'"]
            current.side_effect = [["0173"], [], ["0176"], [suffix_revision], ["0176"]]
            alembic.side_effect = ["", prefix, "", suffix, ""]
            state = RunState()
            await phase_reversible(self.anchor, state)
            return alembic.call_args_list, state

    async def test_both_segments_execute_real_round_trips_without_crossing_barrier(
        self,
    ):
        calls, state = await self.run_reversible()
        database = derived_database(self.anchor, "reversible")
        self.assertEqual(
            calls,
            [
                call(self.anchor, database, "upgrade", "0173"),
                call(self.anchor, database, "downgrade", "base"),
                call(self.anchor, database, "upgrade", "head"),
                call(self.anchor, database, "downgrade", "0174"),
                call(self.anchor, database, "upgrade", "head"),
            ],
        )
        self.assertEqual(len(state.phases), 2)

    async def test_suffix_without_real_downgrades_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "downgrade"):
            await self.run_reversible(suffix_output="")

    async def test_suffix_must_stop_at_exact_barrier(self):
        with self.assertRaisesRegex(ValidationError, "0174"):
            await self.run_reversible(suffix_revision="0173")

    async def test_forward_conversion_and_refusal_stay_at_barrier_before_head(self):
        with (
            patch("validate_migrations.graph_policy", return_value=self.policy),
            patch("validate_migrations.create_owned_database", new_callable=AsyncMock),
            patch(
                "validate_migrations.seed_legacy_rows", new_callable=AsyncMock
            ) as seed,
            patch(
                "validate_migrations.assert_forward_conversion", new_callable=AsyncMock
            ) as check,
            patch(
                "validate_migrations.current_revision", new_callable=AsyncMock
            ) as current,
            patch("validate_migrations.pg_dump_database"),
            patch("validate_migrations.run_alembic") as alembic,
        ):
            current.side_effect = [["0174"], ["0176"]]
            alembic.side_effect = ["", "", "RuntimeError: not reversible", ""]
            seed.return_value = {"authentic": "legacy rows"}
            state = RunState()
            snapshot = await phase_forward(self.anchor, state, object())
            database = derived_database(self.anchor, "forward")
            self.assertEqual(
                alembic.call_args_list,
                [
                    call(self.anchor, database, "upgrade", "0173"),
                    call(self.anchor, database, "upgrade", "0174"),
                    call(self.anchor, database, "downgrade", "0173", check=False),
                    call(self.anchor, database, "upgrade", "head"),
                ],
            )
            self.assertEqual(check.await_count, 3)
            self.assertEqual(state.dumps, [snapshot])


if __name__ == "__main__":
    unittest.main(verbosity=2)
