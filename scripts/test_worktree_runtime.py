"""Worktree lifecycle regression tests, independent of API test databases."""

import importlib.util
import unittest
import tempfile
from pathlib import Path


class MigrationGuardTests(unittest.TestCase):
    def runtime(self):
        self.assertIsNotNone(
            importlib.util.find_spec("worktree_runtime"),
            "A runtime owner must validate revisions before migration",
        )
        import worktree_runtime

        return worktree_runtime

    def test_unknown_database_revision_is_rejected_even_when_skipping(self):
        runtime = self.runtime()
        for skip in (False, True):
            with self.subTest(skip=skip), self.assertRaisesRegex(ValueError, "0167"):
                runtime.validate_revisions(["0165"], ["0165"], ["0167"], skip=skip)

    def test_duplicate_and_multiple_heads_fail_before_connecting(self):
        runtime = self.runtime()
        for revisions, heads in ((["a", "a"], ["a"]), (["a", "b"], ["a", "b"])):
            with self.subTest(heads=heads), self.assertRaises(ValueError):
                runtime.validate_revisions(revisions, heads, [], skip=False)

    def test_skip_requires_database_at_the_actual_checkout_head(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.validate_revisions(["a", "b"], ["b"], ["a"], skip=True)
        runtime.validate_revisions(["a", "b"], ["b"], ["b"], skip=True)
        runtime.validate_revisions(["a", "b"], ["b"], [], skip=False)


class OwnershipTests(unittest.TestCase):
    def test_present_but_untagged_resources_cannot_be_adopted(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "require_owner"))
        self.assertFalse(runtime.require_owner("database", None, "ours"))
        self.assertTrue(runtime.require_owner("database", {"owner": "ours"}, "ours"))
        for owner in (None, "other"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                runtime.require_owner("database", {"owner": owner}, "ours")

    def test_destructive_confirmation_is_exact_and_foreign_inventory_blocks_all(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "validate_destruction"))
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            inventory = {
                "database": {"owner": resources["owner"], "sessions": 0},
                "redis": None,
                "buckets": {},
            }
            for confirmation in (None, "yes", resources["id"]):
                with self.assertRaises(ValueError):
                    runtime.validate_destruction(resources, inventory, confirmation)
            runtime.validate_destruction(
                resources, inventory, resources["confirmation"]
            )
            inventory["buckets"]["foreign"] = {"owner": "other"}
            with self.assertRaises(ValueError):
                runtime.validate_destruction(
                    resources, inventory, resources["confirmation"]
                )
            inventory["buckets"].clear()
            inventory["database"]["sessions"] = 1
            with self.assertRaises(ValueError):
                runtime.validate_destruction(
                    resources, inventory, resources["confirmation"]
                )

    def test_active_environment_lock_is_exclusive(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "environment_lock"))
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            with runtime.environment_lock(resources):
                with self.assertRaisesRegex(ValueError, "使用中"):
                    with runtime.environment_lock(resources):
                        self.fail("Two owners acquired the same environment")
            with runtime.environment_lock(resources):
                pass


class CommandTests(unittest.TestCase):
    def test_third_party_value_errors_cannot_print_connection_secrets(self):
        import worktree_runtime as runtime

        message = runtime.safe_error(
            ValueError("postgresql://user:fixture-secret@localhost/db")
        )
        self.assertNotIn("fixture-secret", message)

    def test_doctor_does_not_report_starting_services_as_healthy(self):
        from unittest.mock import AsyncMock, Mock, patch
        import worktree_runtime as runtime
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            backend = Mock(resources=resources, code_root=root)
            backend.database = AsyncMock(
                return_value={"owner": resources["owner"], "sessions": 0}
            )
            backend.revisions = AsyncMock(return_value=["a"])
            backend.redis_info.return_value = {
                "owner": resources["owner"],
                "running": True,
                "port": 6391,
            }
            backend.bucket_info.return_value = {"owner": resources["owner"]}
            with (
                patch(
                    "worktree_resources.migration_graph", return_value=(["a"], ["a"])
                ),
                patch.object(
                    runtime,
                    "session_record",
                    return_value={
                        "command": "up",
                        "children": [],
                        "worker_requested": True,
                        "worker": "ordinary@local",
                    },
                ),
                patch.object(runtime, "process_matches", return_value=True),
                patch("redis.Redis"),
            ):
                report = runtime.diagnose(backend)
                self.assertFalse(report["healthy"])
                self.assertEqual(report["state"], "starting")
                self.assertEqual(report["worker"], "ordinary@local")
                self.assertEqual(report["maintenance_worker"], "starting")

    def test_cli_help_exposes_lifecycle_commands_without_connecting(self):
        import subprocess
        import sys

        script = Path(__file__).with_name("worktree_runtime.py")
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("doctor", result.stdout)
        self.assertIn("--confirm", result.stdout)

    def test_command_modes_and_exec_separator_are_explicit(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "parse_arguments"))
        default = runtime.parse_arguments([])
        self.assertEqual((default.command, default.mode), ("up", "dev"))
        command = runtime.parse_arguments(
            ["exec", "--mode", "e2e", "--with-worker", "--", "pnpm", "test:e2e"]
        )
        self.assertEqual(command.execute, ["pnpm", "test:e2e"])
        self.assertEqual(command.mode, "e2e")
        self.assertTrue(command.with_worker)
        self.assertEqual(
            runtime.parse_arguments(["exec", "--", "python", "-V"]).mode, "test"
        )
        self.assertEqual(
            runtime.parse_arguments(["--", "--api-port", "8300"]).api_port, 8300
        )
        with self.assertRaises(SystemExit):
            runtime.parse_arguments(["exec"])
        with self.assertRaises(SystemExit):
            runtime.parse_arguments(["--api-port", "0"])

    def test_process_identity_rejects_pid_reuse_and_other_checkouts(self):
        import worktree_runtime as runtime

        self.assertTrue(hasattr(runtime, "process_matches"))
        from unittest.mock import patch

        record = {"pid": 123, "started": "start-a"}
        with patch.object(
            runtime,
            "process_identity",
            return_value={"started": "start-b", "command": "/ours/worktree_runtime.py"},
        ):
            self.assertFalse(
                runtime.process_matches(record, "/ours/worktree_runtime.py")
            )

        with patch.object(
            runtime,
            "process_identity",
            return_value={
                "started": "start-a",
                "command": "/theirs/worktree_runtime.py",
            },
        ):
            self.assertFalse(
                runtime.process_matches(record, "/ours/worktree_runtime.py")
            )

    def test_manual_scenario_is_limited_to_e2e_up(self):
        import worktree_runtime as runtime

        options = runtime.parse_arguments(
            ["up", "--mode", "e2e", "--scenario", "filtering"]
        )
        self.assertEqual(options.scenario, "filtering")
        for args in (
            ["--scenario", "filtering"],
            ["up", "--mode", "test", "--scenario", "filtering"],
            ["reset", "--mode", "e2e", "--scenario", "filtering"],
            ["exec", "--mode", "e2e", "--scenario", "filtering", "--", "true"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                runtime.parse_arguments(args)


class WorkerQueueTests(unittest.TestCase):
    def test_only_own_direct_inbox_is_accepted_in_addition_to_exact_queues(self):
        from worktree_runtime import worker_queues_match
        from celery.utils.nodenames import worker_direct

        node = "owned@local"
        queues = [{"name": "default"}, {"name": "media"}]
        self.assertTrue(worker_queues_match(node, "default,media", queues))
        self.assertTrue(
            worker_queues_match(
                node, "default,media", queues + [{"name": worker_direct(node).name}]
            )
        )
        self.assertFalse(
            worker_queues_match(
                node,
                "default,media",
                queues + [{"name": worker_direct("other@local").name}],
            )
        )
        self.assertFalse(worker_queues_match(node, "default,media", queues[:1]))


if __name__ == "__main__":
    unittest.main()
