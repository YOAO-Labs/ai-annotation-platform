"""Guards and measurement helpers; no external database or storage is touched."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from benchmark_dataset_scan import (
    build_tree,
    check_database_identity,
    check_worker_environment,
    environment_contract,
    tree_fingerprint,
)
from dataset_scan_benchmark_worker import ObservedIterator
from collections import Counter
from worktree_env import topology


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        identity = {"schema": 1, "id": "0123456789abcdef", "root": str(self.root)}
        self.resources = topology(self.root, identity, "e2e")
        directory = self.root / ".worktree/e2e"
        directory.mkdir(parents=True)
        (directory.parent / "identity.json").write_text(json.dumps(identity))
        self.manifest = {
            "resources": self.resources,
            "postgres": {"host": "127.0.0.1", "port": 5432},
        }
        (directory / "resources.json").write_text(json.dumps(self.manifest))
        self.environment = {
            "AAP_WORKTREE_MODE": "e2e",
            "DATABASE_URL": f"postgresql+asyncpg://user:fixture@127.0.0.1/{self.resources['database']}",
            **self.resources["buckets"],
        }

    def test_exact_owned_target_accepted(self):
        self.assertEqual(
            environment_contract(self.root, self.environment), self.resources
        )
        check_database_identity(
            self.resources, self.resources["database"], self.resources["owner"]
        )

    def test_rejects_other_modes_missing_buckets_and_other_databases(self):
        for overrides in [
            {"AAP_WORKTREE_MODE": "dev"},
            {"AAP_WORKTREE_MODE": "test"},
            {"MINIO_TRAINING_BUCKET": "everyday-training"},
            {"DATABASE_URL": "postgresql+asyncpg://user@127.0.0.1/annotation"},
            {
                "DATABASE_URL": f"postgresql+asyncpg://user@remote/{self.resources['database']}"
            },
            {
                "DATABASE_URL": f"postgresql+asyncpg://user@127.0.0.1:6543/{self.resources['database']}"
            },
        ]:
            with self.subTest(overrides=list(overrides)):
                with self.assertRaises(ValueError):
                    environment_contract(self.root, self.environment | overrides)

    def test_rejects_copied_resource_manifest_and_unknown_live_owner(self):
        self.manifest["resources"] = self.resources | {"owner": "another-checkout"}
        (self.root / ".worktree/e2e/resources.json").write_text(
            json.dumps(self.manifest)
        )
        with self.assertRaises(ValueError):
            environment_contract(self.root, self.environment)
        for database, owner in [
            ("annotation", self.resources["owner"]),
            (self.resources["database"], None),
        ]:
            with self.subTest(database=database):
                with self.assertRaises(ValueError):
                    check_database_identity(self.resources, database, owner)

    def test_fixture_preserves_same_names_exclusions_and_deep_directories(self):
        entries, fingerprint = build_tree(self.root / "fixture", 1002)
        self.assertEqual(sum(kind == "file" for kind in entries.values()), 1002)
        self.assertGreater(sum(kind == "directory" for kind in entries.values()), 1)
        names = [Path(path).name for path, kind in entries.items() if kind == "file"]
        self.assertLess(len(set(names)), len(names))
        self.assertTrue(any(name.endswith(".tmp") for name in names))
        self.assertEqual(
            (entries, fingerprint), tree_fingerprint(self.root / "fixture")
        )

    def test_flat_budget_fixture_really_contains_5001_distinct_files(self):
        entries, _ = build_tree(self.root / "flat", 5001, flat=True)
        self.assertEqual(len(entries), 5001)
        self.assertTrue(all(kind == "file" for kind in entries.values()))

    def test_worker_rejects_each_foreign_broker_or_owner_credential(self):
        expected = self.environment | {
            key: "owned"
            for key in [
                "REDIS_URL",
                "CELERY_BROKER_URL",
                "CELERY_BROKER_READ_URL",
                "CELERY_BROKER_WRITE_URL",
                "CELERY_RESULT_BACKEND",
                "MINIO_ENDPOINT",
                "TMPDIR",
            ]
        }
        check_worker_environment(expected, expected)
        for key in [
            "REDIS_URL",
            "CELERY_BROKER_URL",
            "CELERY_BROKER_READ_URL",
            "CELERY_BROKER_WRITE_URL",
            "CELERY_RESULT_BACKEND",
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "PLAYWRIGHT_E2E_DATABASE_URL",
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_worker_environment(expected | {key: "foreign"}, expected)

    def test_fingerprint_detects_body_change_with_same_size_and_timestamps(self):
        expected = build_tree(self.root / "fixture", 1)
        path = next((self.root / "fixture").rglob("*.tmp"))
        info = path.stat()
        path.write_bytes(b"x" * info.st_size)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(expected[1], tree_fingerprint(self.root / "fixture")[1])

    def test_stat_counter_observes_calls_without_reading_file_body(self):
        path = self.root / "entry.png"
        path.write_bytes(b"damaged image")
        counts = Counter()
        iterator = ObservedIterator(os.scandir(self.root), counts)
        try:
            entry = next(item for item in iterator if item.name == path.name)
            self.assertEqual(counts["entry_stat"], 0)
            self.assertEqual(entry.stat(follow_symlinks=False).st_size, 13)
            self.assertEqual(counts["entry_stat"], 1)
            self.assertEqual(counts["regular_file_opens"], 0)
        finally:
            iterator.close()
        self.assertEqual(path.read_bytes(), b"damaged image")


if __name__ == "__main__":
    unittest.main()
