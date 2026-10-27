"""Read mappings derive from real closed copy/finalization checkpoints."""

from copy import deepcopy
import unittest

from app.services import storage_migration_copy as core
from app.services import storage_migration_read_evidence as evidence
from tests import test_storage_migration_finalization_evidence as fixtures


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.EvidenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def build(self, values):
        return evidence.build_read_mapping(
            values, window_id=fixtures.WINDOW, window_revision=fixtures.REVISION
        )

    def test_preserves_exact_source_version_and_inherited_creation_receipt(self):
        values, rows, meta, digest = self.fixture.fixture()
        result = self.build(values)
        entry = result["entries"][0]
        row = rows[0]
        self.assertEqual(result["schema"], evidence.SCHEMA)
        self.assertEqual(result["evidence"]["evidence_sha256"], digest)
        self.assertEqual(entry["source_version_id"], "source-v1")
        self.assertEqual(entry["source_object_id"], row["id"])
        self.assertEqual(entry["target_version_id"], row["receipt"]["version_id"])
        self.assertEqual(entry["target_etag"], row["receipt"]["etag"])
        self.assertEqual(entry["creation_nonce"], row["nonce"])
        self.assertEqual(entry["creation_plan_uuid"], row["creation_plan_uuid"])
        self.assertNotEqual(entry["creation_plan_uuid"], meta["plan_uuid"])
        self.assertEqual(
            entry["target_content_sha256"], row["receipt"]["content_sha256"]
        )
        self.assertEqual(
            result["mapping_sha256"], evidence.mapping_digest(result["entries"])
        )
        self.assertEqual(self.build(values), result)

    def test_source_unversioned_and_mutable_null_still_pin_new_target_version(self):
        for version in (None, "null"):
            with self.subTest(version=version):
                values, _, _, _ = self.fixture.fixture(version=version)
                entry = self.build(values)["entries"][0]
                self.assertEqual(entry["source_version_id"], version)
                self.assertNotIn(entry["target_version_id"], (None, "", "null"))

    def test_historical_only_facts_do_not_become_target_routes(self):
        values, rows, _, _ = self.fixture.fixture(historical=True)
        result = self.build(values)
        self.assertGreater(result["evidence"]["counts"]["history_records"], 0)
        self.assertGreater(result["evidence"]["counts"]["previous_only_objects"], 0)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["source_object_id"], rows[0]["id"])

    def test_retained_rebuild_and_delete_marker_remain_evidence_only(self):
        for policy in ("retain", "rebuild"):
            with self.subTest(policy=policy):
                values, _, _, _ = self.fixture.fixture(policy=policy, marker=True)
                result = self.build(values)
                self.assertEqual(result["entries"], [])
                self.assertEqual(result["evidence"]["counts"]["current_objects"], 1)
                self.assertEqual(
                    result["evidence"]["counts"]["current_delete_markers"], 1
                )

    def test_invalid_current_receipt_cannot_publish_historical_success(self):
        values, _, _, _ = self.fixture.fixture()
        row = deepcopy(values["deltas"][0][1])
        row["receipt"]["version_id"] = "null"
        values["deltas"][0] = (row["id"], row, core.digest(row))
        with self.assertRaises(evidence.FinalizationEvidenceError):
            self.build(values)

    def test_source_conflict_still_fails_full_evidence_validation(self):
        values, _, _, _ = self.fixture.fixture()
        self.fixture.update_row(values, sha256="a" * 64)
        with self.assertRaises(evidence.FinalizationEvidenceError):
            self.build(values)

    def test_mapping_hash_frames_maps_larger_than_one_inventory_record(self):
        values, _, _, _ = self.fixture.fixture()
        row = self.build(values)["entries"][0]
        entries = [
            dict(row, source_object_id=f"{number:064x}") for number in range(1000)
        ]
        with self.assertRaises(ValueError):
            core.digest(entries)
        actual = evidence.mapping_digest(entries)
        self.assertEqual(evidence.mapping_digest(reversed(entries)), actual)
        entries[-1]["target_etag"] = '"changed"'
        self.assertNotEqual(evidence.mapping_digest(entries), actual)
