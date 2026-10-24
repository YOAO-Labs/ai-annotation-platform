"""Offline bundle invariants with an explicit fake S3 and no infrastructure."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch

import storage_backup_bundle as bundle
from storage_backup_bundle import (
    BackupError,
    Limits,
    create_generation,
    restore_objects,
    verify_generation,
)
from worktree_env import BUCKETS


class TracedBody(io.BytesIO):
    def __init__(self, data, reads, on_close=None):
        super().__init__(data)
        self.reads = reads
        self.on_close = on_close

    def read(self, size=-1):
        if not 0 < size <= 1024**2:
            raise AssertionError("Object copies must use bounded chunks")
        self.reads.append(size)
        return super().read(size)

    def close(self):
        if self.on_close:
            callback, self.on_close = self.on_close, None
            callback()
        super().close()


class FakeS3:
    def __init__(self, buckets):
        self.objects = {bucket: {} for bucket in buckets}
        self.owner_tags = {bucket: "unchanged-owner" for bucket in buckets}
        self.versioning = {}
        self.history = {}
        self.delete_markers = {}
        self.uploads = {}
        self.calls = []
        self.reads = []
        self.puts = []
        self.on_get = None
        self.on_close = None
        self.put_corruption = False
        self.duplicate_listing = False

    def add(self, bucket, key, data, **metadata):
        self.objects[bucket][key] = {
            "data": data,
            "Metadata": {},
            "ContentType": "application/octet-stream",
            **deepcopy(metadata),
        }

    def _head(self, bucket, key):
        item = self.objects[bucket][key]
        return {
            **{name: deepcopy(value) for name, value in item.items() if name != "data"},
            "ContentLength": len(item["data"]),
            "ETag": '"' + hashlib.md5(item["data"]).hexdigest() + '"',
        }

    def get_bucket_versioning(self, *, Bucket):
        self.calls.append(("versioning", Bucket))
        return {"Status": self.versioning[Bucket]} if Bucket in self.versioning else {}

    def list_object_versions(self, *, Bucket, KeyMarker=None, VersionIdMarker=None):
        self.calls.append(("versions", Bucket))
        values = [
            {"Key": key, "VersionId": "null", "IsLatest": True}
            for key in sorted(self.objects[Bucket])
        ] + self.history.get(Bucket, [])
        offset = int(KeyMarker or "0")
        selected = values[offset : offset + 2]
        more = offset + len(selected) < len(values)
        return {
            "Versions": selected,
            "DeleteMarkers": self.delete_markers.get(Bucket, []),
            "IsTruncated": more,
            **(
                {"NextKeyMarker": str(offset + 2), "NextVersionIdMarker": "null"}
                if more
                else {}
            ),
        }

    def list_multipart_uploads(self, *, Bucket):
        self.calls.append(("multipart", Bucket))
        return {"Uploads": self.uploads.get(Bucket, []), "IsTruncated": False}

    def list_objects_v2(self, *, Bucket, ContinuationToken=None):
        self.calls.append(("list", Bucket))
        keys = sorted(self.objects[Bucket])
        offset = int(ContinuationToken or "0")
        selected = keys[offset : offset + 2]
        if self.duplicate_listing and offset and keys:
            selected[0] = keys[0]
        more = offset + len(selected) < len(keys)
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": self._head(Bucket, key)["ContentLength"],
                    "ETag": self._head(Bucket, key)["ETag"],
                }
                for key in selected
            ],
            "IsTruncated": more,
            **({"NextContinuationToken": str(offset + 2)} if more else {}),
        }

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket))
        return self._head(Bucket, Key)

    def get_object(self, *, Bucket, Key, IfMatch):
        self.calls.append(("get", Bucket))
        head = self._head(Bucket, Key)
        if IfMatch != head["ETag"]:
            raise RuntimeError("credential-material-must-not-escape")
        data = self.objects[Bucket][Key]["data"]
        if self.on_get:
            callback, self.on_get = self.on_get, None
            callback()
        on_close, self.on_close = self.on_close, None
        return {**head, "Body": TracedBody(data, self.reads, on_close)}

    def put_object(
        self, *, Bucket, Key, ContentLength, IfNoneMatch, Body=None, **metadata
    ):
        if IfNoneMatch != "*" or Key in self.objects[Bucket]:
            raise RuntimeError("Restore must never overwrite objects")
        Body = io.BytesIO(b"") if Body is None else Body
        chunks = []
        while chunk := Body.read(1024**2):
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != ContentLength:
            raise AssertionError("PUT length must be explicit")
        self.puts.append((Bucket, Key))
        if self.put_corruption:
            data = bytes([data[0] ^ 1]) + data[1:] if data else b"corrupt"
        self.add(Bucket, Key, data, **metadata)
        return {"ETag": self._head(Bucket, Key)["ETag"]}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-backup-bundle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "generation"
        self.dump = self.root / "input.dump"
        self.dump.write_bytes(b"PGDMP\x01synthetic custom dump")
        self.dump.chmod(0o600)
        self.source = {role: "source-" + suffix for role, suffix in BUCKETS.items()}
        self.source["MINIO_DATASETS_BUCKET"] = self.source["MINIO_BUCKET"]
        self.target = {
            role: value.replace("source-", "target-", 1)
            for role, value in self.source.items()
        }
        self.s3 = FakeS3(set(self.source.values()) | set(self.target.values()))
        self.shared = self.source["MINIO_BUCKET"]
        self.s3.add(
            self.shared,
            "../images/车辆.png",
            b"fixed original",
            ContentType="image/png",
            Metadata={
                "sha256": "user-metadata-is-not-content-verification",
                "format": "original",
            },
            CacheControl="private, max-age=123",
            ContentDisposition='inline; filename="vehicle.png"',
            ContentEncoding="identity",
            ContentLanguage="zh-CN",
            Expires=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        self.s3.add(self.shared, "large.bin", b"x" * (1024**2 + 13))
        self.s3.add(self.shared, "zero/", b"")
        self.facts = {"head": "test-commit", "frozen": {"manifest_digest": "a" * 64}}

    def create(self, **kwargs):
        return create_generation(
            self.directory,
            s3=self.s3,
            bucket_roles=self.source,
            dump_path=self.dump,
            facts=self.facts,
            **kwargs,
        )

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(BackupError) as error:
            function(*args, **kwargs)
        self.assertEqual(error.exception.code, code)
        self.assertNotIn("credential-material", str(error.exception))
        return error.exception

    def rewrite_manifest(self, change):
        path = self.directory / "manifest.json"
        manifest = json.loads(path.read_bytes())
        change(manifest)
        raw = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        path.write_bytes(raw)
        (self.directory / "COMPLETE.json").write_text(
            json.dumps(
                {"schema": 1, "manifest_sha256": hashlib.sha256(raw).hexdigest()}
            )
        )

    def test_complete_generation_streams_sha_preserves_metadata_and_shared_roles(self):
        manifest = self.create()
        self.assertEqual(manifest, verify_generation(self.directory))
        self.assertEqual(len(manifest["bucket_roles"]), 9)
        self.assertEqual(len(set(manifest["bucket_roles"].values())), 8)
        self.assertEqual(manifest["totals"]["objects"], 3)
        self.assertEqual(
            manifest["totals"]["object_bytes"],
            sum(len(item["data"]) for item in self.s3.objects[self.shared].values()),
        )
        for member in manifest["objects"]:
            self.assertEqual(
                member["sha256"],
                hashlib.sha256(
                    self.s3.objects[self.shared][member["key"]]["data"]
                ).hexdigest(),
            )
            self.assertNotEqual(member["sha256"], member["etag"])
            self.assertRegex(member["file"], r"^objects/[0-9a-f]{64}\.bin$")
        self.assertTrue(self.s3.reads)
        self.assertTrue(all(size == 1024**2 for size in self.s3.reads))
        calls = len(self.s3.calls)
        self.assertEqual(manifest, verify_generation(self.directory))
        self.assertEqual(
            calls, len(self.s3.calls), "verification must have zero external I/O"
        )
        self.facts["head"] = "changed later"
        self.assertEqual(manifest["facts"]["head"], "test-commit")
        for path in self.directory.rglob("*"):
            self.assertEqual(
                path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600
            )

    def test_restore_empty_object_omits_stream_before_next_conditional_put(self):
        self.s3.add(self.shared, "zz-after-empty.bin", b"retained after empty")
        original_put = self.s3.put_object

        def compatible_put(**kwargs):
            # Botocore wraps even b'' into a stream and adds Expect: 100-continue.
            # Empty PUTs must omit Body to keep the next pooled request usable.
            if kwargs["ContentLength"] == 0 and "Body" in kwargs:
                raise OSError("Empty Expect/continue transport is unsupported")
            return original_put(**kwargs)

        self.s3.put_object = compatible_put
        self.create()
        result = restore_objects(self.directory, s3=self.s3, bucket_roles=self.target)
        self.assertEqual(result["objects"], 4)
        destination = self.s3.objects[self.target["MINIO_BUCKET"]]
        self.assertEqual(destination["zero/"]["data"], b"")
        self.assertEqual(
            destination["zz-after-empty.bin"]["data"], b"retained after empty"
        )

    def test_restore_checks_all_targets_and_verifies_bytes_and_metadata(self):
        manifest = self.create()
        owners = deepcopy(self.s3.owner_tags)
        result = restore_objects(self.directory, s3=self.s3, bucket_roles=self.target)
        self.assertEqual(result["objects"], 3)
        self.assertEqual(result["buckets"], 8)
        self.assertEqual(result["object_bytes"], manifest["totals"]["object_bytes"])
        self.assertEqual(self.s3.owner_tags, owners)
        for key, original in self.s3.objects[self.shared].items():
            self.assertEqual(
                self.s3.objects[self.target["MINIO_BUCKET"]][key], original
            )
        self.assertEqual(len(self.s3.puts), 3)
        self.assert_error(
            "target_not_empty",
            restore_objects,
            self.directory,
            s3=self.s3,
            bucket_roles=self.target,
        )
        self.assertEqual(len(self.s3.puts), 3)

    def test_late_nonempty_target_prevents_every_put(self):
        self.create()
        self.s3.add(sorted(set(self.target.values()))[-1], "foreign", b"retain")
        self.assert_error(
            "target_not_empty",
            restore_objects,
            self.directory,
            s3=self.s3,
            bucket_roles=self.target,
        )
        self.assertEqual(self.s3.puts, [])

    def test_role_mapping_must_be_complete_and_preserve_shared_topology(self):
        self.create()
        mappings = []
        missing = dict(self.target)
        missing.pop("MINIO_TRAINING_BUCKET")
        mappings.append((missing, "invalid_roles"))
        split = dict(self.target, MINIO_DATASETS_BUCKET="new-separate-bucket")
        mappings.append((split, "mapping_mismatch"))
        merged = dict(
            self.target, MINIO_TRAINING_BUCKET=self.target["MINIO_AVATARS_BUCKET"]
        )
        mappings.append((merged, "mapping_mismatch"))
        mappings.append((self.source, "mapping_mismatch"))
        for mapping, code in mappings:
            with self.subTest(code=code):
                before = len(self.s3.calls)
                self.assert_error(
                    code,
                    restore_objects,
                    self.directory,
                    s3=self.s3,
                    bucket_roles=mapping,
                )
                self.assertEqual(len(self.s3.calls), before)
                self.assertEqual(self.s3.puts, [])

    def test_missing_corrupt_and_unknown_completion_markers_block_restore(self):
        self.create()
        marker = self.directory / "COMPLETE.json"
        original = marker.read_bytes()
        for bad in (
            None,
            b"{",
            b'{"schema":2,"manifest_sha256":"x"}',
            b'{"schema":1,"manifest_sha256":"x"}',
        ):
            with self.subTest(bad=bad):
                if bad is None:
                    marker.unlink()
                else:
                    marker.write_bytes(bad)
                before = len(self.s3.calls)
                with self.assertRaises(BackupError):
                    restore_objects(
                        self.directory, s3=self.s3, bucket_roles=self.target
                    )
                self.assertEqual(len(self.s3.calls), before)
                self.assertEqual(self.s3.puts, [])
                marker.write_bytes(original)

    def test_corrupt_dump_and_object_are_detected_before_target_io(self):
        manifest = self.create()
        paths = [
            self.directory / manifest["database"]["file"],
            self.directory / manifest["objects"][0]["file"],
        ]
        for path in paths:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                before = len(self.s3.calls)
                with self.assertRaises(BackupError):
                    restore_objects(
                        self.directory, s3=self.s3, bucket_roles=self.target
                    )
                self.assertEqual(len(self.s3.calls), before)
                self.assertEqual(self.s3.puts, [])
                path.write_bytes(original)
        missing = self.directory / manifest["objects"][0]["file"]
        missing.unlink()
        self.assert_error(
            "object_integrity",
            restore_objects,
            self.directory,
            s3=self.s3,
            bucket_roles=self.target,
        )
        self.assertEqual(self.s3.puts, [])

    def test_malformed_manifest_cannot_redirect_files_or_duplicate_objects(self):
        self.create()
        original = (self.directory / "manifest.json").read_bytes()
        changes = [
            lambda value: value["objects"][0].update(file="../../outside"),
            lambda value: value["objects"][0].update(bucket=[]),
            lambda value: value["objects"].append(deepcopy(value["objects"][0])),
            lambda value: value["database"].update(file="../foreign.dump"),
            lambda value: value["objects"][0]["metadata"].update(
                Metadata={"bad\nkey": "unsafe"}
            ),
            lambda value: value["objects"][0].update(size_bytes=True),
            lambda value: value.update(totals={}),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.rewrite_manifest(change)
                before = len(self.s3.calls)
                with self.assertRaises(BackupError):
                    restore_objects(
                        self.directory, s3=self.s3, bucket_roles=self.target
                    )
                self.assertEqual(len(self.s3.calls), before)
                self.assertEqual(self.s3.puts, [])
                (self.directory / "manifest.json").write_bytes(original)

    def test_all_four_budgets_are_checked_before_a_successful_generation(self):
        for index, limits in enumerate(
            (
                Limits(max_objects=2),
                Limits(max_total_bytes=10),
                Limits(max_object_bytes=10),
                Limits(max_dump_bytes=4),
            )
        ):
            with self.subTest(limits=limits):
                self.directory = self.root / f"limited-{index}"
                self.assert_error("budget_exceeded", self.create, limits=limits)
                self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.assertEqual(self.s3.puts, [])
        self.assert_error("invalid_limits", Limits, max_objects=True)
        self.assert_error("invalid_limits", Limits, max_dump_bytes=0)

    def test_lower_restore_budget_rejects_before_any_storage_call(self):
        self.create()
        before = len(self.s3.calls)
        self.assert_error(
            "budget_exceeded",
            restore_objects,
            self.directory,
            s3=self.s3,
            bucket_roles=self.target,
            limits=Limits(max_objects=1),
        )
        self.assertEqual(len(self.s3.calls), before)
        self.assertEqual(self.s3.puts, [])

    def test_versioned_history_deleted_and_multipart_sources_are_rejected(self):
        variants = ("enabled", "suspended", "history", "deleted", "multipart")
        for index, variant in enumerate(variants):
            with self.subTest(variant=variant):
                self.directory = self.root / f"unsupported-{index}"
                if variant in {"enabled", "suspended"}:
                    self.s3.versioning[self.shared] = variant.title()
                elif variant == "history":
                    self.s3.history[self.shared] = [
                        {"Key": "old", "VersionId": "v1", "IsLatest": False}
                    ]
                elif variant == "deleted":
                    self.s3.delete_markers[self.shared] = [
                        {"Key": "gone", "VersionId": "v1"}
                    ]
                else:
                    self.s3.uploads[self.shared] = [
                        {"Key": "unfinished", "UploadId": "upload"}
                    ]
                self.assert_error("unsupported_bucket_state", self.create)
                self.assertFalse((self.directory / "COMPLETE.json").exists())
                self.s3.versioning.clear()
                self.s3.history.clear()
                self.s3.delete_markers.clear()
                self.s3.uploads.clear()

    def test_unsupported_target_state_is_rejected_before_every_put(self):
        self.create()
        last = sorted(set(self.target.values()))[-1]
        for variant in ("versioning", "multipart", "history"):
            with self.subTest(variant=variant):
                if variant == "versioning":
                    self.s3.versioning[last] = "Suspended"
                elif variant == "multipart":
                    self.s3.uploads[last] = [{"Key": "pending", "UploadId": "x"}]
                else:
                    self.s3.history[last] = [
                        {"Key": "old", "VersionId": "v1", "IsLatest": False}
                    ]
                self.assert_error(
                    "unsupported_bucket_state",
                    restore_objects,
                    self.directory,
                    s3=self.s3,
                    bucket_roles=self.target,
                )
                self.assertEqual(self.s3.puts, [])
                self.s3.versioning.clear()
                self.s3.uploads.clear()
                self.s3.history.clear()

    def test_source_changes_and_duplicate_pagination_cannot_publish_completion(self):
        self.s3.on_get = lambda: self.s3.add(self.shared, "new-late", b"late")
        self.assert_error("source_changed", self.create)
        self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.directory = self.root / "metadata-change"
        self.s3.on_close = lambda: self.s3.objects[self.shared][
            "../images/车辆.png"
        ].update(CacheControl="changed")
        self.assert_error("source_changed", self.create)
        self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.directory = self.root / "duplicated-pages"
        self.s3.duplicate_listing = True
        self.assert_error("duplicate_object", self.create)
        self.assertFalse((self.directory / "COMPLETE.json").exists())

    def test_symlinks_permissions_and_parent_traversal_are_rejected(self):
        manifest = self.create()
        alias = self.root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        self.assert_error("unsafe_path", verify_generation, alias)
        self.assert_error(
            "unsafe_path",
            verify_generation,
            self.root / "not-there" / ".." / "generation",
        )
        object_path = self.directory / manifest["objects"][0]["file"]
        old = object_path.read_bytes()
        object_path.unlink()
        object_path.symlink_to(self.dump)
        self.assert_error("unsafe_path", verify_generation, self.directory)
        object_path.unlink()
        object_path.write_bytes(old)
        object_path.chmod(0o600)
        marker = self.directory / "COMPLETE.json"
        marker.chmod(0o644)
        self.assert_error("unsafe_path", verify_generation, self.directory)
        marker.chmod(0o600)
        self.directory.chmod(0o755)
        self.assert_error("unsafe_path", verify_generation, self.directory)

    def test_dump_symlink_invalid_magic_and_existing_generation_are_rejected(self):
        self.create()
        calls = len(self.s3.calls)
        self.assert_error("generation_not_empty", self.create)
        self.assertEqual(len(self.s3.calls), calls)
        self.directory = self.root / "bad-input"
        self.dump.write_bytes(b"not a custom dump")
        self.assert_error("dump_integrity", self.create)
        self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.directory = self.root / "symlink-input"
        linked = self.root / "linked.dump"
        linked.symlink_to(self.dump)
        self.dump = linked
        self.assert_error("unsafe_path", self.create)

    def test_nonregular_object_file_is_rejected_without_opening_a_blocking_stream(self):
        manifest = self.create()
        path = self.directory / manifest["objects"][0]["file"]
        path.unlink()
        os.mkfifo(path, 0o600)
        self.assert_error("unsafe_path", verify_generation, self.directory)

    def test_storage_failures_do_not_disclose_underlying_credentials(self):
        with patch.object(
            self.s3,
            "get_object",
            side_effect=RuntimeError("credential-material-must-not-escape"),
        ):
            error = self.assert_error("storage_io", self.create)
        self.assertNotIn(
            "credential-material", "".join(traceback.format_exception(error))
        )
        self.assertFalse((self.directory / "COMPLETE.json").exists())

    def test_failed_completion_publish_never_becomes_a_valid_generation(self):
        replace = os.replace

        def interrupted(source, destination, **kwargs):
            if destination == "COMPLETE.json":
                raise OSError("credential-material-must-not-escape")
            return replace(source, destination, **kwargs)

        with patch.object(bundle.os, "replace", side_effect=interrupted):
            self.assert_error("filesystem_io", self.create)
        self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.assert_error(
            "incomplete_generation",
            restore_objects,
            self.directory,
            s3=self.s3,
            bucket_roles=self.target,
        )
        self.assertEqual(self.s3.puts, [])

    def test_final_database_proof_runs_after_manifest_and_can_prevent_completion(self):
        observed = []

        def changed_database():
            manifest = json.loads((self.directory / "manifest.json").read_bytes())
            observed.append(manifest["totals"]["objects"])
            self.assertFalse((self.directory / "COMPLETE.json").exists())
            raise BackupError("database_changed", "The offline database changed.")

        self.assert_error(
            "database_changed", self.create, before_publish=changed_database
        )
        self.assertEqual(observed, [3])
        self.assertFalse((self.directory / "COMPLETE.json").exists())
        self.assert_error("incomplete_generation", verify_generation, self.directory)

    def test_restore_readback_corruption_is_a_failure_without_bucket_adoption(self):
        self.create()
        owners = deepcopy(self.s3.owner_tags)
        self.s3.put_corruption = True
        with self.assertRaises(BackupError):
            restore_objects(self.directory, s3=self.s3, bucket_roles=self.target)
        self.assertTrue(self.s3.puts)
        self.assertEqual(self.s3.owner_tags, owners)

    def test_empty_generation_retains_every_role_and_restores_zero_objects(self):
        self.s3.objects[self.shared].clear()
        manifest = self.create(
            limits=Limits(max_objects=0, max_total_bytes=0, max_object_bytes=0)
        )
        self.assertEqual(manifest["totals"]["objects"], 0)
        self.assertEqual(len(manifest["bucket_roles"]), 9)
        result = restore_objects(self.directory, s3=self.s3, bucket_roles=self.target)
        self.assertEqual(result["objects"], 0)
        self.assertEqual(self.s3.puts, [])


if __name__ == "__main__":
    unittest.main()
