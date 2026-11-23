"""Unit tests for the migration-chain policy and the ownership guards.

These are pure-logic tests (no database, no Alembic environment): they pin the
fail-closed behaviour of ``alembic_migration_policy.classify_chain`` and the
refusals in ``validate_migrations.resolve_anchor``.  Run with the API venv::

    apps/api/.venv/bin/python scripts/test_alembic_migration_policy.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alembic_migration_policy import (  # noqa: E402
    Revision,
    UnsupportedChain,
    classify_chain,
    require_single_head,
)
from validate_migrations import (  # noqa: E402
    ValidationError,
    create_action,
    derived_database,
    drop_action,
    assert_forward_conversion,
    phase_forward,
    resolve_anchor,
    RunState,
)


def rev(revision: str, down) -> Revision:
    return Revision(revision=revision, down_revision=down)


class ClassifyChainTests(unittest.TestCase):
    def test_complete_forward_path_is_ordered_from_base_to_head(self):
        revisions = {
            "bo0458": rev("bo0458", "0174"),
            "0174": rev("0174", "0173"),
            "0173": rev("0173", None),
        }
        policy = classify_chain(revisions, "bo0458")
        self.assertEqual(policy.head, "bo0458")
        self.assertEqual(policy.upgrade_path, ("0173", "0174", "bo0458"))

    def test_single_revision_includes_the_real_base_upgrade(self):
        policy = classify_chain({"0001": rev("0001", None)}, "0001")
        self.assertEqual(policy.upgrade_path, ("0001",))

    def test_single_parent_tuple_is_supported(self):
        policy = classify_chain(
            {"0002": rev("0002", ("0001",)), "0001": rev("0001", None)}, "0002"
        )
        self.assertEqual(policy.upgrade_path, ("0001", "0002"))

    def test_unknown_head_fails_closed(self):
        with self.assertRaises(UnsupportedChain):
            classify_chain({"0001": rev("0001", None)}, "missing")

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


class ForwardConversionTests(unittest.IsolatedAsyncioTestCase):
    async def check_conversion(
        self,
        *,
        roles=("employee", "employee"),
        inactive=False,
        memberships=0,
        member_role="annotator",
        pending=None,
        populated=None,
        accepted=None,
        default="'employee'",
    ):
        with (
            patch(
                "validate_migrations.fetch_scalar",
                new_callable=AsyncMock,
                side_effect=[*roles, inactive, memberships, member_role],
            ),
            patch(
                "validate_migrations.fetch_all",
                new_callable=AsyncMock,
                side_effect=[
                    [pending or {"role": "employee", "project_role": "annotator"}],
                    [populated or {"role": "employee", "project_role": "annotator"}],
                    [accepted or {"role": "annotator", "project_role": None}],
                ],
            ),
            patch(
                "validate_migrations.column_default",
                new_callable=AsyncMock,
                return_value=default,
            ),
        ):
            await assert_forward_conversion(
                None,
                "owned_test",
                dict.fromkeys(
                    ("anno", "rev", "member", "pending", "populated", "accepted")
                ),
            )

    async def test_converted_accounts_preserve_membership_and_invitation_history(self):
        await self.check_conversion()

    async def test_lost_roles_activation_or_membership_semantics_are_rejected(self):
        for change in (
            {"roles": ("annotator", "employee")},
            {"roles": ("employee", "reviewer")},
            {"inactive": True},
            {"memberships": 1},
            {"member_role": "reviewer"},
            {"default": "'annotator'"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                await self.check_conversion(**change)

    async def test_lost_or_rewritten_invitation_facts_are_rejected(self):
        for change in (
            {"pending": {"role": "annotator", "project_role": None}},
            {"populated": {"role": "employee", "project_role": "reviewer"}},
            {"accepted": {"role": "employee", "project_role": None}},
            {"accepted": {"role": "annotator", "project_role": "annotator"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                await self.check_conversion(**change)


class ForwardPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_upgrades_cannot_publish_a_completed_phase(self):
        policy = classify_chain(
            {
                "bo0458": rev("bo0458", "0174"),
                "0174": rev("0174", "0173"),
                "0173": rev("0173", None),
            },
            "bo0458",
        )
        anchor = resolve_anchor(
            {
                "MIGRATION_DATABASE_URL": AnchorGuardTests.base,
                "AAP_MIGRATION_VALIDATION_OWNER": "ci:forward-test",
            }
        )
        for revisions in (
            [["0172"]],
            [["0173"], ["0173"]],
            [["0173"], ["0174"], ["bo0457"]],
        ):
            state = RunState()
            with (
                self.subTest(revisions=revisions),
                patch("validate_migrations.graph_policy", return_value=policy),
                patch(
                    "validate_migrations.create_owned_database", new_callable=AsyncMock
                ),
                patch(
                    "validate_migrations.assert_sentinel_columns",
                    new_callable=AsyncMock,
                ),
                patch(
                    "validate_migrations.column_default",
                    new_callable=AsyncMock,
                    return_value="'annotator'",
                ),
                patch("validate_migrations.seed_legacy_rows", new_callable=AsyncMock),
                patch(
                    "validate_migrations.assert_forward_conversion",
                    new_callable=AsyncMock,
                ),
                patch(
                    "validate_migrations.current_revision",
                    new_callable=AsyncMock,
                    side_effect=revisions,
                ),
                patch("validate_migrations.run_alembic"),
            ):
                with self.assertRaises(ValidationError):
                    await phase_forward(anchor, state)
                self.assertEqual(state.phases, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
