"""Regression tests for independent review findings; no live services."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from worktree_env import (
    WorktreeError,
    atomic_json,
    isolated_environment,
    load_identity,
    topology,
)
import worktree_env
import worktree_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


class ReviewRegressions(unittest.TestCase):
    def test_database_identity_challenge_uses_both_servers_and_releases_locks(self):
        from sqlalchemy.engine import make_url
        from worktree_resources import LocalResources

        backend = object.__new__(LocalResources)
        backend.settings = Mock(
            database_url="postgresql+asyncpg://runtime:fixture@127.0.0.1/db"
        )
        backend.url = make_url("postgresql+asyncpg://owner:fixture@[::1]/db")
        backend.resources = {"database": "aap_wt_fixture_test"}
        for acquired in (True, False):
            with self.subTest(different_server=acquired):
                owner, runtime_connection = AsyncMock(), AsyncMock()
                # An existing owned database needs no DDL, but still requires identity proof.
                rows = Mock()
                rows.mappings.return_value.first.return_value = {
                    "owner": "fixture",
                    "sessions": 0,
                }
                owner.execute.return_value = rows
                runtime_connection.scalar.return_value = acquired
                engines = [MagicMock(), MagicMock()]
                for engine, connection in zip(engines, (owner, runtime_connection)):
                    engine.connect.return_value.__aenter__.return_value = connection
                    engine.dispose = AsyncMock()
                with patch(
                    "worktree_resources.create_async_engine", side_effect=engines
                ) as create:
                    if acquired:
                        with self.assertRaisesRegex(WorktreeError, "同一个"):
                            asyncio.run(backend.database())
                    else:
                        self.assertIsNotNone(asyncio.run(backend.database()))
                urls = [call.args[0] for call in create.call_args_list]
                self.assertEqual([url.host for url in urls], ["::1", "127.0.0.1"])
                self.assertEqual(
                    [url.database for url in urls], ["postgres", "postgres"]
                )
                calls = owner.execute.call_args_list
                self.assertIn("pg_advisory_lock", str(calls[0].args[0]))
                self.assertIn("pg_advisory_unlock", str(calls[1].args[0]))
                self.assertEqual(
                    calls[0].args[1], runtime_connection.scalar.call_args.args[1]
                )
                self.assertEqual(len(calls), 2 if acquired else 3)
                for engine in engines:
                    engine.dispose.assert_awaited_once()
                owner.exec_driver_sql.assert_not_called()

    def test_worker_handle_refresh_inserts_missing_records_and_replaces_existing(self):
        self.assertTrue(hasattr(runtime, "register_child"))
        record = {"children": []}
        handle = {
            "pid": 123,
            "started": "start",
            "scope": str(ROOT),
            "command_hash": "fixture-old",
        }
        runtime.register_child(record, handle, "worker")
        self.assertEqual(record["children"], [{**handle, "role": "worker"}])
        runtime.register_child(
            record, {**handle, "command_hash": "fixture-new"}, "worker"
        )
        self.assertEqual(len(record["children"]), 1)
        self.assertEqual(record["children"][0]["command_hash"], "fixture-new")
        with self.assertRaises(WorktreeError):
            runtime.register_child(record, None, "worker")

    def test_orphan_guard_blocks_lifecycle_even_without_a_database_connection(self):
        self.assertTrue(hasattr(runtime, "reject_orphans"))
        backend = Mock(resources={"owner": "fixture"}, code_root=ROOT)
        with (
            patch.object(
                runtime, "session_record", return_value={"children": [{"pid": 123}]}
            ),
            patch.object(runtime, "process_matches", return_value=True),
        ):
            with self.assertRaises(WorktreeError):
                runtime.reject_orphans(backend)

    def test_dev_does_not_export_another_modes_test_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = load_identity(root, create=True)
            dev = topology(root, identity, "dev")
            env = isolated_environment(
                dev,
                database_url="postgresql+asyncpg://user:fixture@localhost/db",
                migration_database_url="",
                minio_endpoint="localhost:9000",
                redis_port=6390,
                inherited={},
            )
            for mode in ("test", "e2e"):
                self.assertNotIn(
                    topology(root, identity, mode)["database"], json.dumps(env)
                )
            self.assertEqual(env["AAP_WORKTREE_MODE"], "dev")
            self.assertTrue(
                env["TEST_DATABASE_URL"].startswith("worktree-mode-required:")
            )

    def test_frontend_and_application_environments_drop_unneeded_credentials(self):
        self.assertTrue(hasattr(worktree_env, "frontend_environment"))
        env = {
            "PATH": "/bin",
            "HOME": "/home/fixture",
            "VITE_WS_HOST": "",
            "DATABASE_URL": "runtime-fixture",
            "MIGRATION_DATABASE_URL": "owner-fixture",
            "TEST_DATABASE_URL": "owner-fixture",
            "PLAYWRIGHT_E2E_DATABASE_URL": "owner-fixture",
            "MINIO_SECRET_KEY": "secret-fixture",
            "SOME_PRIVATE_TOKEN": "token-fixture",
        }
        public = worktree_env.frontend_environment(env)
        self.assertEqual(
            public, {"PATH": "/bin", "HOME": "/home/fixture", "VITE_WS_HOST": ""}
        )
        application = worktree_env.application_environment(env)
        self.assertEqual(application["DATABASE_URL"], "runtime-fixture")
        for key in (
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "PLAYWRIGHT_E2E_DATABASE_URL",
        ):
            self.assertEqual(application[key], "")

    def test_process_handles_match_relative_invocation_without_recording_argv(self):
        self.assertTrue(hasattr(runtime, "process_handle"))
        identity = {
            "started": "start-a",
            "command": "python scripts/worktree_runtime.py up",
        }
        with patch.object(runtime, "process_identity", return_value=identity):
            handle = runtime.process_handle(123, str(ROOT))
            self.assertTrue(runtime.process_matches(handle, str(ROOT)))
            self.assertNotIn("command", handle)
            self.assertFalse(runtime.process_matches(handle, str(ROOT / "other")))
        with patch.object(
            runtime,
            "process_identity",
            return_value={**identity, "command": "python other.py"},
        ):
            self.assertFalse(runtime.process_matches(handle, str(ROOT)))

    def test_corrupt_session_record_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            directory = Path(resources["directory"])
            directory.mkdir()
            (directory / "run.json").write_text("{")
            with self.assertRaises(WorktreeError):
                runtime.session_record(resources)

    def test_verified_destroy_releases_the_infrastructure_manifest(self):
        from worktree_resources import LocalResources

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            manifest = Path(resources["directory"]) / "resources.json"
            atomic_json(manifest, {"fixture": True})
            backend = object.__new__(LocalResources)
            backend.root, backend.resources = root, resources
            backend.inspect = Mock(
                return_value={
                    "database": None,
                    "redis": None,
                    "buckets": {name: None for name in resources["buckets"].values()},
                }
            )
            backend.database = AsyncMock(return_value=None)
            backend.owned_runtime_role = AsyncMock(return_value=None)
            backend.redis_info = Mock(return_value=None)
            backend.bucket_info = Mock(return_value=None)
            backend.destroy(resources["confirmation"])
            self.assertFalse(manifest.exists())
            self.assertTrue((root / ".worktree/identity.json").exists())

    def test_pytest_rejects_dev_mode_before_loading_database_fixtures(self):
        result = subprocess.run(
            [sys.executable, "-c", "import runpy; runpy.run_path('tests/conftest.py')"],
            cwd=ROOT / "apps/api",
            env={**os.environ, "AAP_WORKTREE_MODE": "dev"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exec --mode test", result.stderr)


if __name__ == "__main__":
    unittest.main()
