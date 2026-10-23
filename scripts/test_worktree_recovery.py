"""Guard irreversible recovery boundaries without connecting to infrastructure."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import (
    bucket_mapping,
    identity,
    remap_storage,
    storage_view,
)
from verify_worktree_recovery import (
    prepare_empty_target,
    read_key,
    require_empty_database,
    require_offline,
)


def roles(prefix):
    return {name: prefix + "-" + suffix for name, suffix in BUCKETS.items()}


class RecoveryGuards(unittest.TestCase):
    def test_optimized_python_refuses_before_resource_allocation(self):
        script = Path(__file__).with_name("verify_worktree_recovery.py")
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            result = subprocess.run(
                [sys.executable, "-O", str(script), "--report", str(report)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须保留全部校验", result.stderr)
            self.assertFalse(report.exists())

    def test_foreign_resource_and_existing_target_refuse_before_first_write(self):
        resources = {
            "owner": "this-owner",
            "database": "db",
            "redis_name": "redis",
            "buckets": roles("target"),
        }
        for inventory in (
            {
                "database": {"owner": "foreign", "sessions": 0},
                "redis": None,
                "buckets": {},
            },
            {
                "database": {"owner": "this-owner", "sessions": 0},
                "redis": None,
                "buckets": {},
            },
            {
                "database": None,
                "redis": None,
                "buckets": {"target-datasets": {"owner": "foreign"}},
            },
        ):
            backend = Mock(resources=resources)
            backend.inspect.return_value = inventory
            with self.assertRaises(WorktreeError):
                prepare_empty_target(backend)
            backend.database.assert_not_called()
            backend.s3.create_bucket.assert_not_called()
            backend.start_redis.assert_not_called()

    def test_an_unknown_client_blocks_offline_capture(self):
        backend = Mock(
            resources={
                "owner": "owner",
                "database": "db",
                "redis_name": "redis",
                "buckets": {},
            }
        )
        backend.inspect.return_value = {
            "database": {"owner": "owner", "sessions": 1},
            "redis": None,
            "buckets": {},
        }
        with self.assertRaisesRegex(WorktreeError, "仍有客户端"):
            require_offline(backend)

    def test_key_is_separate_private_and_matches_generation(self):
        from cryptography.fernet import Fernet

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "key.json"
            key = Fernet.generate_key().decode()
            content = json.dumps({"connector_encryption_key": key}).encode()
            path.write_bytes(content)
            path.chmod(0o600)
            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(read_key(path, digest), key)
            with self.assertRaises(WorktreeError):
                read_key(path, "0" * 64)
            path.chmod(0o644)
            with self.assertRaises(WorktreeError):
                read_key(path, digest)
            path.chmod(0o600)
            for value in (None, 42, [], {}):
                invalid = json.dumps({"connector_encryption_key": value}).encode()
                path.write_bytes(invalid)
                with self.assertRaisesRegex(WorktreeError, "格式不合法"):
                    read_key(path, hashlib.sha256(invalid).hexdigest())
            path.write_bytes(content)
            link = Path(temporary) / "linked-key"
            link.symlink_to(path)
            with self.assertRaises(WorktreeError):
                read_key(link, digest)
            path.unlink()
            with self.assertRaises(WorktreeError):
                read_key(path, digest)

    def test_all_purposes_and_alias_partition_are_required(self):
        source, target = roles("source"), roles("target")
        self.assertEqual(len(bucket_mapping(source, target)), 9)
        for changed in (
            {},
            {**target, "MINIO_BUCKET": target["MINIO_DATASETS_BUCKET"]},
            {**target, "MINIO_BUCKET": source["MINIO_BUCKET"]},
        ):
            with self.assertRaises(WorktreeError):
                bucket_mapping(source, changed)
        source["MINIO_BUCKET"] = source["MINIO_DATASETS_BUCKET"]
        with self.assertRaises(WorktreeError):
            bucket_mapping(source, target)
        target["MINIO_BUCKET"] = target["MINIO_DATASETS_BUCKET"]
        self.assertEqual(len(bucket_mapping(source, target)), 8)


class RecoveryDatabaseGuards(unittest.IsolatedAsyncioTestCase):
    async def test_empty_target_is_checked_again_before_restore(self):
        backend = SimpleNamespace(
            url="postgresql+asyncpg://unused/owned-target",
            resources={"database": "owned-target", "owner": "owner"},
            database=AsyncMock(return_value={"owner": "owner", "sessions": 0}),
        )
        connection = SimpleNamespace(scalar=AsyncMock(return_value=1))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=connection)
        context.__aexit__ = AsyncMock(return_value=False)
        engine = SimpleNamespace(
            connect=Mock(return_value=context), dispose=AsyncMock()
        )
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine):
            with self.assertRaisesRegex(WorktreeError, "非空恢复目标"):
                await require_empty_database(backend)
            connection.scalar.assert_awaited_once()
            engine.dispose.assert_awaited_once()
            connection.scalar.return_value = 0
            await require_empty_database(backend)
            backend.database.return_value = {"owner": "owner", "sessions": 1}
            engine.connect.reset_mock()
            with self.assertRaisesRegex(WorktreeError, "仍有客户端"):
                await require_empty_database(backend)
            engine.connect.assert_not_called()


class MappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.db.models.dataset_asset import (
            DatasetAssetLocation,
            StorageBinding,
            StorageLocation,
        )
        from app.db.models.dataset_source import DatasetSourcePreview
        from app.db.models.original_cache import OriginalCacheEntry
        from app.db.models.prediction_input import PredictionInputLease
        from app.db.models.training_storage_deletion import TrainingStorageDeletion
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        self.models = SimpleNamespace(
            asset=DatasetAssetLocation,
            binding=StorageBinding,
            location=StorageLocation,
            preview=DatasetSourcePreview,
            cache=OriginalCacheEntry,
            input=PredictionInputLease,
            deletion=TrainingStorageDeletion,
        )
        self.source, self.target, self.endpoint = (
            roles("source"),
            roles("target"),
            "http://127.0.0.1:9000",
        )
        self.location_id = LEGACY_LOCATION_ID
        self.rows = {
            StorageBinding: [
                SimpleNamespace(
                    purpose=suffix,
                    location_id=LEGACY_LOCATION_ID,
                    generation=1,
                    bucket=self.source[role],
                )
                for role, suffix in BUCKETS.items()
            ],
            DatasetAssetLocation: [
                SimpleNamespace(
                    asset_id="asset",
                    location_id=LEGACY_LOCATION_ID,
                    bucket=self.source["MINIO_DATASETS_BUCKET"],
                )
            ],
            DatasetSourcePreview: [
                SimpleNamespace(
                    source_id="source",
                    path_digest="digest",
                    location_id=LEGACY_LOCATION_ID,
                    bucket=self.source["MINIO_MEDIA_CACHE_BUCKET"],
                )
            ],
            OriginalCacheEntry: [
                SimpleNamespace(
                    id="cache",
                    location_id=LEGACY_LOCATION_ID,
                    binding_generation=1,
                    bucket=self.source["MINIO_MEDIA_CACHE_BUCKET"],
                    storage_identity=identity(
                        self.endpoint, self.source["MINIO_MEDIA_CACHE_BUCKET"]
                    ),
                )
            ],
            PredictionInputLease: [
                SimpleNamespace(
                    id="input",
                    bucket=self.source["MINIO_IMPORT_BUCKET"],
                    storage_identity=identity(
                        self.endpoint, self.source["MINIO_IMPORT_BUCKET"]
                    ),
                )
            ],
            TrainingStorageDeletion: [
                SimpleNamespace(
                    id="deletion", bucket=self.source["MINIO_TRAINING_BUCKET"]
                )
            ],
        }
        self.db = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    kind="s3",
                    config_revision=1,
                    config={"endpoint": "127.0.0.1:9000", "use_ssl": False},
                )
            ),
            scalars=AsyncMock(
                side_effect=lambda statement: self.rows[
                    statement.column_descriptions[0]["entity"]
                ]
            ),
            flush=AsyncMock(),
        )

    async def test_every_durable_physical_owner_is_relocated(self):
        from app.services import original_cache, prediction_inputs

        ledger = await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(
            {entry["table"] for entry in ledger},
            {
                "storage_bindings",
                "dataset_asset_locations",
                "dataset_source_previews",
                "original_cache_entries",
                "prediction_input_leases",
                "training_storage_deletions",
            },
        )
        target = storage_view(self.endpoint, self.target)
        self.assertEqual(
            self.rows[self.models.cache][0].storage_identity,
            original_cache.storage_identity(target),
        )
        self.assertEqual(
            self.rows[self.models.input][0].storage_identity,
            prediction_inputs.storage_identity(target),
        )
        for rows in self.rows.values():
            self.assertTrue(all(row.bucket in self.target.values() for row in rows))
        self.db.flush.assert_awaited_once()

    async def test_unknown_cleanup_owner_refuses_all_planned_changes(self):
        self.rows[self.models.deletion][0].bucket = "not-owned"
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertTrue(
            all(
                row.bucket in self.source.values()
                for row in self.rows[self.models.binding]
            )
        )
        self.db.flush.assert_not_awaited()

    async def test_cache_with_foreign_storage_identity_is_not_repaired_silently(self):
        self.rows[self.models.cache][0].storage_identity = "foreign"
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(
            self.rows[self.models.asset][0].bucket, self.source["MINIO_DATASETS_BUCKET"]
        )
        self.db.flush.assert_not_awaited()

    async def test_bad_legacy_binding_generation_cannot_be_adopted(self):
        self.rows[self.models.binding][-1].generation = 2
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.db.flush.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
