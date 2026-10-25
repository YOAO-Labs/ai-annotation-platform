"""Report framing, descriptor ownership and exact opaque locator joins."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import storage_migration_references as core
from storage_migration_inventory import PURPOSES, _Writer


def source_file(path, objects=(), *, address="http://source:9000", complete=True):
    writer = _Writer(path)
    try:
        writer.emit(
            {
                "type": "header",
                "schema": "aap.storage-migration-inventory/v1",
                "source": {"endpoint": address},
                "purpose_buckets": dict.fromkeys(PURPOSES, "bucket"),
            }
        )
        for row in objects:
            writer.emit(
                {
                    "type": "object",
                    "bucket": "bucket",
                    "key": "raw/key",
                    "version_id": "v1",
                    "size_bytes": 3,
                    "etag": '"opaque"',
                    "verification": {
                        "status": "verified",
                        "sha256": hashlib.sha256(b"abc").hexdigest(),
                    },
                    **row,
                }
            )
        writer.finish({"complete": complete, "migration_ready": False})
    finally:
        writer.stream.close()


def reference(**changes):
    return {
        "type": "reference",
        "family": "fixture",
        "owner_id": ["logical-owner"],
        "role": "saved_owner",
        "endpoint_url": "http://source:9000",
        "bucket": "bucket",
        "key": "raw/key",
        "version_id": "v1",
        **changes,
    }


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-ref-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.jsonl"

    @contextmanager
    def index(self, objects=(), **kwargs):
        source_file(self.source, objects, **kwargs)
        index = core.SourceIndex(
            self.root / "source.sqlite", self.source, core.Budget(core.Limits())
        )
        try:
            yield index
        finally:
            index.close()

    def test_exact_version_has_independent_sha_and_etag(self):
        with self.index([{}]) as index:
            value = index.match(
                reference(sha256=hashlib.sha256(b"abc").hexdigest(), size_bytes=3)
            )
            self.assertEqual(value["status"], "exact_version")
            self.assertEqual(value["source_etag"], '"opaque"')
            self.assertNotEqual(value["source_sha256"], value["source_etag"])

    def test_endpoint_bucket_and_version_traps_do_not_match(self):
        with self.index([{}]) as index:
            for changes, expected in [
                ({"endpoint_url": "http://other:9000"}, "outside_selected_source"),
                ({"bucket": "another"}, "outside_selected_source"),
                ({"version_id": "newer"}, "not_observed"),
            ]:
                with self.subTest(changes=changes):
                    self.assertEqual(
                        index.match(reference(**changes))["status"], expected
                    )

    def test_null_slot_is_mutable_and_not_an_unpinned_or_immutable_version(self):
        with self.index([{"version_id": "null"}, {"version_id": "v1"}]) as index:
            self.assertEqual(
                index.match(reference(version_id="null"))["status"], "mutable_null"
            )
            self.assertEqual(
                index.match(reference(version_id=None)),
                {"status": "unpinned_key", "observed_versions": 2},
            )
            self.assertEqual(core.version_kind(None), "unpinned")
            self.assertEqual(core.version_kind("null"), "mutable_null")
            self.assertEqual(core.version_kind("v1"), "explicit_version")

    def test_raw_keys_are_not_decoded_or_normalized(self):
        with self.index([{"key": "raw/%2F/../图 %.bin"}]) as index:
            self.assertEqual(
                index.match(reference(key="raw/%2F/../图 %.bin"))["status"],
                "exact_version",
            )
            for key in ["raw///../图 %.bin", "raw/图 %.bin", "raw/%2f/../图 %.bin"]:
                self.assertEqual(
                    index.match(reference(key=key))["status"], "not_observed"
                )

    def test_prefix_uses_literal_boundary_and_not_sql_wildcards(self):
        with self.index(
            [{"key": "raw_%/x"}, {"key": "raw_A/x"}, {"key": "raw_%2/x"}]
        ) as index:
            self.assertEqual(
                index.match(reference(key=None, prefix="raw_%/"))["observed_objects"], 1
            )
            self.assertEqual(
                index.match(reference(key=None, prefix="raw_%"))["status"], "unlocated"
            )
            self.assertEqual(
                index.match(reference(key=None, prefix="absent/"))["status"],
                "not_observed",
            )

    def test_byte_claim_conflicts_are_not_hidden_by_equal_etag(self):
        with self.index([{}]) as index:
            for changes in ({"sha256": "0" * 64}, {"size_bytes": 4}):
                self.assertEqual(
                    index.match(reference(**changes))["status"], "conflict"
                )

    def test_unverified_object_is_not_a_verified_reference(self):
        with self.index(
            [{"verification": {"status": "unverified", "code": "denied"}}]
        ) as index:
            self.assertEqual(index.match(reference())["status"], "source_unverified")

    def test_delete_marker_is_not_object_content(self):
        with self.index([{"type": "delete_marker"}]) as index:
            self.assertEqual(index.match(reference())["status"], "conflict")

    def test_duplicate_source_identity_is_refused(self):
        source_file(self.source, [{}, {}])
        with self.assertRaisesRegex(core.ReferenceError, "source_duplicate_identity"):
            core.SourceIndex(
                self.root / "source.sqlite", self.source, core.Budget(core.Limits())
            )

    def test_saved_endpoint_wins_and_missing_location_does_not_drop_history(self):
        ref = reference(location_id="gone")
        located = core.address_reference(ref, {}, "http://other:9000")
        self.assertEqual(located["endpoint_url"], ref["endpoint_url"])
        self.assertEqual(located["address_basis"], "saved_endpoint")

    def test_identity_only_claim_requires_known_endpoint_hash(self):
        ref = reference(
            endpoint_url=None,
            location_id="L",
            storage_identity=core.identity("http://history:9000", "bucket"),
        )
        located = core.address_reference(
            ref, {"L": {"endpoint_url": "http://today:9000"}}, "http://source:9000"
        )
        self.assertEqual(located["unknown"], "endpoint_unlocated")
        located = core.address_reference(ref, {}, "http://history:9000")
        self.assertEqual(located["endpoint_url"], "http://history:9000")

    def test_creation_history_is_not_a_current_address_fallback(self):
        current = reference(
            endpoint_url=None,
            role="current_locator",
            location_id="L",
            storage_identity="f" * 64,
        )
        located = core.address_reference(
            current,
            {"L": {"endpoint_url": "http://source:9000"}},
            "http://history:9000",
        )
        self.assertEqual(located["unknown"], "endpoint_unlocated")

    def test_bad_saved_endpoint_never_exports_signature_or_userinfo(self):
        for endpoint in ("http://u:secret@host", "http://host?token=secret"):
            located = core.address_reference(
                reference(endpoint_url=endpoint), {}, "http://source:9000"
            )
            self.assertNotIn("secret", json.dumps(located))
            self.assertEqual(located["unknown"], "invalid_saved_endpoint")

    def test_saved_identity_conflict_is_not_current_config_adoption(self):
        located = core.address_reference(
            reference(storage_identity="f" * 64), {}, "http://source:9000"
        )
        self.assertEqual(located["unknown"], "saved_identity_conflict")

    def test_unknown_current_saved_reference_is_never_filled_from_registration(self):
        for code in (
            "unknown_saved_schema",
            "incomplete_saved_reference",
            "saved_locator_conflict",
        ):
            with self.subTest(code=code):
                located = core.address_reference(
                    reference(
                        endpoint_url=None,
                        role="current_locator",
                        location_id="L",
                        unknown=code,
                    ),
                    {"L": {"endpoint_url": "http://today:9000"}},
                    "http://source:9000",
                )
                self.assertIsNone(located.get("endpoint_url"))
                self.assertEqual(located["unknown"], code)
                self.assertNotIn("address_basis", located)

    def test_object_root_metadata_is_safe_and_does_not_conflict_with_exact_key(self):
        with self.index([{}]) as index:
            value = core.address_reference(
                reference(key_prefix="raw/", prefix=None), {}, "http://source:9000"
            )
            self.assertEqual(index.match(value)["status"], "exact_version")
            value = core.address_reference(
                reference(key_prefix={"secret": "private-value"}),
                {},
                "http://source:9000",
            )
            self.assertNotIn("private-value", json.dumps(value))
            self.assertEqual(value["unknown"], "invalid_locator_field")

    def test_source_path_replacement_does_not_change_verified_descriptor(self):
        source_file(self.source, [{}])
        replacement = self.root / "replacement.jsonl"
        source_file(replacement, [{"key": "different"}])
        original = Path.open
        opens = []

        def opening(path, *args, **kw):
            stream = original(path, *args, **kw)
            if path == self.source:
                opens.append(path)
                os.replace(replacement, self.source)
            return stream

        with patch.object(Path, "open", opening):
            index = core.SourceIndex(
                self.root / "source.sqlite", self.source, core.Budget(core.Limits())
            )
        try:
            self.assertEqual(len(opens), 1)
            self.assertEqual(index.match(reference())["status"], "exact_version")
            self.assertEqual(
                index.match(reference(key="different"))["status"], "not_observed"
            )
        finally:
            index.close()

    def test_in_place_source_change_is_refused(self):
        source_file(self.source, [{}])
        original = core.framed

        def changing(*args, **kwargs):
            for record in original(*args, **kwargs):
                if record["type"] == "verified_tail":
                    os.utime(self.source, ns=(1, 1))
                yield record

        with (
            patch.object(core, "framed", changing),
            self.assertRaisesRegex(core.ReferenceError, "source_changed"),
        ):
            core.SourceIndex(
                self.root / "source.sqlite", self.source, core.Budget(core.Limits())
            )

    def catalog(self, records, **limits):
        source_file(self.source, [{}])
        spool = self.root / "spool.jsonl"
        spool.write_bytes(b"".join(core.canonical(row) for row in records))
        output = self.root / "references.jsonl"
        summary = core.write_catalog(
            output,
            spool,
            {"enumeration_complete": True, "families": ["fixture"]},
            self.source,
            self.root,
            core.Budget(core.Limits(**limits)),
        )
        return output, summary

    def test_report_is_private_exclusive_and_seals_unknowns(self):
        path, summary = self.catalog([reference(version_id=None)])
        self.assertFalse(summary["complete"])
        self.assertTrue(summary["enumeration_complete"])
        self.assertFalse(summary["migration_ready"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(core.verify(path)["counts"]["references"], 1)
        with self.assertRaises(FileExistsError):
            core.Writer(path, 65536)

    def test_exact_reference_can_complete_catalog_but_never_migration(self):
        path, summary = self.catalog([reference()])
        self.assertTrue(summary["complete"])
        self.assertFalse(summary["migration_ready"])
        self.assertEqual(core.verify(path)["source_objects_without_exact_match"], 0)

    def test_unexpanded_documents_stay_unknown_and_no_match_is_not_orphan(self):
        path, summary = self.catalog([{"type": "unexpanded", "document": "snapshot"}])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["counts"]["unexpanded"], 1)
        self.assertFalse(summary["resolution_complete"])
        self.assertEqual(summary["source_objects_without_exact_match"], 1)
        self.assertIn("never orphan", summary["no_match_meaning"])
        self.assertFalse(core.verify(path)["migration_ready"])

    def test_output_budget_still_seals_incomplete_report(self):
        path, summary = self.catalog(
            [reference(owner_id=[str(i)]) for i in range(1000)], max_output_bytes=65536
        )
        self.assertFalse(summary["enumeration_complete"])
        self.assertEqual(summary["counts"]["issues"], 1)
        self.assertLessEqual(path.stat().st_size, 65536)
        self.assertFalse(core.verify(path)["complete"])
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            summary["counts"]["references"],
            sum(row["type"] == "reference" for row in rows),
        )

    def test_source_budget_seals_unexpanded_snapshot_without_false_digest(self):
        path, summary = self.catalog([reference()], max_source_bytes=1)
        self.assertFalse(summary["complete"])
        self.assertFalse(summary["resolution_complete"])
        self.assertEqual(summary["counts"]["references"], 0)
        self.assertEqual(summary["counts"]["unexpanded"], 1)
        self.assertEqual(core.verify(path)["counts"]["issues"], 1)
        header = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(header["source_report"], {"verified": False})

    def test_expired_join_budget_is_sealed_without_claiming_matches(self):
        source_file(self.source, [{}])
        spool = self.root / "spool.jsonl"
        spool.write_bytes(core.canonical(reference()))
        budget = core.Budget(core.Limits())
        budget.started -= 1000
        output = self.root / "expired.jsonl"
        summary = core.write_catalog(
            output,
            spool,
            {"enumeration_complete": True},
            self.source,
            self.root,
            budget,
        )
        self.assertFalse(summary["complete"])
        self.assertFalse(core.verify(output)["resolution_complete"])
        self.assertIn(b'"code":"time_budget"', output.read_bytes())

    def test_invalid_receipt_is_unknown_and_raw_value_is_removed(self):
        value = core.address_reference(
            reference(sha256="private-payload", size_bytes=True),
            {},
            "http://source:9000",
        )
        self.assertNotIn("sha256", value)
        self.assertNotIn("size_bytes", value)
        self.assertEqual(value["unknown"], "invalid_content_receipt")

    def test_tampered_or_unsealed_report_is_refused(self):
        path, _ = self.catalog([reference()])
        data = path.read_bytes()
        for changed in (
            data[:-1],
            data.replace(b'"migration_ready":false', b'"migration_ready":true'),
            data + b"{}\n",
        ):
            path.write_bytes(changed)
            with self.assertRaises(core.ReferenceError):
                core.verify(path)

    def test_second_source_header_cannot_change_join_endpoint(self):
        writer = _Writer(self.source)
        try:
            for address in ("http://source:9000", "http://other:9000"):
                writer.emit(
                    {
                        "type": "header",
                        "schema": "aap.storage-migration-inventory/v1",
                        "source": {"endpoint": address},
                        "purpose_buckets": dict.fromkeys(PURPOSES, "bucket"),
                    }
                )
            writer.finish({"complete": True, "migration_ready": False})
        finally:
            writer.stream.close()
        with self.assertRaisesRegex(core.ReferenceError, "invalid_report"):
            core.SourceIndex(
                self.root / "index.sqlite", self.source, core.Budget(core.Limits())
            )

    def test_source_entry_and_byte_budgets_refuse_before_report(self):
        source_file(self.source, [{}, {"key": "other"}])
        for i, limits in enumerate(
            (core.Limits(max_source_entries=1), core.Limits(max_source_bytes=1))
        ):
            with self.assertRaises(core.ReferenceError):
                core.SourceIndex(
                    self.root / f"source{i}.sqlite", self.source, core.Budget(limits)
                )

    def test_limits_reject_nonfinite_bool_and_unbounded_page(self):
        for changes in (
            {"max_rows": True},
            {"max_seconds": float("nan")},
            {"page_size": 1001},
            {"max_output_bytes": 1},
        ):
            with self.assertRaises(core.ReferenceError):
                core.Limits(**changes)


if __name__ == "__main__":
    unittest.main()
