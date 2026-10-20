"""Additive accepted-input bucket upgrades preserve exact worktree ownership."""

import asyncio
import json
import re
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.engine import make_url

from worktree_env import atomic_json, load_identity, read_state, topology
from worktree_resources import (
    IDENTITY_RUNTIME_CAPABILITIES,
    IMAGE_ACCEPTED_INPUT_RUNTIME_CAPABILITIES,
    IMAGE_ACCEPTED_RUNTIME_CAPABILITIES,
    IMAGE_LABELING_INPUT_RUNTIME_CAPABILITIES,
    IMAGE_MANIFEST_RUNTIME_CAPABILITIES,
    IMAGE_MEDIA_RUNTIME_CAPABILITIES,
    LocalResources,
    VIDEO_RUNTIME_CAPABILITIES,
)


def _without_buckets(manifest, *names):
    result = json.loads(json.dumps(manifest))
    for name in names:
        result["resources"]["buckets"].pop(name, None)
    return result


class BucketUpgradeTests(unittest.TestCase):
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
        self.training_target = _without_buckets(self.target, "MINIO_INPUTS_BUCKET")
        self.legacy_9 = self.training_target
        self.legacy_8 = _without_buckets(self.training_target, "MINIO_TRAINING_BUCKET")
        self.input_name = resources["buckets"]["MINIO_INPUTS_BUCKET"]
        self.training_name = resources["buckets"]["MINIO_TRAINING_BUCKET"]
        atomic_json(self.path, self.legacy_8)
        self.mock_s3 = MagicMock()
        with patch("worktree_resources.boto3.client", return_value=self.mock_s3):
            self.backend = LocalResources(resources, self.settings, code_root=self.root)
        self.buckets = {
            name: {"owner": resources["owner"]}
            for name in self.legacy_8["resources"]["buckets"].values()
        }
        self.versioning = {}
        self._bind_backend(self.backend)
        self.mock_s3.create_bucket.side_effect = lambda **kwargs: self.buckets.update(
            {kwargs["Bucket"]: {"owner": None}}
        )
        self.mock_s3.put_bucket_tagging.side_effect = lambda **kwargs: (
            self.buckets.update(
                {kwargs["Bucket"]: {"owner": self.target["resources"]["owner"]}}
            )
        )
        self.mock_s3.get_bucket_versioning.side_effect = lambda **kwargs: {
            "Status": self.versioning.get(kwargs["Bucket"])
        }
        self.mock_s3.put_bucket_versioning.side_effect = lambda **kwargs: (
            self.versioning.update(
                {kwargs["Bucket"]: kwargs["VersioningConfiguration"]["Status"]}
            )
        )

    def _bind_backend(self, backend):
        owner = self.target["resources"]["owner"]
        backend.bucket_info = lambda name: self.buckets.get(name)
        backend.inspect = lambda: {
            "database": {"owner": owner, "sessions": 0},
            "redis": None,
            "buckets": {
                name: self.buckets.get(name)
                for name in backend.resources["buckets"].values()
            },
        }

        async def database(action="inspect"):
            return {"owner": owner, "sessions": 0} if action == "inspect" else None

        backend.database = database
        backend.redis_info = lambda: None

    def _use_legacy_9(self):
        atomic_json(self.path, self.legacy_9)
        self.buckets[self.training_name] = {"owner": self.target["resources"]["owner"]}
        with patch("worktree_resources.boto3.client", return_value=self.mock_s3):
            self.backend = LocalResources(
                self.target["resources"], self.settings, code_root=self.root
            )
        self._bind_backend(self.backend)

    def test_old_8_and_9_manifests_remain_read_only_until_explicit_prepare(self):
        self.assertEqual(self.backend.resources, self.legacy_8["resources"])
        self.assertNotIn("MINIO_TRAINING_BUCKET", self.backend.resources["buckets"])
        self.assertNotIn("MINIO_INPUTS_BUCKET", self.backend.resources["buckets"])
        self.mock_s3.create_bucket.assert_not_called()
        self.mock_s3.put_bucket_versioning.assert_not_called()

        self._use_legacy_9()
        self.assertEqual(read_state(self.path), self.legacy_9)
        self.assertNotIn("MINIO_INPUTS_BUCKET", self.backend.resources["buckets"])
        self.mock_s3.create_bucket.assert_not_called()
        self.mock_s3.put_bucket_versioning.assert_not_called()

    def test_8_bucket_upgrade_is_staged_before_accepted_inputs(self):
        self.backend.upgrade_training_bucket()
        self.assertEqual(read_state(self.path), self.training_target)
        self.assertIn("MINIO_TRAINING_BUCKET", self.backend.resources["buckets"])
        self.assertNotIn("MINIO_INPUTS_BUCKET", self.backend.resources["buckets"])
        self.assertNotIn(self.input_name, self.buckets)
        self.mock_s3.put_bucket_versioning.assert_not_called()

        self.backend.upgrade_inputs_bucket()
        self.assertEqual(read_state(self.path), self.target)
        self.assertEqual(self.backend.resources, self.target["resources"])
        self.assertEqual(
            self.buckets[self.input_name]["owner"], self.target["resources"]["owner"]
        )
        self.assertEqual(self.versioning[self.input_name], "Enabled")
        self.assertNotIn(
            self.target["resources"]["buckets"]["MINIO_MEDIA_CACHE_BUCKET"],
            self.versioning,
        )

    def test_9_bucket_upgrade_creates_and_versions_only_the_owned_input_bucket(self):
        self._use_legacy_9()
        self.backend.upgrade_inputs_bucket()
        self.assertEqual(read_state(self.path), self.target)
        self.assertEqual(self.backend.resources, self.target["resources"])
        self.assertEqual(self.versioning, {self.input_name: "Enabled"})
        self.assertFalse((self.path.parent / "inputs-bucket-upgrade.json").exists())

    def test_existing_owned_inputs_bucket_is_brought_to_enabled_versioning(self):
        self._use_legacy_9()
        self.buckets[self.input_name] = {"owner": self.target["resources"]["owner"]}
        self.versioning[self.input_name] = "Suspended"
        self.backend.upgrade_inputs_bucket()
        self.assertEqual(self.versioning[self.input_name], "Enabled")
        self.mock_s3.put_bucket_versioning.assert_called_once_with(
            Bucket=self.input_name,
            VersioningConfiguration={"Status": "Enabled"},
        )

    def test_failed_input_bucket_tagging_keeps_old_manifest_and_refuses_adoption(self):
        self._use_legacy_9()
        self.mock_s3.put_bucket_tagging.side_effect = RuntimeError("tag failure")
        with self.assertRaisesRegex(ValueError, "保留恢复记录"):
            self.backend.upgrade_inputs_bucket()
        journal = self.path.parent / "inputs-bucket-upgrade.json"
        self.assertEqual(read_state(self.path), self.legacy_9)
        self.assertEqual(
            read_state(journal),
            {
                "bucket": self.input_name,
                "owner": self.target["resources"]["owner"],
            },
        )
        self.assertEqual(self.buckets[self.input_name]["owner"], None)
        self.mock_s3.put_bucket_versioning.assert_not_called()

        with self.assertRaisesRegex(ValueError, "不会自动接管"):
            self.backend.upgrade_inputs_bucket()
        self.assertEqual(read_state(self.path), self.legacy_9)

    def test_foreign_inputs_bucket_is_never_adopted(self):
        self._use_legacy_9()
        self.buckets[self.input_name] = {"owner": "another-worktree"}
        with self.assertRaisesRegex(ValueError, "拒绝接管或删除"):
            self.backend.upgrade_inputs_bucket()
        self.mock_s3.create_bucket.assert_not_called()
        self.mock_s3.put_bucket_versioning.assert_not_called()
        self.assertEqual(read_state(self.path), self.legacy_9)

    def test_destroy_refuses_unmanifested_input_upgrade_journal(self):
        self._use_legacy_9()
        atomic_json(
            self.path.parent / "inputs-bucket-upgrade.json",
            {"bucket": self.input_name, "owner": self.target["resources"]["owner"]},
        )
        with self.assertRaisesRegex(ValueError, "升级未完成"):
            self.backend.destroy(self.backend.resources["confirmation"])
        self.mock_s3.delete_bucket.assert_not_called()

    def test_legacy_stop_and_destroy_do_not_touch_an_unmanifested_input_bucket(self):
        self._use_legacy_9()
        self.buckets[self.input_name] = {"owner": self.target["resources"]["owner"]}

        self.backend.stop_redis()
        self.assertEqual(self.backend.resources, self.legacy_9["resources"])
        self.mock_s3.get_bucket_versioning.assert_not_called()
        self.mock_s3.put_bucket_versioning.assert_not_called()

        class EmptyPaginator:
            def paginate(self, **_kwargs):
                return iter([{"Uploads": []}])

        self.mock_s3.get_paginator.side_effect = lambda _name: EmptyPaginator()
        self.mock_s3.delete_bucket.side_effect = lambda **kwargs: self.buckets.pop(
            kwargs["Bucket"], None
        )
        self.backend.destroy(self.legacy_9["resources"]["confirmation"])
        self.assertIn(self.input_name, self.buckets)
        self.mock_s3.get_bucket_versioning.assert_not_called()

    def test_legacy_manifest_can_be_constructed_for_read_only_diagnosis(self):
        self._use_legacy_9()
        from worktree_runtime import diagnose

        async def revisions():
            return []

        self.backend.revisions = revisions

        with patch("worktree_resources.migration_graph", return_value=([], [])):
            report = diagnose(self.backend)
        self.assertIn("buckets", report["checks"])
        self.assertNotIn(self.input_name, report["checks"]["buckets"])
        self.mock_s3.get_bucket_versioning.assert_not_called()

    def test_doctor_checks_versioning_only_for_manifested_owned_input_bucket(self):
        atomic_json(self.path, self.target)
        with patch("worktree_resources.boto3.client", return_value=self.mock_s3):
            backend = LocalResources(
                self.target["resources"], self.settings, code_root=self.root
            )
        self._bind_backend(backend)
        self.buckets = {
            name: {"owner": self.target["resources"]["owner"]}
            for name in self.target["resources"]["buckets"].values()
        }
        self.versioning[self.input_name] = "Enabled"

        from worktree_runtime import diagnose

        async def revisions():
            return []

        backend.revisions = revisions
        with patch("worktree_resources.migration_graph", return_value=([], [])):
            report = diagnose(backend)

        self.assertEqual(
            report["checks"]["inputs_bucket_versioning"], {"Status": "Enabled"}
        )
        self.mock_s3.get_bucket_versioning.assert_called_once_with(
            Bucket=self.input_name
        )

    def test_destroy_removes_all_input_versions_and_markers_before_bucket(self):
        atomic_json(self.path, self.target)
        with patch("worktree_resources.boto3.client", return_value=self.mock_s3):
            backend = LocalResources(
                self.target["resources"], self.settings, code_root=self.root
            )
        self._bind_backend(backend)
        self.buckets = {
            name: {"owner": self.target["resources"]["owner"]}
            for name in self.target["resources"]["buckets"].values()
        }
        backend.bucket_info = lambda name: self.buckets.get(name)
        self.versioning[self.input_name] = "Enabled"
        operations = []

        input_name = self.input_name
        input_versions = [
            {
                "Versions": [
                    {"Key": "accepted-inputs/id/source", "VersionId": "v1"},
                    {"Key": "accepted-inputs/id/source", "VersionId": "v2"},
                ],
                "DeleteMarkers": [
                    {"Key": "accepted-inputs/id/source", "VersionId": "d1"}
                ],
            }
        ]

        class BucketPaginator:
            def __init__(self, name):
                self.name = name

            def paginate(self, *, Bucket):
                if self.name == "list_multipart_uploads":
                    return iter([{"Uploads": []}])
                return iter(
                    input_versions
                    if Bucket == input_name
                    else [{"Versions": [], "DeleteMarkers": []}]
                )

        self.mock_s3.get_paginator.side_effect = BucketPaginator

        def delete_objects(**kwargs):
            operations.append(
                ("delete_objects", kwargs["Bucket"], kwargs["Delete"]["Objects"])
            )
            return {}

        self.mock_s3.delete_objects.side_effect = delete_objects

        def delete_bucket(**kwargs):
            operations.append(("delete_bucket", kwargs["Bucket"]))
            self.buckets.pop(kwargs["Bucket"], None)

        self.mock_s3.delete_bucket.side_effect = delete_bucket
        backend.destroy(self.target["resources"]["confirmation"])

        input_deletes = [
            operation
            for operation in operations
            if operation[0] == "delete_objects" and operation[1] == self.input_name
        ]
        assert len(input_deletes) == 1
        assert {(item["Key"], item["VersionId"]) for item in input_deletes[0][2]} == {
            ("accepted-inputs/id/source", "v1"),
            ("accepted-inputs/id/source", "v2"),
            ("accepted-inputs/id/source", "d1"),
        }
        assert operations.index(input_deletes[0]) < operations.index(
            ("delete_bucket", self.input_name)
        )

    def test_rechecks_manifest_after_mode_lock(self):
        changed = {
            **self.legacy_8,
            "postgres": {"host": "another-server", "port": 5432},
        }
        atomic_json(self.path, changed)
        with self.assertRaisesRegex(ValueError, "清单在准备期间变化"):
            self.backend.upgrade_training_bucket()
        self.mock_s3.create_bucket.assert_not_called()

    def test_changed_endpoint_or_old_bucket_rejected_at_construction(self):
        changed = json.loads(json.dumps(self.legacy_8))
        changed["resources"]["buckets"]["MINIO_BUCKET"] = "foreign"
        atomic_json(self.path, changed)
        with self.assertRaisesRegex(ValueError, "目标.*不一致"):
            LocalResources(self.target["resources"], self.settings, code_root=self.root)

    def test_lan_runtime_preserves_embedded_identity_capabilities(self):
        sql = (
            Path(__file__).resolve().parents[1]
            / "infra/docker/lan-production-grants.sql"
        ).read_text()
        grants = re.findall(
            r'GRANT EXECUTE ON FUNCTION\s+(.*?)\s+TO\s+:"application_role";',
            sql,
            flags=re.DOTALL,
        )
        normalized = re.sub(r"\s+", "", ",".join(grants))
        for signature in IDENTITY_RUNTIME_CAPABILITIES:
            self.assertIn(signature, normalized)
        for private in (
            "aap_identity_recovery_v1._actor_record(uuid)",
            "aap_identity_recovery_v1._token_record(text,uuid)",
            "aap_settings_v1.identity_recovery_policy(jsonb)",
        ):
            self.assertNotIn(private, normalized)

    def test_lan_runtime_grants_exact_video_capabilities_without_sweep_or_gc(self):
        grant_file = (
            Path(__file__).resolve().parents[1]
            / "infra/docker/lan-production-grants.sql"
        )
        sql = grant_file.read_text()
        runtime_grants = re.findall(
            r'GRANT EXECUTE ON FUNCTION\s+(.*?)\s+TO\s+:"application_role";',
            sql,
            flags=re.DOTALL,
        )
        normalized = re.sub(r"\s+", "", ",".join(runtime_grants))

        self.assertEqual(len(VIDEO_RUNTIME_CAPABILITIES), 25)
        self.assertEqual(len(set(VIDEO_RUNTIME_CAPABILITIES)), 25)
        for signature in VIDEO_RUNTIME_CAPABILITIES:
            self.assertIn(signature, normalized)
        for excluded in (
            "aap_assets_v1.sweep_accepted_video_inputs(integer)",
            "aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)",
            "aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)",
        ):
            self.assertNotIn(excluded, normalized)

    def test_lan_runtime_grants_image_and_accepted_image_allowlists(self):
        grant_file = (
            Path(__file__).resolve().parents[1]
            / "infra/docker/lan-production-grants.sql"
        )
        sql = grant_file.read_text()
        runtime_grants = re.findall(
            r'GRANT EXECUTE ON FUNCTION\s+(.*?)\s+TO\s+:"application_role";',
            sql,
            flags=re.DOTALL,
        )
        normalized = re.sub(r"\s+", "", ",".join(runtime_grants))

        self.assertEqual(len(IMAGE_MEDIA_RUNTIME_CAPABILITIES), 22)
        self.assertEqual(len(set(IMAGE_MEDIA_RUNTIME_CAPABILITIES)), 22)
        self.assertEqual(len(IMAGE_ACCEPTED_INPUT_RUNTIME_CAPABILITIES), 9)
        self.assertEqual(len(IMAGE_MANIFEST_RUNTIME_CAPABILITIES), 5)
        self.assertEqual(len(IMAGE_LABELING_INPUT_RUNTIME_CAPABILITIES), 2)
        self.assertEqual(len(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES), 16)
        self.assertEqual(len(set(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES)), 16)
        for signature in (
            *IMAGE_MEDIA_RUNTIME_CAPABILITIES,
            *IMAGE_ACCEPTED_RUNTIME_CAPABILITIES,
        ):
            self.assertIn(signature, normalized)
        for excluded in (
            "aap_assets_v1.sweep_accepted_image_inputs(integer)",
            "aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)",
            "aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)",
            "aap_assets_v1._lock_accepted_image_input_receipt(uuid,uuid,uuid)",
            "aap_assets_v1._lock_accepted_image_input_pin(uuid,uuid,text)",
        ):
            self.assertNotIn(excluded, normalized)

        image_grant_section = sql.split(
            "-- The monolithic API/Celery runtime still composes the image media owner",
            maxsplit=1,
        )[1].split("REVOKE ALL ON SCHEMA aap_assets_v1", maxsplit=1)[0]
        image_signatures = re.findall(
            r"aap_(?:assets|labeling)_v1\.[a-z0-9_]+\([^)]*\)",
            re.sub(r"\s+", "", image_grant_section),
        )
        self.assertEqual(
            set(image_signatures),
            set(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            | set(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES),
        )
        self.assertEqual(
            len(image_signatures),
            len(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            + len(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES),
        )

    def test_worktree_runtime_grant_uses_media_allowlists(self):
        backend = object.__new__(LocalResources)
        backend.settings = SimpleNamespace(
            database_url="postgresql+asyncpg://app@localhost/app"
        )
        backend.url = make_url("postgresql+asyncpg://owner@localhost/app")
        backend.resources = {"database": "aap_test"}
        statements = []

        class Connection:
            async def exec_driver_sql(self, sql):
                statements.append(sql)

        class Preparer:
            @staticmethod
            def quote_identifier(value):
                return f'"{value}"'

        class Engine:
            dialect = SimpleNamespace(identifier_preparer=Preparer())

            @asynccontextmanager
            async def begin(self):
                yield Connection()

            async def dispose(self):
                return None

        with patch("worktree_resources.create_async_engine", return_value=Engine()):
            asyncio.run(backend.grant_runtime())

        expected = (
            "GRANT EXECUTE ON FUNCTION "
            + ", ".join(VIDEO_RUNTIME_CAPABILITIES)
            + ' TO "app"'
        )
        self.assertIn(expected, statements)
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION "
            + ", ".join(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            + ' TO "app"',
            statements,
        )
        self.assertIn(
            "GRANT EXECUTE ON FUNCTION "
            + ", ".join(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES)
            + ' TO "app"',
            statements,
        )
        worktree_image_signatures = re.findall(
            r"aap_(?:assets|labeling)_v1\.[a-z0-9_]+\([^)]*\)",
            " ".join(
                statement
                for statement in statements
                if any(
                    marker in statement
                    for marker in ("read_image_source(", "find_accepted_image_input(")
                )
            ),
        )
        self.assertEqual(
            set(worktree_image_signatures),
            set(IMAGE_MEDIA_RUNTIME_CAPABILITIES)
            | set(IMAGE_ACCEPTED_RUNTIME_CAPABILITIES),
        )
        for excluded in (
            "aap_assets_v1.sweep_accepted_image_inputs(integer)",
            "aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)",
            "aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)",
            "aap_assets_v1._lock_accepted_image_input_receipt(uuid,uuid,uuid)",
            "aap_assets_v1._lock_accepted_image_input_pin(uuid,uuid,text)",
        ):
            self.assertNotIn(excluded, " ".join(statements))


if __name__ == "__main__":
    unittest.main()
