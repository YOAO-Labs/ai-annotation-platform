"""Real framed inventory/catalog and copy-core proofs, without infrastructure."""

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from app.services import storage_migration_copy as core  # noqa: E402
from app.services import storage_migration_inventory as inventory  # noqa: E402
from app.services import storage_migration_finalization_evidence as evidence  # noqa: E402
from test_storage_migration_inventory import S3  # noqa: E402
from test_storage_migration_copy import Client, TARGET, catalog  # noqa: E402

WINDOW = str(uuid.UUID(int=31))
PARENT = str(uuid.UUID(int=32))
REVISION = 4
SOURCE = {"endpoint": "http://source:9000"}


class Target(Client):
    """The existing exact-version provider fake, retaining actual copied headers."""

    def put_object(self, **kwargs):
        response = super().put_object(**kwargs)
        self.objects[kwargs["Key"]][-1]["headers"] = {
            k: kwargs[k] for k in core.CONTENT_FIELDS if k != "Metadata" and k in kwargs
        }
        return response

    def response(self, kwargs, *, body=False):
        response = super().response(kwargs, body=body)
        saved = next(
            v
            for v in self.objects[kwargs["Key"]]
            if v["version"] == response["VersionId"]
        )
        for field in core.CONTENT_FIELDS:
            if field != "Metadata":
                response.pop(field, None)
        response.update(saved["headers"])
        return response


def reseal(path, change):
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    records, summary = change(records[:-2], records[-2])
    path.unlink()
    writer = inventory._Writer(path)
    try:
        for row in records:
            writer.emit(row)
        writer.finish({k: v for k, v in summary.items() if k != "type"})
    finally:
        writer.stream.close()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-final-evidence-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.number = 0

    def fixture(
        self,
        *,
        policy="migrate",
        version="source-v1",
        marker=False,
        over_budget=False,
        historical=False,
        source_change=None,
    ):
        self.number += 1
        root = self.root / str(self.number)
        root.mkdir(mode=0o700)
        source = S3()
        source.status = "Enabled" if version is not None else None
        source.add("private/原件", b"real content", version=version)
        if marker:
            source.objects[0]["IsLatest"] = False
            source.markers.append(
                {
                    "Key": "private/原件",
                    "VersionId": "marker-v2",
                    "IsLatest": True,
                    "LastModified": "stable",
                }
            )
        purposes = dict.fromkeys(inventory.PURPOSES, "source")
        handling = dict.fromkeys(inventory.PURPOSES, policy)
        limits = core.Limits(max_object_bytes=1 if over_budget else 1024)
        path = root / "source.jsonl"
        inventory.write_inventory(
            path,
            source,
            purposes,
            source=SOURCE,
            observations={
                "migration_window": {
                    "window_id": WINDOW,
                    "window_revision": REVISION,
                    "source_quiescence_proven": False,
                }
            },
        )
        if source_change:
            reseal(path, source_change)
        refs = catalog(root, path, complete=True)
        parent_report, parent_catalog = path, refs
        if historical:
            parent_dir = root / "parent-input"
            parent_dir.mkdir(mode=0o700)
            parent_report = parent_dir / "source.jsonl"
            parent_report.write_bytes(path.read_bytes())
            parent_report.chmod(0o600)

            def previous(records, summary):
                records.extend(
                    [
                        {
                            "type": "issue",
                            "operation": "old",
                            "bucket": "source",
                            "code": "old_issue",
                        },
                        {
                            "type": "multipart",
                            "bucket": "source",
                            "key": "old",
                            "upload_id": "old-upload",
                        },
                        {
                            **deepcopy(
                                next(r for r in records if r["type"] == "object")
                            ),
                            "key": "old-only",
                        },
                    ]
                )
                summary["complete"] = False
                return records, summary

            reseal(parent_report, previous)
            parent_catalog = catalog(parent_dir, parent_report, complete=False)
        parent_path, child_path = root / "parent.sqlite", root / "child.sqlite"
        core.plan(
            parent_path,
            parent_report,
            parent_catalog,
            TARGET,
            handling=handling,
            limits=limits,
        )
        target = Target()
        with core.Checkpoint(parent_path) as parent:
            # A retained old-only record needs no live source read; migrated
            # historical facts are intentionally unexecuted evidence here.
            if not historical:
                core.execute(parent, source, target)
            parent_sha = parent.evidence_digest()
            core.plan_generation(
                child_path,
                path,
                refs,
                TARGET,
                parent=parent,
                parent_evidence_sha256=parent_sha,
                handling=handling,
                limits=limits,
            )
        baseline = child_path.read_bytes()
        with core.Checkpoint(child_path) as child:
            core.execute(child, source, target)
            core.execute(child, source, target, verify_only=True)
            rows = list(child.rows())
            meta = deepcopy(child.meta)
            expected_digest = child.evidence_digest()
        values = {
            "source_report": path.read_bytes(),
            "catalog": refs.read_bytes(),
            "baseline": baseline,
            "source": {**SOURCE, "purpose_buckets": purposes},
            "target": deepcopy(TARGET),
            "handling": handling,
            "limits": asdict(limits),
            "generation": 2,
            "parent_copy_id": PARENT,
            "parent_evidence_sha256": parent_sha,
            "window_id": WINDOW,
            "window_revision": REVISION,
            "deltas": [(row["id"], row, core.digest(row)) for row in rows],
        }
        for name in ("source_report", "catalog", "baseline"):
            values[name + "_sha256"] = hashlib.sha256(values[name]).hexdigest()
        return values, rows, meta, expected_digest

    def build(self, values):
        return evidence.build_final_evidence(
            values, window_id=WINDOW, window_revision=REVISION
        )

    def assert_blocked(self, values, code=None):
        with self.assertRaises(evidence.FinalizationEvidenceError) as caught:
            self.build(values)
        self.assertTrue(caught.exception.code.startswith("finalization_"))
        self.assertEqual(str(caught.exception), caught.exception.code)
        self.assertNotIn("private", str(caught.exception))
        if code:
            self.assertEqual(caught.exception.code, code)

    def update_row(self, values, **updates):
        row = deepcopy(values["deltas"][0][1])
        row.update(updates)
        values["deltas"][0] = (row["id"], row, core.digest(row))

    def update_baseline(self, values, change):
        path = self.root / "mutated.sqlite"
        path.write_bytes(values["baseline"])
        path.chmod(0o600)
        with core.Checkpoint(path) as state:
            change(state)
        values["baseline"] = path.read_bytes()
        values["baseline_sha256"] = hashlib.sha256(values["baseline"]).hexdigest()
        path.unlink()

    def test_actual_core_success_and_logical_digest_are_bounded_private(self):
        values, rows, meta, expected = self.fixture()
        result = self.build(values)
        self.assertEqual(result["schema"], evidence.SCHEMA)
        self.assertEqual(result["evidence_sha256"], expected)
        self.assertEqual(set(result["counts"]), set(evidence.COUNT_KEYS))
        self.assertEqual(result["counts"]["copied_objects"], 1)
        self.assertEqual(result["counts"]["verified_target_bytes"], 12)
        self.assertEqual(result["catalog_summary"]["counts"]["unexpanded"], 1)
        self.assertFalse(result["catalog_summary"]["complete"])
        self.assertEqual(
            set(result["catalog_summary"]),
            {"complete", "enumeration_complete", "resolution_complete", "counts"},
        )
        encoded = json.dumps(result)
        for private in (
            "http://",
            "private",
            "source-v1",
            rows[0]["target_key"],
            "migration_ready",
        ):
            self.assertNotIn(private, encoded)
        self.assertLess(len(encoded), 16384)
        self.assertEqual(meta["handling"], values["handling"])

    def test_reviewed_copy_policy_overrides_unreviewed_inventory(self):
        values, _, _, _ = self.fixture(policy="retain", over_budget=True)
        header = json.loads(values["source_report"].splitlines()[0])
        self.assertEqual(set(header["handling"].values()), {"unreviewed"})
        result = self.build(values)
        self.assertEqual(result["counts"]["retained_objects"], 1)
        self.assertEqual(result["counts"]["over_object_limit"], 1)

    def test_rebuild_and_real_delete_marker_are_explicit_policy_skips(self):
        values, _, _, _ = self.fixture(policy="rebuild", marker=True, over_budget=True)
        result = self.build(values)
        self.assertEqual(result["counts"]["rebuild_objects"], 1)
        self.assertEqual(result["counts"]["current_delete_markers"], 1)
        self.assertEqual(result["counts"]["copied_objects"], 0)

    def test_history_issues_mpu_and_previous_only_are_reported_not_new_gates(self):
        values, _, _, _ = self.fixture(policy="retain", historical=True)
        result = self.build(values)
        self.assertGreaterEqual(result["counts"]["history_records"], 4)
        self.assertEqual(result["counts"]["previous_only_objects"], 1)

    def test_mutable_null_and_unpinned_are_not_source_completeness_gates(self):
        for version, field in (
            ("null", "mutable_null_objects"),
            (None, "unpinned_objects"),
        ):
            with self.subTest(version=version):
                values, _, _, _ = self.fixture(version=version, policy="retain")
                self.assertEqual(self.build(values)["counts"][field], 1)

    def test_raw_sha_and_materialized_delta_checksum_are_checked(self):
        values, _, _, _ = self.fixture()
        for field in ("source_report", "catalog", "baseline"):
            bad = deepcopy(values)
            bad[field] += b"private"
            self.assert_blocked(bad, "finalization_input_integrity")
        values["deltas"][0] = (*values["deltas"][0][:2], "a" * 64)
        self.assert_blocked(values)

    def test_window_values_and_sealed_header_are_both_bound(self):
        values, _, _, _ = self.fixture()
        for field, value in (("window_id", PARENT), ("window_revision", REVISION + 1)):
            bad = deepcopy(values)
            bad[field] = value
            self.assert_blocked(bad, "finalization_window_binding")

        def other_window(records, summary):
            records[0]["observations"]["migration_window"]["window_revision"] += 1
            return records, summary

        values, _, _, _ = self.fixture(source_change=other_window)
        self.assert_blocked(values, "finalization_window_binding")

    def test_frozen_source_target_and_metadata_are_bound(self):
        values, _, _, _ = self.fixture()
        for field, change in (
            ("source", {**values["source"], "endpoint": "http://other:9000"}),
            ("target", {**values["target"], "key_prefix": "other/"}),
            ("parent_evidence_sha256", "b" * 64),
            ("generation", 3),
        ):
            bad = deepcopy(values)
            bad[field] = change
            self.assert_blocked(bad)

    def test_all_nonpolicy_skip_classes_block_even_if_marked_skipped(self):
        values, _, _, _ = self.fixture(policy="retain")
        for decision in (
            "handling_ambiguous",
            "source_unverified",
            "object_byte_budget",
            "source_identity_conflict",
            "unreviewed",
            "failed",
            "other",
        ):
            with self.subTest(decision=decision):
                bad = deepcopy(values)
                # Keep the baseline and materialized immutable row consistent;
                # the final validator, rather than materialize alone, must reject.
                self.update_baseline(
                    bad, lambda state: self.rewrite_decision(state, decision)
                )
                self.update_row(bad, decision=decision, state="skipped")
                self.assert_blocked(bad, "finalization_decision_blocked")

    @staticmethod
    def rewrite_decision(state, decision):
        row = next(state.rows())
        row.update(decision=decision, state="skipped")
        state.save(row)

    def test_migrate_copy_budget_skip_is_not_completion(self):
        values, _, _, _ = self.fixture(over_budget=True)
        self.assert_blocked(values, "finalization_decision_blocked")

    def test_pending_failed_uncertain_or_missing_receipt_never_seal(self):
        values, _, _, _ = self.fixture()
        for updates in (
            {"state": "pending"},
            {"state": "failed"},
            {"state": "uncertain"},
            {"receipt": None},
        ):
            bad = deepcopy(values)
            self.update_row(bad, **updates)
            self.assert_blocked(
                bad,
                "finalization_target_receipt_invalid"
                if "receipt" not in updates
                else None,
            )

    def test_receipt_requires_exact_version_sha_size_metadata_and_time(self):
        values, rows, _, _ = self.fixture()
        for field, value in (
            ("version_id", "null"),
            ("sha256", "a" * 64),
            ("size_bytes", 13),
            ("size_bytes", True),
            ("content_sha256", "a" * 64),
            ("etag", None),
            ("verified_at", "not-time"),
        ):
            with self.subTest(field=field, value=value):
                bad = deepcopy(values)
                receipt = {**rows[0]["receipt"], field: value}
                self.update_row(bad, receipt=receipt)
                self.assert_blocked(bad)

    def test_only_all_nine_consistent_explicit_policies_are_accepted(self):
        values, _, _, _ = self.fixture(policy="retain")
        for change in (
            {},
            {**values["handling"], next(iter(inventory.PURPOSES)): "migrate"},
            dict.fromkeys(inventory.PURPOSES, "unreviewed"),
        ):
            bad = deepcopy(values)
            bad["handling"] = change
            self.assert_blocked(bad, "finalization_handling_invalid")

    def test_source_count_coverage_and_current_issues_or_mpu_block(self):
        def bad_counts(records, summary):
            summary["counts"]["verified_objects"] += 1
            return records, summary

        values, _, _, _ = self.fixture(policy="retain", source_change=bad_counts)
        self.assert_blocked(values, "finalization_source_incomplete")
        for kind in ("issue", "multipart"):

            def extra(records, summary):
                records.append({"type": kind, "bucket": "source", "key": "private"})
                return records, summary

            values, _, _, _ = self.fixture(policy="retain", source_change=extra)
            self.assert_blocked(values, "finalization_source_incomplete")

    def test_missing_or_duplicate_bucket_facts_block(self):
        for duplicate in (False, True):

            def wrong(records, summary):
                bucket = next(r for r in records if r["type"] == "bucket")
                records = [r for r in records if r["type"] != "bucket"]
                if duplicate:
                    records.extend([bucket, deepcopy(bucket)])
                return records, summary

            values, _, _, _ = self.fixture(policy="retain", source_change=wrong)
            self.assert_blocked(values, "finalization_source_incomplete")

    def test_input_and_catalog_binding_never_trust_row_summary(self):
        values, _, _, _ = self.fixture()
        values["summary"] = {"copy_subset_complete": True, "counts": {"verified": 999}}
        self.assertEqual(self.build(values)["counts"]["copied_objects"], 1)
        values["catalog"] = values["catalog"].replace(
            b'"http://source:9000"', b'"http://forged:9000"'
        )
        values["catalog_sha256"] = hashlib.sha256(values["catalog"]).hexdigest()
        self.assert_blocked(values)

    def test_temporary_materialization_is_cleaned_on_success_and_failure(self):
        values, _, _, _ = self.fixture()
        temporary = tempfile.TemporaryDirectory
        created = []

        def tracked(*args, **kwargs):
            result = temporary(*args, **kwargs)
            created.append(Path(result.name))
            return result

        with patch.object(evidence.tempfile, "TemporaryDirectory", side_effect=tracked):
            self.build(values)
            self.update_row(values, state="failed")
            self.assert_blocked(values)
        self.assertTrue(created)
        self.assertTrue(all(not p.exists() for p in created))

    def test_cancellation_is_not_converted_or_used_as_proof(self):
        values, _, _, _ = self.fixture()
        with patch.object(evidence, "verify_inventory", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.build(values)


if __name__ == "__main__":
    unittest.main()
