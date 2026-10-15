"""Additive training bucket upgrades preserve existing resource ownership."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from worktree_env import atomic_json, load_identity, read_state, topology
from worktree_resources import LocalResources


class TrainingBucketUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        resources = topology(self.root, load_identity(self.root, create=True), "test")
        self.settings = SimpleNamespace(
            database_url="postgresql+asyncpg://test:test@localhost/db",
            effective_migration_database_url="postgresql+asyncpg://test:test@localhost/db",
            minio_endpoint="localhost:9000",
            minio_use_ssl=False,
            minio_access_key="test",
            minio_secret_key="test",
        )
        self.path = self.root / ".worktree/test/resources.json"
        self.target = {
            "schema": 1,
            "resources": resources,
            "postgres": {"host": "localhost", "port": 5432},
            "minio": {"endpoint": "localhost:9000", "ssl": False},
        }
        self.legacy = json.loads(json.dumps(self.target))
        self.name = self.legacy["resources"]["buckets"].pop("MINIO_TRAINING_BUCKET")
        atomic_json(self.path, self.legacy)
        self.mock_s3 = MagicMock()
        with patch("worktree_resources.boto3.client", return_value=self.mock_s3):
            self.backend = LocalResources(resources, self.settings, code_root=self.root)
        self.buckets = {
            name: {"owner": resources["owner"]}
            for name in self.legacy["resources"]["buckets"].values()
        }
        self.backend.bucket_info = lambda name: self.buckets.get(name)
        self.backend.inspect = lambda: {
            "database": {"owner": resources["owner"], "sessions": 0},
            "redis": {"owner": resources["owner"]},
            "buckets": {
                name: self.buckets.get(name)
                for name in self.backend.resources["buckets"].values()
            },
        }
        self.mock_s3.put_bucket_tagging.side_effect = lambda **kwargs: (
            self.buckets.update({kwargs["Bucket"]: {"owner": resources["owner"]}})
        )
        self.mock_s3.create_bucket.side_effect = lambda **kwargs: self.buckets.update(
            {kwargs["Bucket"]: {"owner": None}}
        )
        self.mock_s3.delete_bucket.side_effect = lambda **kwargs: self.buckets.pop(
            kwargs["Bucket"], None
        )

    def test_old_commands_retain_exact_legacy_inventory(self):
        self.assertEqual(self.backend.resources, self.legacy["resources"])
        self.assertNotIn("MINIO_TRAINING_BUCKET", self.backend.resources["buckets"])
        self.mock_s3.create_bucket.assert_not_called()
        self.assertEqual(read_state(self.path), self.legacy)

    def test_upgrade_preserves_old_inventory_and_publishes_atomically(self):
        old = dict(self.buckets)
        self.backend.upgrade_training_bucket()
        self.assertEqual(read_state(self.path), self.target)
        self.assertTrue(all(self.buckets[name] == value for name, value in old.items()))
        self.assertEqual(self.backend.resources, self.target["resources"])
        self.assertFalse((self.path.parent / "training-bucket-upgrade.json").exists())

    def test_failed_tagging_compensates_empty_bucket_and_retry_succeeds(self):
        self.mock_s3.put_bucket_tagging.side_effect = RuntimeError("injected failure")
        with self.assertRaises(RuntimeError):
            self.backend.upgrade_training_bucket()
        self.assertEqual(read_state(self.path), self.legacy)
        self.assertNotIn(self.name, self.buckets)
        self.mock_s3.delete_bucket.assert_called_once_with(Bucket=self.name)
        self.mock_s3.put_bucket_tagging.side_effect = lambda **kwargs: (
            self.buckets.update(
                {kwargs["Bucket"]: {"owner": self.target["resources"]["owner"]}}
            )
        )
        self.backend.upgrade_training_bucket()
        self.assertEqual(read_state(self.path), self.target)

    def test_failed_compensation_keeps_recovery_and_rejects_automatic_adoption(self):
        self.mock_s3.put_bucket_tagging.side_effect = RuntimeError("injected failure")
        self.mock_s3.delete_bucket.side_effect = RuntimeError("bucket not empty")
        with self.assertRaises(RuntimeError):
            self.backend.upgrade_training_bucket()
        self.assertEqual(read_state(self.path), self.legacy)
        self.assertEqual(
            read_state(self.path.parent / "training-bucket-upgrade.json")["bucket"],
            self.name,
        )
        with self.assertRaisesRegex(ValueError, "创建曾中断"):
            self.backend.upgrade_training_bucket()

    def test_foreign_bucket_is_never_adopted(self):
        self.buckets[self.name] = {"owner": "another-worktree"}
        with self.assertRaisesRegex(ValueError, "拒绝接管"):
            self.backend.upgrade_training_bucket()
        self.mock_s3.create_bucket.assert_not_called()
        self.assertEqual(read_state(self.path), self.legacy)

    def test_rechecks_manifest_after_mode_lock(self):
        atomic_json(
            self.path,
            {**self.target, "postgres": {"host": "another-server", "port": 5432}},
        )
        with self.assertRaisesRegex(ValueError, "清单在准备期间变化"):
            self.backend.upgrade_training_bucket()
        self.mock_s3.create_bucket.assert_not_called()

    def test_changed_endpoint_or_old_bucket_rejected_at_construction(self):
        changed = json.loads(json.dumps(self.legacy))
        changed["resources"]["buckets"]["MINIO_BUCKET"] = "foreign"
        atomic_json(self.path, changed)
        with self.assertRaisesRegex(ValueError, "目标.*不一致"):
            LocalResources(self.target["resources"], self.settings, code_root=self.root)


if __name__ == "__main__":
    unittest.main()
