"""Resource and checkout roots are distinct fixture ownership boundaries."""

import os
from pathlib import Path
import tempfile
import unittest

from fixture_suite_coordinator import (
    FixtureSetupRefused,
    _resource_root,
    _verified_resources,
    acquire_store_destruction_lock,
)
from worktree_env import atomic_json, load_identity, state_path, topology


class FixtureResourceRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "owned"
        self.root.mkdir()
        self.resources = topology(
            self.root, load_identity(self.root, create=True), "test"
        )
        self.manifest_path = state_path(self.root, "test", "resources.json")
        atomic_json(self.manifest_path, {"resources": self.resources, "postgres": {}})
        self.environment = {
            "AAP_WORKTREE_MODE": "test",
            "AAP_WORKTREE_RESOURCE_ROOT": str(self.root),
        }

    def test_external_resource_root_selects_its_own_identity_and_manifest(self):
        root, resources, manifest = _verified_resources(self.environment)
        self.assertEqual(root, self.root)
        self.assertEqual(resources, self.resources)
        self.assertEqual(manifest["resources"], self.resources)
        self.assertEqual(_resource_root(resources), self.root)

    def test_missing_relative_or_symlinked_root_cannot_fall_back_to_checkout(self):
        alias = self.root.parent / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for value in (None, "owned", str(alias), str(self.root / "missing")):
            with self.subTest(root=value), self.assertRaises(FixtureSetupRefused):
                _verified_resources(
                    {**self.environment, "AAP_WORKTREE_RESOURCE_ROOT": value}
                )

    def test_mismatched_resource_manifest_is_not_adopted(self):
        atomic_json(
            self.manifest_path, {"resources": {**self.resources, "owner": "other"}}
        )
        with self.assertRaisesRegex(FixtureSetupRefused, "fixture_manifest_mismatch"):
            _verified_resources(self.environment)
        with self.assertRaisesRegex(FixtureSetupRefused, "fixture_manifest_mismatch"):
            _resource_root(self.resources)

    def test_destroy_lock_is_taken_in_the_owned_root(self):
        atomic_json(state_path(self.root, "test", "fixture-store-owner.json"), {})
        lock_path = state_path(self.root, "test", "fixture-store.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        held = acquire_store_destruction_lock(self.resources)
        self.addCleanup(os.close, held)
        self.assertEqual(os.fstat(held).st_ino, lock_path.stat().st_ino)

    def test_destroy_refuses_an_identity_from_another_root(self):
        other = self.root.parent / "other"
        other.mkdir()
        load_identity(other, create=True)
        with self.assertRaisesRegex(FixtureSetupRefused, "fixture_topology_invalid"):
            _resource_root({**self.resources, "root": str(other)})


if __name__ == "__main__":
    unittest.main()
