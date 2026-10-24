"""Offline prediction locator mapping preserves receipts and historical provenance."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from worktree_env import WorktreeError
from worktree_recovery_mapping import identity, prediction_input_changes


class PredictionInputMapping(unittest.TestCase):
    def row(self):
        identifier = uuid.uuid4()
        sha = hashlib.sha256(b"input").hexdigest()
        return SimpleNamespace(
            id=identifier,
            contract_version=2,
            bucket="source-import",
            storage_identity=identity("http://saved.test:9000", "source-import"),
            maintenance_revision=3,
            object_count=1,
            size_bytes=5,
            source_ref={"bucket": "historical-source", "sha256": "a" * 64},
            inventory={
                "schema": 1,
                "objects": {
                    "original/" + sha + ".png": {
                        "sha256": sha,
                        "size_bytes": 5,
                        "media_type": "image/png",
                        "write_nonce": str(uuid.uuid4()),
                        "state": "verified",
                        "etag": '"historical-etag"',
                        "version_id": "historical-version",
                    }
                },
            },
            storage_ref={
                "schema": 1,
                "purpose": "import",
                "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
                "config_revision": 1,
                "binding_generation": 1,
                "endpoint_url": "http://saved.test:9000",
                "bucket": "source-import",
                "key_prefix": "prediction-inputs/" + str(identifier) + "/",
                "storage_identity": identity("http://saved.test:9000", "source-import"),
            },
        )

    def mapping(self, row):
        from app.services import prediction_inputs

        with patch.object(
            prediction_inputs, "_endpoint", return_value="http://saved.test:9000"
        ):
            return prediction_input_changes(
                row,
                source_bucket="source-import",
                target_bucket="target-import",
                endpoint_url="http://saved.test:9000",
            )

    def test_verified_and_planned_receipts_are_never_rewritten_or_resigned(self):
        for state in ("planned", "verified"):
            row = self.row()
            entry = next(iter(row.inventory["objects"].values()))
            entry["state"] = state
            if state == "planned":
                entry["etag"] = entry["version_id"] = None
            before = deepcopy(vars(row))
            values = self.mapping(row)
            self.assertEqual(set(values), {"bucket", "storage_identity", "storage_ref"})
            self.assertEqual(vars(row), before)
            self.assertEqual(
                values["storage_ref"]["key_prefix"], row.storage_ref["key_prefix"]
            )
            self.assertEqual(values["storage_ref"]["bucket"], "target-import")
            self.assertEqual(
                values["storage_ref"]["storage_identity"], values["storage_identity"]
            )
            self.assertNotIn("inventory", values)
            self.assertNotIn("source_ref", values)

    def test_legacy_changes_only_existing_mirrors_without_inventing_pin(self):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            contract_version=1,
            bucket="source-import",
            storage_identity=identity("http://saved.test:9000", "source-import"),
            storage_ref=None,
            inventory=None,
            maintenance_revision=None,
            source_ref=None,
        )
        values = self.mapping(row)
        self.assertEqual(set(values), {"bucket", "storage_identity"})
        self.assertIsNone(row.storage_ref)
        self.assertIsNone(row.inventory)

    def test_unknown_scope_prefix_and_receipt_refuse_before_mutation(self):
        for changed in ("prefix", "bucket", "sha", "version", "legacy_pin", "contract"):
            row = self.row()
            if changed == "prefix":
                row.storage_ref["key_prefix"] = (
                    "prediction-inputs/" + str(uuid.uuid4()) + "/"
                )
            elif changed == "bucket":
                row.storage_ref["bucket"] = "unknown"
            elif changed == "sha":
                next(iter(row.inventory["objects"].values()))["sha256"] = "b" * 64
            elif changed == "version":
                next(iter(row.inventory["objects"].values()))["version_id"] = (
                    "bad\nversion"
                )
            elif changed == "legacy_pin":
                row.contract_version = 1
            else:
                row.contract_version = True
            before = deepcopy(vars(row))
            with self.assertRaises(WorktreeError):
                self.mapping(row)
            self.assertEqual(vars(row), before)


if __name__ == "__main__":
    unittest.main()
