"""Offline prediction locator mapping preserves receipts and historical provenance."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
            owner_kind="predict_test",
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

    def video_row(self, state="verified"):
        row = self.row()
        row.contract_version, row.owner_kind = 3, "video_predict_test"
        old = next(iter(row.inventory["objects"].values()))
        sha = old["sha256"]
        old.update(
            media_type="application/octet-stream",
            container="mov",
            probe={
                "probe_contract": 1,
                "video": {
                    "duration_ms": 2000,
                    "fps": 3.0,
                    "frame_count": 6,
                    "width": 64,
                    "height": 48,
                    "codec": "h264",
                },
            },
            state=state,
        )
        if state == "planned":
            old["etag"] = old["version_id"] = None
        row.inventory["objects"] = {"video/" + sha + ".mov": old}
        row.owner_id, row.task_id = uuid.uuid4(), uuid.uuid4()
        row.created_at = datetime(2026, 10, 25, tzinfo=timezone.utc)
        row.expires_at = row.created_at + timedelta(hours=2)
        row.retain_until = row.expires_at + timedelta(days=7)
        row.next_cleanup_at = row.expires_at + timedelta(hours=1)
        row.state, row.deleted_at = "cleanup_pending", None
        row.source_ref = {
            "task_id": str(row.task_id),
            "version": 9,
            "original": {
                "schema": 3,
                "bucket": "historical-video-source",
                "sha256": sha,
                "size_bytes": row.size_bytes,
                "facts_digest": "b" * 64,
            },
            "authority": {
                "actor_id": str(uuid.uuid4()),
                "scopes": ["annotations:write"],
            },
        }
        return row

    def test_v3_verified_planned_and_deleted_preserve_all_historical_facts(self):
        for state, deleted in (
            ("verified", False),
            ("planned", False),
            ("verified", True),
        ):
            row = self.video_row(state)
            if deleted:
                row.state, row.deleted_at = "deleted", row.retain_until
            before = deepcopy(vars(row))
            values = self.mapping(row)
            self.assertEqual(vars(row), before)
            self.assertEqual(set(values), {"bucket", "storage_identity", "storage_ref"})
            self.assertEqual(
                values["storage_ref"],
                {
                    **before["storage_ref"],
                    "bucket": "target-import",
                    "storage_identity": identity(
                        "http://saved.test:9000", "target-import"
                    ),
                },
            )
            self.assertNotIn("source_ref", values)
            self.assertNotIn("inventory", values)
            self.assertNotIn("expires_at", values)
            self.assertNotIn("deleted_at", values)

    def test_v3_unknown_owner_probe_or_legacy_disguise_refuse_without_mutation(self):
        for change in (
            "owner",
            "legacy",
            "unknown_contract",
            "bool_contract",
            "probe",
            "nonce",
        ):
            row = self.video_row()
            entry = next(iter(row.inventory["objects"].values()))
            if change == "owner":
                row.owner_kind = "predict_test"
            elif change == "legacy":
                row.contract_version = 1
            elif change == "unknown_contract":
                row.contract_version = 4
            elif change == "bool_contract":
                row.contract_version = True
            elif change == "probe":
                entry["probe"]["video"]["frame_count"] = True
            else:
                entry["write_nonce"] = "guessed-owner"
            before = deepcopy(vars(row))
            with self.assertRaises(WorktreeError):
                self.mapping(row)
            self.assertEqual(vars(row), before)

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

    def test_mask_repair_mapping_preserves_frozen_plan_binding_and_authority(self):
        row = self.row()
        row.owner_kind = "mask_repair"
        row.owner_id, row.task_id = uuid.uuid4(), uuid.uuid4()
        row.initial_expires_at = "2026-10-25T01:00:00+00:00"
        row.source_ref = {
            "binding": {
                "task_id": str(row.task_id),
                "version": 7,
                "original": {
                    "bucket": "historical-originals",
                    "sha256": "a" * 64,
                    "facts_digest": "b" * 64,
                    "facts": {"saved_bucket": "historical-originals"},
                },
            },
            "batch_id": str(uuid.uuid4()),
            "plan_digest": "c" * 64,
            "unit_digest": "d" * 64,
        }
        before = deepcopy(vars(row))
        values = self.mapping(row)
        self.assertEqual(set(values), {"bucket", "storage_identity", "storage_ref"})
        self.assertEqual(vars(row), before)
        self.assertEqual(
            row.source_ref["binding"]["original"]["bucket"], "historical-originals"
        )
        self.assertNotIn("source_ref", values)
        self.assertNotIn("initial_expires_at", values)
        self.assertNotIn("owner_id", values)

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
