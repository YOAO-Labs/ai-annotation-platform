"""Pure worktree topology tests; never import API database fixtures."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class IdentityTests(unittest.TestCase):
    def test_identity_is_persistent_checkout_bound_and_modes_are_disjoint(self):
        self.assertIsNotNone(
            importlib.util.find_spec("worktree_env"),
            "The launcher needs an owned, persistent environment topology",
        )
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = load_identity(root, create=True)
            self.assertEqual(first, load_identity(root))
            dev = topology(root, first, "dev")
            test = topology(root, first, "test")
            self.assertNotEqual(dev["database"], test["database"])
            self.assertNotEqual(dev["redis_name"], test["redis_name"])
            self.assertTrue(test["database"].endswith("_test"))
            self.assertTrue(
                set(dev["buckets"].values()).isdisjoint(test["buckets"].values())
            )
            self.assertEqual(len(dev["buckets"]), 8)
            document = json.loads((root / ".worktree/identity.json").read_text())
            self.assertEqual(document["root"], str(root.resolve()))
            self.assertNotIn("password", json.dumps(document).lower())

    def test_rejects_copied_identity_and_symlinked_state(self):
        from worktree_env import load_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            load_identity(first, create=True)
            (second / ".worktree").symlink_to(first / ".worktree")
            with self.assertRaisesRegex(ValueError, "symlink|符号链接"):
                load_identity(second, create=True)
            (second / ".worktree").unlink()
            (second / ".worktree").mkdir()
            (second / ".worktree/identity.json").write_bytes(
                (first / ".worktree/identity.json").read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "checkout|工作树"):
                load_identity(second)

    def test_rejects_malformed_identity_and_modes(self):
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = load_identity(root, create=True)
            path = root / ".worktree/identity.json"
            path.write_text(json.dumps({**identity, "id": "../../annotation"}))
            with self.assertRaises(ValueError):
                load_identity(root)
            with self.assertRaises(ValueError):
                topology(root, identity, "../../shared")

    def test_concurrent_initializers_publish_one_complete_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        from worktree_env import load_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ThreadPoolExecutor(max_workers=8) as pool:
                identities = list(
                    pool.map(lambda _: load_identity(root, create=True), range(32))
                )
            self.assertTrue(all(identity == identities[0] for identity in identities))


class EnvironmentTests(unittest.TestCase):
    def test_overrides_all_mutable_targets_without_changing_shared_settings(self):
        import worktree_env

        self.assertTrue(hasattr(worktree_env, "isolated_environment"))
        from worktree_env import load_identity, topology, isolated_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            inherited = {
                "database_url": "must-not-win",
                "CELERY_BROKER_URL": "redis://shared:6379/0",
                "MINIO_BUCKET": "annotations",
                "DUCKDB_PATH": "/shared/analytics.duckdb",
                "MINIO_PUBLIC_URL": "http://localhost:9000",
                "MINIO_PROXY_TARGET": "http://shared:9000",
                "PATH": "/bin",
            }
            env = isolated_environment(
                resources,
                database_url="postgresql+asyncpg://runtime:fixture@localhost:5432/annotation",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost:5432/annotation",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited=inherited,
            )
            self.assertNotIn("database_url", env)
            self.assertTrue(env["DATABASE_URL"].endswith("/" + resources["database"]))
            self.assertIn("runtime:", env["DATABASE_URL"])
            self.assertIn("owner:", env["MIGRATION_DATABASE_URL"])
            self.assertEqual(env["TEST_DATABASE_URL"], env["MIGRATION_DATABASE_URL"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_URL"])
            self.assertIn("CELERY_RESULT_BACKEND", env)
            self.assertEqual(env["REDIS_URL"], env["CELERY_RESULT_BACKEND"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_READ_URL"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_WRITE_URL"])
            self.assertEqual(env["MINIO_BUCKET"], resources["buckets"]["MINIO_BUCKET"])
            self.assertTrue(env["DUCKDB_PATH"].startswith(resources["directory"]))
            self.assertEqual(env["ML_BACKEND_OBSERVE_URLS"], "[]")
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(inherited["MINIO_BUCKET"], "annotations")
            self.assertIn("PLAYWRIGHT_E2E_DATABASE_URL", env)
            self.assertEqual(
                env["PLAYWRIGHT_E2E_DATABASE_URL"], env["MIGRATION_DATABASE_URL"]
            )
            self.assertEqual(env["ML_BACKEND_ROUTER_MODE"], "off")
            self.assertEqual(env["VITE_WS_HOST"], "")
            self.assertEqual(env["MINIO_PUBLIC_URL"], "/minio")
            self.assertEqual(env["MINIO_PROXY_TARGET"], "http://localhost:9000")

    def test_refuses_remote_or_mismatched_infrastructure_before_provisioning(self):
        import worktree_env

        self.assertTrue(hasattr(worktree_env, "isolated_environment"))
        from worktree_env import load_identity, topology, isolated_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            local = "postgresql+asyncpg://owner:fixture@localhost:5432/annotation"
            for overrides in (
                {
                    "migration_database_url": local.replace(
                        "localhost", "production.example"
                    )
                },
                {"migration_database_url": local.replace("5432", "5433")},
                {"database_url": local + "?host=production.example"},
                {"minio_endpoint": "objects.example:9000"},
            ):
                arguments = dict(
                    database_url=local,
                    migration_database_url=local,
                    minio_endpoint="localhost:9000",
                    redis_port=6391,
                    inherited={},
                )
                arguments.update(overrides)
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    isolated_environment(resources, **arguments)


if __name__ == "__main__":
    unittest.main()
