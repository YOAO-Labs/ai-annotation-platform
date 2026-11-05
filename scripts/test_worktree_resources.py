"""Additive accepted-input bucket upgrades preserve exact worktree ownership."""

import fcntl
import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


from worktree_env import WorktreeError, atomic_json, load_identity, read_state, topology
from worktree_resources import (
    IDENTITY_RUNTIME_CAPABILITIES,
    LocalResources,
    NOTIFICATION_RUNTIME_CAPABILITIES,
    JOBS_LIST_RUNTIME_CAPABILITIES,
    JOBS_LIFECYCLE_RUNTIME_CAPABILITIES,
    JOBS_ROW_LOCK_RUNTIME_CAPABILITIES,
    JOBS_DOMAIN_QUERY_RUNTIME_CAPABILITIES,
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

        async def owned_runtime_role(action="inspect"):
            return None

        backend.database = database
        backend.owned_runtime_role = owned_runtime_role
        backend.redis_info = lambda: None

    def test_same_owner_requires_private_mode_credentials_and_never_falls_back(self):
        from worktree_env import WorktreeError

        with self.assertRaisesRegex(WorktreeError, "运行 init"):
            self.backend.runtime_url()
        document = {**self.backend._runtime_credential_identity(), "password": "a" * 43}
        path = self.path.with_name("runtime-credentials.json")
        atomic_json(path, document)
        url = self.backend.runtime_url()
        self.assertEqual(url.username, self.backend.resources["database"] + "_app")
        self.assertEqual(url.password, document["password"])
        self.assertEqual(self.backend.settings.database_url, self.settings.database_url)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaisesRegex(WorktreeError, "0600"):
            self.backend.runtime_url()
        path.chmod(0o600)
        atomic_json(path, {**document, "owner": "foreign-owner"})
        with self.assertRaisesRegex(WorktreeError, "不一致"):
            self.backend.runtime_url()

    def test_runtime_credentials_do_not_follow_symlinks_or_replace_corrupt_state(self):
        from worktree_env import WorktreeError

        path = self.path.with_name("runtime-credentials.json")
        outside = self.root / "outside.json"
        outside.write_text("preserve")
        path.symlink_to(outside)
        with self.assertRaisesRegex(WorktreeError, "symlink|符号链接"):
            self.backend.runtime_url()
        self.assertEqual(outside.read_text(), "preserve")
        path.unlink()
        path.write_text("{")
        path.chmod(0o600)
        with self.assertRaisesRegex(WorktreeError, "损坏"):
            self.backend.runtime_url()
        self.assertEqual(path.read_text(), "{")

    def test_prepare_uses_inventory_after_inputs_upgrade(self):
        self._use_legacy_9()

        async def revisions():
            return ["owned-head"]

        async def granted():
            return True

        self.backend.revisions = revisions
        self.backend.grant_runtime = granted
        self.backend.verify_runtime_login = granted
        self.backend.start_redis = lambda: None
        with patch(
            "worktree_resources.migration_graph",
            return_value=(["owned-head"], ["owned-head"]),
        ):
            self.backend.prepare(skip_migrations=True)
        self.mock_s3.create_bucket.assert_called_once_with(Bucket=self.input_name)
        self.assertEqual(read_state(self.path), self.target)

    def test_partial_fresh_prepare_cleans_owned_bucket_or_retains_unowned_recovery(
        self,
    ):
        for tag_failure in (False, True):
            with self.subTest(tag_failure=tag_failure):
                self.path.unlink(missing_ok=True)
                journal = self.path.parent / "inputs-bucket-upgrade.json"
                journal.unlink(missing_ok=True)
                self.buckets.clear()
                with patch(
                    "worktree_resources.boto3.client", return_value=self.mock_s3
                ):
                    backend = LocalResources(
                        self.target["resources"], self.settings, code_root=self.root
                    )
                self._bind_backend(backend)

                async def revisions():
                    return ["owned-head"]

                backend.revisions = revisions
                self.mock_s3.put_bucket_tagging.side_effect = (
                    RuntimeError("tag interrupted")
                    if tag_failure
                    else lambda **kwargs: self.buckets.update(
                        {kwargs["Bucket"]: {"owner": self.target["resources"]["owner"]}}
                    )
                )
                self.mock_s3.get_bucket_versioning.side_effect = RuntimeError(
                    "versioning read interrupted"
                )
                self.mock_s3.get_paginator.return_value.paginate.return_value = iter(
                    [{}]
                )
                self.mock_s3.delete_bucket.side_effect = lambda **kwargs: (
                    self.buckets.pop(kwargs["Bucket"], None)
                )
                with patch(
                    "worktree_resources.migration_graph",
                    return_value=(["owned-head"], ["owned-head"]),
                ):
                    with self.assertRaises((RuntimeError, WorktreeError)):
                        backend.prepare(skip_migrations=True)
                self.assertEqual(read_state(self.path), self.target)
                self.assertTrue(journal.exists())
                if tag_failure:
                    with self.assertRaises(WorktreeError):
                        if backend.touched:
                            backend.destroy(backend.resources["confirmation"])
                    self.assertEqual(self.buckets[self.input_name], {"owner": None})
                    self.assertTrue(self.path.exists())
                    self.assertTrue(journal.exists())
                else:
                    if backend.touched:
                        backend.destroy(backend.resources["confirmation"])
                    self.assertNotIn(self.input_name, self.buckets)
                    self.assertFalse(self.path.exists())

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
            r'GRANT EXECUTE ON FUNCTION\s+([^;]*?)\s+TO\s+:"application_role";',
            sql,
            flags=re.DOTALL,
        )
        normalized = re.sub(r"\s+", "", ",".join(grants))
        for signature in IDENTITY_RUNTIME_CAPABILITIES:
            self.assertIn(signature, normalized)
        for signature in NOTIFICATION_RUNTIME_CAPABILITIES:
            self.assertIn(signature, normalized)
        for signature in (
            *JOBS_LIST_RUNTIME_CAPABILITIES,
            *JOBS_LIFECYCLE_RUNTIME_CAPABILITIES,
            *JOBS_ROW_LOCK_RUNTIME_CAPABILITIES,
            *JOBS_DOMAIN_QUERY_RUNTIME_CAPABILITIES,
        ):
            self.assertIn(signature, normalized)
        self.assertNotIn(
            "aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)",
            normalized,
        )
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
            r'GRANT EXECUTE ON FUNCTION\s+([^;]*?)\s+TO\s+:"application_role";',
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

    def test_private_acl_rejects_missing_or_duplicated_source(self):
        backend = object.__new__(LocalResources)
        backend.code_root = self.root
        target = self.root / "infra/docker/lan-production-grants.sql"
        target.parent.mkdir(parents=True)
        for domain in ("labeling", "workspace"):
            block = f"DO ${domain}_private_acl$ BEGIN END; ${domain}_private_acl$;"
            for content in ("", block + block):
                target.write_text(content)
                with self.subTest(domain=domain, content=content):
                    with self.assertRaises(WorktreeError):
                        backend.private_runtime_acl(domain)

    def test_audit_maintenance_grant_is_bounded_to_its_owner_command(self):
        sql = (
            Path(__file__).resolve().parents[1]
            / "infra/docker/audit-maintenance-grants.sql"
        ).read_text()
        self.assertNotIn("\\ir", sql)
        self.assertEqual(
            sql.count(
                "GRANT EXECUTE ON FUNCTION aap_audit_v1.maintenance_command(jsonb)"
            ),
            1,
        )
        for required in (
            "function.prosecdef",
            "function.provolatile='v'",
            "search_path=pg_catalog, pg_temp",
            "TimeZone=UTC",
            "'CREATE,TEMP'",
            "REVOKE ALL ON ALL TABLES IN SCHEMA",
            "REVOKE ALL ON ALL SEQUENCES IN SCHEMA",
            "Audit maintenance LOGIN owns database objects",
        ):
            with self.subTest(required_guard=required):
                self.assertIn(required, sql)


class FixtureInferenceIdentityTests(unittest.TestCase):
    def test_credentials_are_private_and_bound_to_database_owner(self):
        from sqlalchemy.engine import make_url

        from fixture_inference_services import FixtureInferenceServices

        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            resources = topology(root, load_identity(root, create=True), "e2e")
            backend = SimpleNamespace(
                resources=resources,
                code_root=root,
                url=make_url(
                    f"postgresql+asyncpg://owner:secret@localhost/{resources['database']}"
                ),
            )
            service = FixtureInferenceServices(backend, code_root=root)
            credentials = service._load_credentials(create=True)
            self.assertEqual(service.credentials_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(service._load_credentials(), credentials)
            self.assertNotEqual(
                credentials["routing_password"], credentials["health_password"]
            )
            other = SimpleNamespace(
                resources=resources,
                code_root=root,
                url=backend.url.set(username="other_owner"),
            )
            with self.assertRaises(WorktreeError):
                FixtureInferenceServices(other, code_root=root)._load_credentials()

    def test_authority_fence_requires_closed_generation_and_no_owner(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            resources = topology(root, load_identity(root, create=True), "e2e")
            backend = LocalResources.__new__(LocalResources)
            backend.root = root
            backend.resources = resources
            lock = root / ".worktree/e2e/fixture-authority.lock"
            lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                with self.assertRaises(WorktreeError):
                    with backend.fixture_authority_fence():
                        self.fail("a live authority must block destruction")
            finally:
                os.close(descriptor)
            generation = root / ".worktree/e2e/fixture-authority/generation.json"
            record = {"resources": resources, "nonce": "a" * 64, "state": "running"}
            atomic_json(generation, record)
            with self.assertRaises(WorktreeError):
                with backend.fixture_authority_fence():
                    self.fail("an unsettled generation must block destruction")
            atomic_json(generation, {**record, "state": "closed"})
            with backend.fixture_authority_fence():
                self.assertTrue(generation.exists())
            owner = generation.with_name("owner.json")
            atomic_json(owner, {"owner": resources["owner"]})
            with self.assertRaises(WorktreeError):
                with backend.fixture_authority_fence():
                    self.fail("an owner record must block destruction")
            self.assertTrue(owner.exists())


if __name__ == "__main__":
    unittest.main()
