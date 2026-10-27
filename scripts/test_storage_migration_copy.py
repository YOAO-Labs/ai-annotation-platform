"""Initial-copy evidence, crash windows and bounded resource ownership."""

import hashlib
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import storage_migration_copy as core
import inventory_storage_references as reference_cli
import storage_migration_references as references
from storage_migration_inventory import PURPOSES, SCHEMA, _Writer
from storage_migration_references import Writer, SCHEMA as REF_SCHEMA

BODY = b"abc"
SHA = hashlib.sha256(BODY).hexdigest()
TARGET = {
    "endpoint_url": "http://target:9000",
    "bucket": "target",
    "key_prefix": "copy/",
    "access_key_id": "private-access",
    "secret_access_key": "private-secret",
}


def inputs(root, rows=None, *, complete=True, handling=None):
    source = root / "source.jsonl"
    writer = _Writer(source)
    try:
        writer.emit(
            {
                "type": "header",
                "schema": SCHEMA,
                "source": {"endpoint": "http://source:9000"},
                "purpose_buckets": dict.fromkeys(PURPOSES, "source"),
                "handling": handling or dict.fromkeys(PURPOSES, "migrate"),
            }
        )
        for row in rows if rows is not None else [{}]:
            writer.emit(
                {
                    "type": "object",
                    "bucket": "source",
                    "key": "raw/%2F/../图 %.bin",
                    "version_id": "v1",
                    "size_bytes": len(BODY),
                    "etag": '"opaque"',
                    "verification": {"status": "verified", "sha256": SHA},
                    **row,
                }
            )
        writer.finish({"complete": complete, "migration_ready": False})
    finally:
        writer.stream.close()
    return source, catalog(root, source, complete=complete)


def catalog(root, source, *, complete=True):
    path = root / "references.jsonl"
    writer = Writer(path, 65536)
    try:
        writer.emit(
            {
                "type": "header",
                "schema": REF_SCHEMA,
                "source_report": {
                    "endpoint": "http://source:9000",
                    "complete": complete,
                    "file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
            }
        )
        writer.emit({"type": "unexpanded", "document": "legacy_document"})
        writer.finish(
            {
                "complete": False,
                "enumeration_complete": True,
                "resolution_complete": False,
                "counts": {"unexpanded": 1},
                "migration_ready": False,
            }
        )
    finally:
        writer.stream.close()
    return path


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Body(io.BytesIO):
    def __init__(self, value, *, fail_close=False):
        super().__init__(value)
        self.was_closed = False
        self.fail_close = fail_close

    def close(self):
        self.was_closed = True
        super().close()
        if self.fail_close:
            raise OSError("private failure")


class Client:
    def __init__(self, *, source=False):
        self.source, self.objects, self.bodies, self.puts = source, {}, [], 0
        self.fail_get = self.fail_close = self.lose_ack = False
        self.versioning = "Enabled"
        self.calls = []

    def get_bucket_versioning(self, **kwargs):
        return {"Status": self.versioning}

    def response(self, kwargs, *, body=False):
        self.calls.append(dict(kwargs))
        key = kwargs["Key"]
        if self.source:
            value, metadata, version = (
                BODY,
                {"source": "actual value"},
                kwargs.get("VersionId"),
            )
        else:
            candidates = self.objects.get(key, [])
            candidates = [
                v
                for v in candidates
                if kwargs.get("VersionId", v["version"]) == v["version"]
            ]
            if not candidates:
                raise S3Error("NoSuchKey")
            latest = candidates[-1]
            value, metadata, version = (
                latest["body"],
                latest["metadata"],
                latest["version"],
            )
        response = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "ContentLength": len(value),
            "ETag": '"opaque"',
            "VersionId": version,
            "ContentType": "application/octet-stream",
            "CacheControl": "max-age=17",
            "Metadata": metadata,
            "LastModified": "stable",
        }
        if body:
            response["Body"] = Body(value, fail_close=self.fail_close)
            self.bodies.append(response["Body"])
        return response

    def head_object(self, **kwargs):
        return self.response(kwargs)

    def get_object(self, **kwargs):
        if self.fail_get:
            raise S3Error("AccessDenied")
        return self.response(kwargs, body=True)

    def put_object(self, **kwargs):
        self.puts += 1
        if kwargs["Key"] in self.objects:
            raise S3Error("PreconditionFailed")
        assert kwargs["IfNoneMatch"] == "*"
        stream = kwargs["Body"]
        chunks = []
        while part := stream.read(2):
            chunks.append(part)
        value = {
            "body": b"".join(chunks),
            "metadata": kwargs["Metadata"],
            "version": "actual-target-" + str(self.puts),
        }
        self.objects[kwargs["Key"]] = [value]
        if self.lose_ack:
            raise OSError("private secret error")
        return {"VersionId": value["version"]}

    def list_object_versions(self, **kwargs):
        return {
            "Versions": [
                {"Key": key, "VersionId": value["version"]}
                for key, values in self.objects.items()
                for value in values
                if key.startswith(kwargs["Prefix"])
            ]
        }


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aap-copy-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "checkpoint.sqlite"
        self.source, self.target = Client(source=True), Client()

    def plan(self, rows=None, **kwargs):
        paths = inputs(
            self.root,
            rows,
            **{k: kwargs.pop(k) for k in ("complete", "handling") if k in kwargs},
        )
        return core.plan(self.path, *paths, TARGET, **kwargs)

    def run_copy(self, **kwargs):
        with core.Checkpoint(self.path) as state:
            return core.execute(state, self.source, self.target, **kwargs)

    def rows(self):
        with core.Checkpoint(self.path) as state:
            return list(state.rows())

    def test_two_source_versions_get_distinct_raw_locator_addresses(self):
        self.plan([{}, {"version_id": "v2"}])
        rows = self.rows()
        self.assertNotEqual(rows[0]["target_key"], rows[1]["target_key"])
        for row in rows:
            self.assertEqual(row["source"]["key"], "raw/%2F/../图 %.bin")
            self.assertTrue(row["target_key"].endswith("/" + SHA))
            self.assertIn("/" + core.digest(row["source"]) + "/", row["target_key"])

    def test_sealed_incomplete_inputs_allow_only_copy_subset(self):
        result = self.plan(complete=False)
        self.assertFalse(result["source"]["complete"])
        self.assertFalse(result["catalog"]["complete"])
        self.assertFalse(self.run_copy()["migration_ready"])

    def test_catalog_is_bound_to_full_source_digest(self):
        paths = inputs(self.root)
        paths[0].write_bytes(
            paths[0].read_bytes().replace(b'"complete":true', b'"complete":false')
        )
        with self.assertRaises(core.ReferenceError):
            core.plan(self.path, *paths, TARGET)

    def test_other_valid_source_cannot_adopt_original_catalog(self):
        paths = inputs(self.root)
        old_catalog = paths[1].read_bytes()
        for path in paths:
            path.unlink()
        paths = inputs(self.root, [{"key": "different"}])
        paths[1].write_bytes(old_catalog)
        with self.assertRaisesRegex(core.CopyError, "catalog_source_mismatch"):
            core.plan(self.path, *paths, TARGET)

    def test_unsealed_catalog_is_refused(self):
        paths = inputs(self.root)
        paths[1].write_bytes(paths[1].read_bytes().rsplit(b"\n", 2)[0] + b"\n")
        with self.assertRaises(core.ReferenceError):
            core.plan(self.path, *paths, TARGET)

    def test_unknown_business_reference_does_not_block_known_body(self):
        self.plan()
        result = self.run_copy()
        self.assertEqual(result["counts"], {"verified": 1})
        self.assertEqual(result["catalog"]["counts"]["unexpanded"], 1)

    def test_current_catalog_physical_roots_feed_existing_copy_checkpoint(self):
        prefix = "公司/%2F/_aap_managed/media-cache/thumbnails/objects/I/"
        key = prefix + "thumbnail.webp"
        source, catalog_path = inputs(
            self.root, [{"key": key}, {"key": "without-current-reference"}]
        )
        catalog_path.unlink()
        family = next(
            item for item in reference_cli.FAMILIES if item.table == "thumbnail_outputs"
        )
        ref = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": "saved-location",
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": "http://source:9000",
            "bucket": "source",
            "prefix": prefix,
            "key_prefix": "公司/%2F/_aap_managed/media-cache/",
            "storage_identity": references.identity("http://source:9000", "source"),
        }
        record = next(
            reference_cli.references(
                family,
                {
                    "id": "I",
                    "storage_ref": ref,
                    "ref_present": True,
                    "version_id": "v1",
                    "sha256": SHA,
                    "size_bytes": len(BODY),
                },
            )
        )
        spool = self.root / "spool.jsonl"
        records = [
            record,
            {
                **record,
                "owner_id": ["other-endpoint"],
                "endpoint_url": "http://other:9000",
                "storage_identity": references.identity("http://other:9000", "source"),
            },
            {
                **record,
                "owner_id": ["prefix-only"],
                "key": None,
                "prefix": prefix,
                "sha256": None,
                "size_bytes": None,
                "version_id": None,
            },
            {
                **record,
                "owner_id": ["unknown-schema"],
                "unknown": "unknown_saved_schema",
            },
        ]
        spool.write_bytes(b"".join(references.canonical(row) for row in records))
        result = references.write_catalog(
            catalog_path,
            spool,
            {"enumeration_complete": True, "families": [family.table]},
            source,
            self.root,
            references.Budget(references.Limits()),
        )
        self.assertEqual(
            result["matches"],
            {
                "exact_version": 1,
                "outside_selected_source": 1,
                "prefix_scope": 1,
                "unlocated": 1,
            },
        )
        result = core.plan(self.path, source, catalog_path, TARGET)
        self.assertEqual(result["source"]["exact_catalog_matches"], 1)
        self.assertEqual(result["catalog"]["counts"]["resolution_unknown"], 3)
        self.assertFalse(result["migration_ready"])
        self.assertEqual(self.run_copy()["counts"], {"verified": 2})
        self.assertIn(key, {row["source"]["key"] for row in self.rows()})

    def test_neighbor_inventory_is_unknown_in_source_join_and_copy_plan(self):
        key = "公司/%2F/objects/I-neighbor/payload"
        source, catalog_path = inputs(self.root, [{"key": key}])
        catalog_path.unlink()
        family = next(
            item
            for item in reference_cli.FAMILIES
            if item.table == "image_pyramid_outputs"
        )
        ref = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": "saved-location",
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": "http://source:9000",
            "bucket": "source",
            "prefix": "公司/%2F/objects/I/",
            "key_prefix": "公司/%2F/",
            "storage_identity": references.identity("http://source:9000", "source"),
        }
        row = {
            "id": "I",
            "storage_ref": ref,
            "inventory_key": key,
            "object_sha256": SHA,
            "object_size": len(BODY),
            "object_version": "v1",
            "object_state": "verified",
        }

        class Result:
            def mappings(self):
                return self

            def all(self):
                return [row]

        engine = SimpleNamespace(execute=AsyncMock(return_value=Result()))
        spool_path = self.root / "neighbor-spool.jsonl"
        spool = reference_cli.Spool(spool_path, references.Budget(references.Limits()))
        try:
            asyncio.run(
                reference_cli.expand_inventory(
                    engine, family, spool, spool.budget, set()
                )
            )
        finally:
            spool.stream.close()
        record = json.loads(spool_path.read_text())
        self.assertEqual(record["unknown"], "inventory_key_outside_saved_prefix")
        summary = references.write_catalog(
            catalog_path,
            spool_path,
            {"enumeration_complete": True, "families": [family.table]},
            source,
            self.root,
            references.Budget(references.Limits()),
        )
        self.assertEqual(summary["matches"], {"unlocated": 1})
        self.assertEqual(summary["source_objects_without_exact_match"], 1)
        result = core.plan(self.path, source, catalog_path, TARGET)
        self.assertEqual(result["source"]["exact_catalog_matches"], 0)
        self.assertEqual(result["catalog"]["counts"]["resolution_unknown"], 1)
        self.assertFalse(result["catalog"]["resolution_complete"])
        # Unknown business ownership still does not block a verified source pre-copy.
        self.assertEqual(self.run_copy()["counts"], {"verified": 1})
        self.assertFalse(self.run_copy()["migration_ready"])

    def test_shared_bucket_requires_consistent_reviewed_purposes(self):
        selections = dict.fromkeys(PURPOSES, "migrate")
        selections[next(iter(PURPOSES))] = "retain"
        result = self.plan(handling=selections)
        self.assertEqual(result["decisions"], {"handling_ambiguous": 1})
        self.assertEqual(self.run_copy()["io"]["upload_bytes"], 0)

    def test_handling_override_is_fixed_and_retained_buckets_are_not_uploaded(self):
        paths = inputs(self.root)
        result = core.plan(
            self.path, *paths, TARGET, handling=dict.fromkeys(PURPOSES, "retain")
        )
        self.assertEqual(result["decisions"], {"retain": 1})
        self.assertEqual(self.run_copy()["io"]["upload_bytes"], 0)

    def test_delete_marker_unverified_oversized_and_multipart_stay_explicit(self):
        result = self.plan(
            [
                {"type": "delete_marker"},
                {"key": "unverified", "verification": {"status": "unverified"}},
                {"key": "large", "size_bytes": 999},
                {"type": "multipart", "key": "unfinished", "upload_id": "u"},
            ],
            limits=core.Limits(max_object_bytes=3),
        )
        self.assertEqual(
            result["decisions"],
            {"delete_marker": 1, "source_unverified": 1, "object_byte_budget": 1},
        )
        self.assertEqual(result["facts"], {"multipart": 1})
        self.assertEqual(result["source"]["objects"], 2)
        self.assertEqual(result["source"]["unverified_objects"], 1)
        self.assertEqual(result["source"]["over_object_limit"], 1)
        self.assertEqual(result["source"]["delete_markers"], 1)

    def test_mutable_null_and_unpinned_are_preserved_and_conditionally_read(self):
        self.plan([{"version_id": "null"}, {"version_id": None}])
        self.assertEqual(self.run_copy()["counts"], {"verified": 2})
        self.assertTrue(any(c.get("VersionId") == "null" for c in self.source.calls))
        self.assertTrue(any("VersionId" not in c for c in self.source.calls))
        self.assertTrue(all(c["IfMatch"] == '"opaque"' for c in self.source.calls))

    def test_target_same_physical_source_bucket_is_refused(self):
        paths = inputs(self.root)
        with self.assertRaisesRegex(core.CopyError, "target_is_source"):
            core.plan(
                self.path,
                *paths,
                {**TARGET, "endpoint_url": "http://SOURCE:9000/", "bucket": "source"},
            )

    def test_verified_is_unique_and_resume_does_not_upload_again(self):
        self.plan()
        self.run_copy()
        result = self.run_copy()
        self.assertEqual(result["counts"], {"verified": 1})
        self.assertEqual(self.target.puts, 1)
        row = self.rows()[0]
        self.assertEqual(row["receipt"]["version_id"], "actual-target-1")
        self.assertEqual(row["content"]["Metadata"], {"source": "actual value"})
        self.assertNotIn(
            TARGET["secret_access_key"], self.path.read_bytes().decode(errors="ignore")
        )

    def test_lost_put_ack_is_claimed_without_second_upload(self):
        self.plan()
        self.target.lose_ack = True
        self.assertEqual(self.run_copy()["counts"], {"uncertain": 1})
        self.target.lose_ack = False
        self.assertEqual(self.run_copy()["counts"], {"verified": 1})
        self.assertEqual(self.target.puts, 1)

    def test_hard_exit_after_put_resumes_saved_nonce_and_closes_stream(self):
        self.plan()

        def kill(name, row):
            if name == "after_put":
                raise SystemExit("hard stop")

        with self.assertRaises(SystemExit):
            self.run_copy(event=kill)
        self.assertEqual(self.rows()[0]["state"], "writing")
        self.assertEqual(self.run_copy()["counts"], {"verified": 1})
        self.assertEqual(self.target.puts, 1)
        self.assertTrue(
            all(b.was_closed for b in self.source.bodies + self.target.bodies)
        )

    def test_pause_before_put_has_durable_intent_and_no_object(self):
        self.plan()

        def pause(name, row):
            if name == "before_put":
                raise core.Paused("signal_pause")

        self.assertEqual(self.run_copy(event=pause)["paused"], "signal_pause")
        self.assertEqual(self.rows()[0]["state"], "uncertain")
        self.assertEqual(self.target.puts, 0)
        self.assertEqual(self.run_copy()["counts"], {"verified": 1})

    def test_failed_source_retries_only_with_explicit_flag(self):
        self.plan()
        self.source.fail_get = True
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.source.fail_get = False
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertEqual(self.run_copy(retry_failed=True)["counts"], {"verified": 1})

    def test_unknown_existing_target_is_never_overwritten_or_adopted(self):
        self.plan()
        key = self.rows()[0]["target_key"]
        self.target.objects[key] = [
            {"body": b"abc", "metadata": {}, "version": "foreign"}
        ]
        self.run_copy()
        self.run_copy()
        self.assertEqual(len(self.target.objects[key]), 1)
        self.assertNotEqual(self.rows()[0]["state"], "verified")

    def test_acknowledged_version_is_verified_even_if_other_version_became_latest(self):
        self.plan()
        self.run_copy()
        row = self.rows()[0]
        self.target.objects[row["target_key"]].append(
            {"body": b"bad", "metadata": {}, "version": "foreign-latest"}
        )
        self.assertEqual(self.run_copy(verify_only=True)["counts"], {"verified": 1})

    def test_verify_is_read_only_and_failure_retains_original_receipt(self):
        self.plan()
        self.run_copy()
        receipt = self.rows()[0]["receipt"]
        self.target.objects[self.rows()[0]["target_key"]][0]["body"] = b"bad"
        self.assertEqual(self.run_copy(verify_only=True)["counts"], {"failed": 1})
        self.assertEqual(self.rows()[0]["receipt"], receipt)
        self.assertEqual(self.target.puts, 1)
        self.assertTrue(all(b.was_closed for b in self.target.bodies))

    def test_stream_close_failure_never_publishes_success(self):
        self.plan()
        self.source.fail_close = True
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertTrue(all(b.was_closed for b in self.source.bodies))
        self.assertEqual(self.target.puts, 0)

    def test_byte_and_object_budgets_pause_without_losing_pending(self):
        self.plan([{}, {"key": "second"}])
        result = self.run_copy(limits=core.Limits(max_objects=1))
        self.assertEqual(result["paused"], "objects_budget")
        self.assertEqual(result["counts"], {"verified": 1, "pending": 1})
        result = self.run_copy(limits=core.Limits(max_source_bytes=1))
        self.assertEqual(result["paused"], "source_bytes_budget")
        self.assertEqual(self.run_copy()["counts"], {"verified": 2})

    def test_disk_write_and_close_cleanup_owned_staging(self):
        self.plan()
        with patch.object(core, "source_read", side_effect=OSError("disk full")):
            self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertFalse(list(self.root.glob(".aap-copy-*")))

    def test_checkpoint_private_exclusive_locked_and_corruption_refused(self):
        self.plan()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with core.Checkpoint(self.path):
            with self.assertRaisesRegex(core.CopyError, "checkpoint_busy"):
                core.Checkpoint(self.path)
        with core.Checkpoint(self.path) as state:
            state.db.execute("UPDATE results SET data='{}'")
            state.db.commit()
        with self.assertRaisesRegex(core.CopyError, "checkpoint_corrupt"):
            core.Checkpoint(self.path)

    def test_target_versioning_is_required(self):
        self.plan()
        self.target.versioning = "Suspended"
        with self.assertRaisesRegex(core.CopyError, "target_versioning_required"):
            self.run_copy()

    def test_verification_budget_failure_revokes_success_but_keeps_receipt(self):
        self.plan()
        self.run_copy()
        receipt = self.rows()[0]["receipt"]
        result = self.run_copy(verify_only=True, limits=core.Limits(max_verify_bytes=1))
        self.assertFalse(result["copy_subset_complete"])
        self.assertEqual(self.rows()[0]["state"], "failed")
        self.assertEqual(self.rows()[0]["receipt"], receipt)

    def test_target_metadata_change_revokes_success(self):
        self.plan()
        self.run_copy()
        row = self.rows()[0]
        self.target.objects[row["target_key"]][0]["metadata"]["fixture"] = "changed"
        self.assertEqual(self.run_copy(verify_only=True)["counts"], {"failed": 1})
        self.assertEqual(self.rows()[0]["error"], "target_creation_conflict")

    def test_source_changed_between_heads_never_uploads(self):
        self.plan()
        original = self.source.head_object
        calls = 0

        def head(**kwargs):
            nonlocal calls
            calls += 1
            result = original(**kwargs)
            if calls == 2:
                result["Metadata"] = {"changed": "source"}
            return result

        self.source.head_object = head
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertEqual(self.target.puts, 0)
        self.assertTrue(all(body.was_closed for body in self.source.bodies))

    def test_source_body_digest_mismatch_closes_and_never_uploads(self):
        self.plan()
        original = self.source.get_object

        def get(**kwargs):
            result = original(**kwargs)
            result["Body"].write(b"bad")
            result["Body"].seek(0)
            return result

        self.source.get_object = get
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertEqual(self.rows()[0]["error"], "body_digest_mismatch")
        self.assertEqual(self.target.puts, 0)
        self.assertTrue(all(body.was_closed for body in self.source.bodies))

    def test_delete_marker_target_history_never_becomes_a_new_version(self):
        self.plan()
        key = self.rows()[0]["target_key"]
        self.target.list_object_versions = lambda **kw: {
            "DeleteMarkers": [{"Key": key, "VersionId": "foreign-marker"}]
        }
        self.assertEqual(self.run_copy()["counts"], {"failed": 1})
        self.assertEqual(self.target.puts, 0)
        self.assertEqual(self.rows()[0]["error"], "target_history_conflict")

    def test_crash_before_verified_commit_uses_saved_acknowledged_version(self):
        self.plan()

        def kill(name, row):
            if name == "before_verified_commit":
                raise SystemExit("hard stop")

        with self.assertRaises(SystemExit):
            self.run_copy(event=kill)
        row = self.rows()[0]
        self.assertEqual(row["state"], "writing")
        self.target.objects[row["target_key"]].append(
            {"body": b"bad", "metadata": {}, "version": "foreign-latest"}
        )
        self.assertEqual(self.run_copy()["counts"], {"verified": 1})
        self.assertEqual(self.rows()[0]["receipt"]["version_id"], "actual-target-1")
        self.assertEqual(self.target.puts, 1)

    def test_catalog_summary_cannot_export_arbitrary_private_fields(self):
        paths = inputs(self.root)
        header = json.loads(paths[1].read_text().splitlines()[0])
        paths[1].unlink()
        writer = Writer(paths[1], 65536)
        try:
            writer.emit(header)
            writer.finish(
                {
                    "complete": False,
                    "counts": {"private-key": "private-secret"},
                    "migration_ready": False,
                }
            )
        finally:
            writer.stream.close()
        with self.assertRaisesRegex(core.CopyError, "invalid_catalog_summary"):
            core.plan(self.path, *paths, TARGET)

    def test_catalog_record_kinds_never_become_unbounded_terminal_labels(self):
        paths = inputs(self.root)
        header = json.loads(paths[1].read_text().splitlines()[0])
        paths[1].unlink()
        writer = Writer(paths[1], 65536)
        try:
            writer.emit(header)
            writer.emit({"type": "private-secret"})
            writer.finish({"complete": False, "migration_ready": False})
        finally:
            writer.stream.close()
        with self.assertRaisesRegex(core.CopyError, "invalid_catalog_record"):
            core.plan(self.path, *paths, TARGET)

    def test_finite_positive_budgets(self):
        for values in (
            {"max_objects": True},
            {"max_seconds": float("inf")},
            {"max_source_bytes": 0},
            {"read_timeout_seconds": float("nan")},
        ):
            with self.subTest(values=values), self.assertRaises(core.CopyError):
                core.Limits(**values)

    def test_private_secret_config_never_accepts_symlinks_or_duplicates(self):
        path = self.root / "target.json"
        path.write_text(json.dumps(TARGET))
        path.chmod(0o600)
        self.assertEqual(core.target_config(path), TARGET)
        path.write_text('{"bucket":"a","bucket":"b"}')
        with self.assertRaises(core.CopyError):
            core.target_config(path)
        path.unlink()
        path.symlink_to(self.path)
        with self.assertRaises(OSError):
            core.target_config(path)


if __name__ == "__main__":
    unittest.main()


class ApplicationSeamTests(unittest.TestCase):
    setUp = CopyTests.setUp
    plan = CopyTests.plan
    rows = CopyTests.rows

    def test_durable_callback_precedes_intent_and_ack_precedes_readback(self):
        self.plan()
        saved = []
        original_put, original_head = self.target.put_object, self.target.head_object

        def persist(row, checksum):
            self.assertEqual(checksum, core.digest(row))
            saved.append(json.loads(core.packed(row)))

        def put(**kwargs):
            self.assertEqual(saved[-1]["state"], "writing")
            self.assertTrue(saved[-1]["nonce"])
            return original_put(**kwargs)

        def head(**kwargs):
            self.assertEqual(saved[-1]["receipt"]["version_id"], kwargs["VersionId"])
            return original_head(**kwargs)

        self.target.put_object, self.target.head_object = put, head
        with core.Checkpoint(self.path, on_save=persist) as state:
            result = core.execute(
                state,
                self.source,
                self.target,
                anonymous_staging=True,
                strict_closure=True,
            )
        self.assertTrue(result["copy_subset_complete"])
        self.assertFalse(list(self.root.glob("**/body.part")))

    def test_refused_durable_intent_stops_before_any_put(self):
        self.plan()

        def refuse(*args):
            raise core.CopyStopped("copy_checkpoint_commit_failed")

        with core.Checkpoint(self.path, on_save=refuse) as state:
            with self.assertRaises(core.CopyStopped):
                core.execute(state, self.source, self.target, anonymous_staging=True)
        self.assertEqual(self.target.puts, 0)

    def test_pg_ack_survives_later_local_sqlite_write_failure(self):
        self.plan()
        saved = []
        with core.Checkpoint(self.path) as state:

            def persist(row, checksum):
                saved.append(json.loads(core.packed(row)))
                if row.get("receipt"):
                    state.db.execute("PRAGMA query_only=ON")

            state.on_save = persist
            with self.assertRaises(core.CopyStopped) as caught:
                core.execute(state, self.source, self.target, anonymous_staging=True)
            self.assertEqual(caught.exception.code, "copy_local_checkpoint_failed")
        self.assertTrue(saved[-1]["receipt"]["put_acknowledged"])
        self.assertEqual(self.target.puts, 1)

    def test_strict_unknown_body_close_aborts_later_objects(self):
        self.plan([{}, {"version_id": "v2"}])
        self.source.fail_close = True
        with core.Checkpoint(self.path) as state:
            with self.assertRaises(core.CopyStopped) as caught:
                core.execute(state, self.source, self.target, strict_closure=True)
        self.assertTrue(caught.exception.storage_operation_unsettled)
        self.assertEqual(self.target.puts, 0)
        self.assertTrue(all(body.was_closed for body in self.source.bodies))

    def test_application_stop_bypasses_recoverable_per_object_catches(self):
        self.plan([{}, {"version_id": "v2"}])

        def stop():
            raise core.CopyStopped("copy_cancelled")

        with core.Checkpoint(self.path) as state:
            with self.assertRaises(core.CopyStopped):
                core.execute(state, self.source, self.target, stopped=stop)
        self.assertEqual(self.target.puts, 0)

    def test_selected_retry_two_failed_objects_only_retries_one(self):
        self.plan([{}, {"version_id": "v2"}])
        with core.Checkpoint(self.path) as state:
            for row in state.rows():
                row.update(state="failed", error="source_denied")
                state.save(row)
            selected = [next(state.rows())["id"]]
            result = core.execute(
                state,
                self.source,
                self.target,
                retry_failed=True,
                retry_object_ids=selected,
            )
        self.assertEqual(result["counts"], {"verified": 1, "failed": 1})
        self.assertTrue(result["attempt_subset_complete"])
        self.assertFalse(result["copy_subset_complete"])
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(self.target.puts, 1)

    def test_application_plan_row_and_sqlite_page_limits(self):
        source, catalog_path = inputs(self.root, [{}, {"version_id": "v2"}])
        with self.assertRaises(core.CopyError) as caught:
            core.plan(self.path, source, catalog_path, TARGET, max_rows=1)
        self.assertEqual(caught.exception.code, "plan_row_budget")
