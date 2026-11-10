"""Additive accepted-input bucket upgrades preserve exact worktree ownership."""

import asyncio
import fcntl
import json
import os
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from worktree_env import WorktreeError, atomic_json, load_identity, read_state, topology
from worktree_resources import (
    LocalResources,
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
            database_url="postgresql+asyncpg://test:test@127.0.0.1:1/db",
            effective_migration_database_url="postgresql+asyncpg://test:test@127.0.0.1:1/db",
            minio_endpoint="localhost:9000",
            minio_use_ssl=False,
            minio_access_key="test",
            minio_secret_key="test",
            connector_encryption_key="bucket-unit-test",
            connector_host_allowlist=["127.0.0.1"],
        )
        self.path = self.root / ".worktree/test/resources.json"
        self.target = {
            "schema": 1,
            "resources": resources,
            "postgres": {"host": "127.0.0.1", "port": 1},
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
        # Bucket unit tests stop at the private store owner boundary. Its real
        # provisioning and reader lifecycle have separate disposable tests.
        self.enterContext(
            patch(
                "fixture_suite_coordinator.prepare_worktree_store",
                new=AsyncMock(return_value={}),
            )
        )
        self.enterContext(
            patch(
                "fixture_suite_coordinator.prepare_store_destruction",
                new=AsyncMock(return_value=None),
            )
        )
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
        backend.environment = lambda: {}

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


class FixtureInferenceIdentityTests(unittest.TestCase):
    def test_private_gpu_selection_discards_inherited_file_and_case_aliases(self):
        from fixture_inference_services import _set_private_gpu_selectors
        from yoao_inference.__main__ import _environment_value

        name = "INFERENCE_GPU_DATABASE_URL"
        with patch.dict(
            os.environ,
            {
                name: "postgresql://foreign/db",
                name.lower(): "postgresql://foreign-lower/db",
                name + "_FILE": "/foreign/secret",
                (name + "_FILE").lower(): "/foreign/lower-secret",
            },
            clear=True,
        ):
            _set_private_gpu_selectors({name: "postgresql://fixture/db"})
            self.assertEqual(_environment_value(name), "postgresql://fixture/db")
            self.assertNotIn(name.lower(), os.environ)
            self.assertNotIn(name + "_FILE", os.environ)
            self.assertNotIn((name + "_FILE").lower(), os.environ)

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

    def test_closed_generation_cannot_hide_unsettled_native_children(self):
        for family, suite_directory in (
            ("owned", False),
            ("owned", True),
            ("screenshot", True),
        ):
            with (
                self.subTest(family=family, suite_directory=suite_directory),
                TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                mode = "test" if family == "screenshot" else "e2e"
                resources = topology(root, load_identity(root, create=True), mode)
                backend = LocalResources.__new__(LocalResources)
                backend.root, backend.resources = root, resources
                suite_id = uuid.uuid4()
                run_id = suite_id if family == "screenshot" else uuid.uuid4()
                directory = root / ".worktree" / mode / "fixture-authority"
                directory.mkdir(mode=0o700, parents=True)
                if suite_directory:
                    directory /= "suite-" + suite_id.hex
                directory.mkdir(mode=0o700, exist_ok=True)
                atomic_json(
                    directory / "generation.json",
                    {
                        "resources": resources,
                        "nonce": "a" * 64,
                        "state": "closed",
                    },
                )
                child = directory / ("native-run-" + run_id.hex)
                child.mkdir(mode=0o700)
                children = (
                    "celery-media",
                    "celery-pyramid",
                    "media-worker",
                    "image-worker",
                    "proxy",
                )
                outcomes = {
                    name: {
                        "pid": index + 100,
                        "start": index + 1000,
                        "exit_code": 0,
                        "pidfd_exit": True,
                        "descendants_drained": True,
                        "acknowledgment": "warm_exit"
                        if name.startswith("celery-")
                        else "settled",
                    }
                    for index, name in enumerate(children)
                }
                run = {
                    "schema": "aap-fixture-native-run/v1",
                    "suite_id": str(suite_id),
                    "run_id": str(run_id),
                    "state": "retired",
                    "receipt_sha256": "b" * 64,
                    "drained": list(children),
                    "child_dispositions": outcomes,
                    **{
                        name: {"pid": result["pid"], "start": result["start"]}
                        for name, result in outcomes.items()
                    },
                    "target_proof": {
                        "schema": "aap-fixture-native-target-retirement/v1",
                        "run_id": str(run_id),
                        "storage_identity": "a" * 64,
                        "target_receipt_sha256": "b" * 64,
                        "retirement_id": str(uuid.uuid4()),
                        "location_id": str(uuid.uuid4()),
                        "binding_generation": 1,
                    },
                }
                supervisor = {
                    "schema": "aap-fixture-native-supervisor/v1",
                    "suite_id": str(suite_id),
                    "resources": resources,
                    "state": "retired",
                    "api_owned": True,
                    "native_run_ids": [str(run_id)],
                    "resolver_retired": True,
                    "business_retired": True,
                    "api": {"pid": 10, "start": 100},
                    "control": {"pid": 20, "start": 200},
                    "api_disposition": {
                        "pid": 10,
                        "start": 100,
                        "exit_code": 0,
                        "pidfd_exit": True,
                        "lifespan_shutdown": True,
                        "descendants_drained": True,
                    },
                    "control_disposition": {
                        "pid": 20,
                        "start": 200,
                        "exit_code": 0,
                        "pidfd_exit": True,
                        "settled_ack": True,
                        "descendants_drained": True,
                    },
                }
                if family == "screenshot":
                    supervisor.update(
                        api_owned=False,
                        root_origin={"family": "screenshot", "parent_run_id": None},
                        birth_admission_id=str(uuid.uuid4()),
                        authority_nonce="a" * 64,
                    )
                    del supervisor["api"]
                    del supervisor["api_disposition"]
                atomic_json(directory / "native-supervisor.json", supervisor)
                atomic_json(child / "owner.json", run)
                protected = root / "protected-resource"
                protected.write_bytes(b"owned data")
                damages = [
                    "unknown_supervisor",
                    "missing_api_topology",
                    "undrained_control",
                    "undrained_child",
                    "changed_child_birth",
                    "changed_target_birth",
                    "missing_child",
                ]
                damages += (
                    [
                        "missing_root_origin",
                        "changed_root_family",
                        "changed_authority_nonce",
                        "missing_birth_admission",
                    ]
                    if family == "screenshot"
                    else ["forced_api_exit", "undrained_api"]
                )
                for damage in damages:
                    with self.subTest(damage=damage):
                        current_supervisor = json.loads(json.dumps(supervisor))
                        current_run = json.loads(json.dumps(run))
                        if damage == "unknown_supervisor":
                            current_supervisor["state"] = "unsettled"
                        elif damage == "missing_api_topology":
                            del current_supervisor["api_owned"]
                        elif damage == "forced_api_exit":
                            current_supervisor["api_disposition"]["exit_code"] = -15
                        elif damage in ("undrained_api", "undrained_control"):
                            participant = damage.removeprefix("undrained_")
                            current_supervisor[participant + "_disposition"][
                                "descendants_drained"
                            ] = False
                        elif damage == "undrained_child":
                            current_run["child_dispositions"]["proxy"][
                                "descendants_drained"
                            ] = False
                        elif damage == "changed_child_birth":
                            current_run["proxy"]["start"] += 1
                        elif damage == "changed_target_birth":
                            current_run["receipt_sha256"] = "c" * 64
                        elif damage == "missing_root_origin":
                            del current_supervisor["root_origin"]
                        elif damage == "changed_root_family":
                            current_supervisor["root_origin"]["family"] = "scale"
                        elif damage == "changed_authority_nonce":
                            current_supervisor["authority_nonce"] = "b" * 64
                        elif damage == "missing_birth_admission":
                            del current_supervisor["birth_admission_id"]
                        atomic_json(
                            directory / "native-supervisor.json", current_supervisor
                        )
                        atomic_json(child / "owner.json", current_run)
                        if damage == "missing_child":
                            (child / "owner.json").unlink()
                        with self.assertRaises(WorktreeError):
                            with backend.fixture_authority_fence():
                                protected.unlink()
                        self.assertEqual(protected.read_bytes(), b"owned data")
                atomic_json(directory / "native-supervisor.json", supervisor)
                atomic_json(child / "owner.json", run)
                extra = directory / ("native-run-" + uuid.uuid4().hex)
                extra.mkdir(mode=0o700)
                with self.assertRaises(WorktreeError):
                    with backend.fixture_authority_fence():
                        protected.unlink()
                self.assertEqual(protected.read_bytes(), b"owned data")
                extra.rmdir()
                with backend.fixture_authority_fence():
                    protected.unlink()
                self.assertFalse(protected.exists())

    def test_api_less_native_owner_is_explicit_and_test_only(self):
        for mode in ("test", "e2e"):
            with self.subTest(mode=mode), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                resources = topology(root, load_identity(root, create=True), mode)
                backend = LocalResources.__new__(LocalResources)
                backend.root, backend.resources = root, resources
                suite_id = uuid.uuid4()
                directory = root / ".worktree" / mode / "fixture-authority"
                directory.mkdir(mode=0o700, parents=True)
                atomic_json(
                    directory / "generation.json",
                    {
                        "resources": resources,
                        "nonce": "a" * 64,
                        "state": "closed",
                    },
                )
                supervisor = {
                    "schema": "aap-fixture-native-supervisor/v1",
                    "suite_id": str(suite_id),
                    "resources": resources,
                    "state": "retired",
                    "api_owned": False,
                    "native_run_ids": [],
                    "resolver_retired": True,
                    "business_retired": True,
                    "control": {"pid": 20, "start": 200},
                    "control_disposition": {
                        "pid": 20,
                        "start": 200,
                        "exit_code": 0,
                        "pidfd_exit": True,
                        "settled_ack": True,
                        "descendants_drained": True,
                    },
                }
                protected = root / "protected-resource"
                protected.write_bytes(b"owned data")
                if mode == "e2e":
                    atomic_json(directory / "native-supervisor.json", supervisor)
                    with self.assertRaises(WorktreeError):
                        with backend.fixture_authority_fence():
                            protected.unlink()
                    self.assertEqual(protected.read_bytes(), b"owned data")
                    continue
                for damage in (
                    "missing_api_topology",
                    "contradictory_api_birth",
                    "missing_control",
                ):
                    with self.subTest(damage=damage):
                        current = json.loads(json.dumps(supervisor))
                        if damage == "missing_api_topology":
                            del current["api_owned"]
                        elif damage == "contradictory_api_birth":
                            current["api"] = {"pid": 10, "start": 100}
                        else:
                            del current["control_disposition"]
                        atomic_json(directory / "native-supervisor.json", current)
                        with self.assertRaises(WorktreeError):
                            with backend.fixture_authority_fence():
                                protected.unlink()
                        self.assertEqual(protected.read_bytes(), b"owned data")
                atomic_json(directory / "native-supervisor.json", supervisor)
                with backend.fixture_authority_fence():
                    protected.unlink()
                self.assertFalse(protected.exists())

    def test_closed_suite_cannot_hide_another_unclosed_suite(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            resources = topology(root, load_identity(root, create=True), "e2e")
            backend = LocalResources.__new__(LocalResources)
            backend.root, backend.resources = root, resources
            directory = root / ".worktree/e2e/fixture-authority"
            directory.mkdir(mode=0o700, parents=True)
            first = directory / ("suite-" + uuid.uuid4().hex)
            second = directory / ("suite-" + uuid.uuid4().hex)
            first.mkdir(mode=0o700, parents=True)
            second.mkdir(mode=0o700)
            record = {"resources": resources, "nonce": "a" * 64, "state": "closed"}
            atomic_json(first / "generation.json", record)
            atomic_json(second / "generation.json", {**record, "state": "running"})
            protected = root / "protected-resource"
            protected.write_bytes(b"neighbor remains")
            with self.assertRaises(WorktreeError):
                with backend.fixture_authority_fence():
                    protected.unlink()
            self.assertEqual(protected.read_bytes(), b"neighbor remains")
            atomic_json(second / "generation.json", record)
            with backend.fixture_authority_fence():
                protected.unlink()
            self.assertFalse(protected.exists())


class FinishedBootstrapRetirementTests(unittest.TestCase):
    def setUp(self):
        from fixture_store import MINIO_IMAGE
        from storage_s3_contract import CHECKOUT_LABEL, OWNER_LABEL
        from verify_storage_candidates import ROOT

        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.resources = topology(
            self.root, load_identity(self.root, create=True), "test"
        )
        self.settings = SimpleNamespace(
            database_url="postgresql+asyncpg://test:test@127.0.0.1:5432/db",
            effective_migration_database_url="postgresql+asyncpg://test:test@127.0.0.1:5432/db",
            minio_endpoint="172.26.9.65:32768",
            minio_use_ssl=False,
            minio_access_key="test",
            minio_secret_key="test",
        )
        self.owner_path = self.root / "owner.json"
        self.owner = {
            "schema": "aap.storage-poc-owner/v1",
            "checkout": str(ROOT),
            "nonce": "a" * 32,
            "private_directory": str(self.root / ("private-" + "a" * 32)),
            "cleaned": False,
            "verification": {
                "state": "finished",
                "endpoint": "http://172.26.9.65:32768",
                "returncode": 1,
            },
            "resources": [
                {"kind": "network", "id": "network", "state": "created"},
                {
                    "kind": "container",
                    "id": "container",
                    "state": "created",
                    "image": MINIO_IMAGE,
                },
            ],
        }
        labels = {OWNER_LABEL: "a" * 32, CHECKOUT_LABEL: str(ROOT)}
        self.network = {
            "Id": "network",
            "Labels": labels.copy(),
            "Driver": "bridge",
            "IPAM": {"Config": [{"Gateway": "172.26.9.65"}]},
            "Containers": {"container": {"EndpointID": "attachment"}},
        }
        self.container = {
            "Id": "container",
            "State": {"Running": True},
            "Image": "sha256:" + "b" * 64,
            "Config": {
                "Image": MINIO_IMAGE,
                "Labels": {**labels, "com.docker.compose.service": "minio"},
            },
            "NetworkSettings": {
                "Ports": {"9000/tcp": [{"HostIp": "172.26.9.65", "HostPort": "32768"}]},
                "Networks": {
                    "bootstrap": {
                        "NetworkID": "network",
                        "Gateway": "172.26.9.65",
                        "EndpointID": "attachment",
                    }
                },
            },
        }
        self.save_owner()
        self.manifest_path = self.root / ".worktree/test/resources.json"
        self.manifest = {
            "schema": 1,
            "resources": self.resources,
            "postgres": {"host": "127.0.0.1", "port": 5432},
            "minio": {"endpoint": self.settings.minio_endpoint, "ssl": False},
        }
        atomic_json(self.manifest_path, self.manifest)
        self.enterContext(
            patch.dict(
                os.environ,
                {"AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER": str(self.owner_path)},
            )
        )
        self.enterContext(
            patch(
                "verify_storage_candidates.Owner.inspect",
                lambda _owner, item: (
                    self.network if item["kind"] == "network" else self.container
                ),
            )
        )
        self.enterContext(patch("worktree_resources.boto3.client"))

    def save_owner(self):
        atomic_json(self.owner_path, self.owner)

    def backend(self, *, retirement=False):
        return LocalResources(
            self.resources,
            self.settings,
            code_root=self.root,
            retire_finished_bootstrap=retirement,
        )

    def test_finished_owner_is_only_available_for_explicit_retirement(self):
        from worktree_env import isolated_environment

        self.owner["verification"]["state"] = "running"
        self.save_owner()
        self.backend()
        with self.assertRaises(WorktreeError):
            self.backend(retirement=True)
        self.owner["verification"]["state"] = "finished"
        self.save_owner()
        with self.assertRaises(WorktreeError):
            self.backend()
        backend = self.backend(retirement=True)
        with self.assertRaises(WorktreeError):
            backend.prepare()
        with self.assertRaises(WorktreeError):
            backend.environment()
        with self.assertRaises(WorktreeError):
            backend._environment(6379)
        with self.assertRaises(WorktreeError):
            backend.start_redis()
        with self.assertRaises(WorktreeError):
            asyncio.run(backend.database("create"))
        with self.assertRaises(WorktreeError):
            asyncio.run(backend.owned_runtime_role("create"))
        with self.assertRaises(WorktreeError):
            isolated_environment(
                self.resources,
                code_root=self.root,
                database_url=self.settings.database_url,
                migration_database_url=self.settings.effective_migration_database_url,
                minio_endpoint=self.settings.minio_endpoint,
                redis_port=6379,
                inherited=os.environ,
            )

    def test_retirement_requires_existing_manifest_and_matching_endpoint_and_bridge(self):
        self.manifest_path.unlink()
        with self.assertRaises(WorktreeError):
            self.backend(retirement=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.manifest_path, {"resources": self.resources})
        with self.assertRaises(WorktreeError):
            self.backend(retirement=True)
        atomic_json(self.manifest_path, self.manifest)
        self.owner["verification"]["endpoint"] = "http://172.26.9.65:32769"
        self.save_owner()
        with self.assertRaises(WorktreeError):
            self.backend(retirement=True)
        self.owner["verification"]["endpoint"] = "http://172.26.9.65:32768"
        self.save_owner()
        self.network["IPAM"]["Config"][0]["Gateway"] = "172.26.9.66"
        with self.assertRaises(WorktreeError):
            self.backend(retirement=True)
        self.network["IPAM"]["Config"][0]["Gateway"] = "172.26.9.65"
        self.container["Id"] = "different-container"
        with self.assertRaises(ValueError):
            self.backend(retirement=True)

    def test_destroy_preserves_confirmation_and_active_session_guards(self):
        backend = self.backend(retirement=True)
        backend.inspect = lambda: {
            "database": {"owner": self.resources["owner"], "sessions": 1},
            "redis": None,
            "buckets": {},
        }
        with self.assertRaises(WorktreeError):
            backend.destroy("wrong")
        with self.assertRaises(WorktreeError):
            backend.destroy(self.resources["confirmation"])

    def test_retirement_flag_is_restricted_to_test_inspection_and_destroy(self):
        from worktree_runtime import parse_arguments

        for command in ("init", "reset", "up"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parse_arguments([command, "--mode", "test", "--retire-finished-bootstrap"])
        with self.assertRaises(SystemExit):
            parse_arguments(["destroy", "--mode", "e2e", "--retire-finished-bootstrap"])
        for command in ("doctor", "destroy"):
            self.assertTrue(
                parse_arguments([command, "--mode", "test", "--retire-finished-bootstrap"])
                .retire_finished_bootstrap
            )


if __name__ == "__main__":
    unittest.main()
