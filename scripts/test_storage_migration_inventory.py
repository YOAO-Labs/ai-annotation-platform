"""Source inventory invariants with a read-only fake client and no infrastructure."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import storage_migration_inventory as inventory


class S3Error(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Body(io.BytesIO):
    def __init__(self, value, *, close_error=False, callback=None):
        super().__init__(value)
        self.close_calls = 0
        self.close_error = close_error
        self.callback = callback

    def read(self, size=-1):
        if not 0 < size <= 1024**2:
            raise AssertionError("Unbounded body read")
        if self.callback:
            self.callback()
        return super().read(size)

    def close(self):
        self.close_calls += 1
        super().close()
        if self.close_error:
            raise OSError("credential must not appear")


class S3:
    def __init__(self):
        self.status = None
        self.objects = []
        self.markers = []
        self.uploads = []
        self.calls = []
        self.bodies = []
        self.lifecycle_error = "NoSuchLifecycleConfiguration"
        self.rules = []
        self.list_hook = None
        self.get_hook = None
        self.head_hook = None
        self.body_hook = None
        self.close_error = False
        self.body_override = None
        self.config_calls = 0
        self.change_config = False

    def add(self, key, data=b"real source bytes", version=None, latest=True):
        item = {
            "Key": key,
            "data": data,
            "VersionId": version,
            "IsLatest": latest,
            "Size": len(data),
            "ETag": '"opaque-not-an-md5"',
            "LastModified": datetime(2026, 10, 25, tzinfo=timezone.utc),
        }
        self.objects.append(item)
        return item

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        self.config_calls += 1
        if self.change_config and self.config_calls > 1:
            return {"Status": "Suspended"}
        return {"Status": self.status} if self.status else {}

    def get_bucket_lifecycle_configuration(self, **kwargs):
        self.calls.append(("get_bucket_lifecycle_configuration", kwargs))
        if self.lifecycle_error:
            raise S3Error(self.lifecycle_error)
        return {"Rules": deepcopy(self.rules)}

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        offset = int(kwargs.get("ContinuationToken", 0))
        limit = kwargs["MaxKeys"]
        values = [
            {
                key: value
                for key, value in item.items()
                if key not in ("data", "VersionId", "IsLatest")
            }
            for item in self.objects
        ]
        response = {
            "Contents": deepcopy(values[offset : offset + limit]),
            "IsTruncated": offset + limit < len(values),
        }
        if response["IsTruncated"]:
            response["NextContinuationToken"] = str(offset + limit)
        return self.list_hook(response) if self.list_hook else response

    def list_object_versions(self, **kwargs):
        self.calls.append(("list_object_versions", kwargs))
        offset = int(kwargs.get("KeyMarker", 0))
        limit = kwargs["MaxKeys"]
        values = [
            ("Versions", {key: value for key, value in item.items() if key != "data"})
            for item in self.objects
        ] + [("DeleteMarkers", item) for item in self.markers]
        response = {
            "Versions": [],
            "DeleteMarkers": [],
            "IsTruncated": offset + limit < len(values),
        }
        for field, item in values[offset : offset + limit]:
            response[field].append(deepcopy(item))
        if response["IsTruncated"]:
            response.update(
                NextKeyMarker=str(offset + limit), NextVersionIdMarker="exact-token"
            )
        return self.list_hook(response) if self.list_hook else response

    def list_multipart_uploads(self, **kwargs):
        self.calls.append(("list_multipart_uploads", kwargs))
        offset = int(kwargs.get("KeyMarker", 0))
        limit = kwargs["MaxUploads"]
        response = {
            "Uploads": deepcopy(self.uploads[offset : offset + limit]),
            "IsTruncated": offset + limit < len(self.uploads),
        }
        if response["IsTruncated"]:
            response.update(
                NextKeyMarker=str(offset + limit), NextUploadIdMarker="exact-upload"
            )
        return response

    def _item(self, kwargs):
        return next(
            item
            for item in self.objects
            if item["Key"] == kwargs["Key"]
            and item["VersionId"] == kwargs.get("VersionId")
        )

    def _headers(self, item):
        result = {
            "ContentLength": item["Size"],
            "ETag": item["ETag"],
            "ContentType": "image/png",
            "Metadata": {"owner": "source"},
            "LastModified": item["LastModified"],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
        if item["VersionId"] is not None:
            result["VersionId"] = item["VersionId"]
        return result

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        item = self._item(kwargs)
        if kwargs["IfMatch"] != item["ETag"]:
            raise S3Error("PreconditionFailed")
        result = self._headers(item)
        return self.head_hook(result) if self.head_hook else result

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        item = self._item(kwargs)
        result = self._headers(item)
        body = Body(
            self.body_override if self.body_override is not None else item["data"],
            close_error=self.close_error,
            callback=self.body_hook,
        )
        self.bodies.append(body)
        result["Body"] = body
        return self.get_hook(result) if self.get_hook else result


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-e07-core-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "inventory.jsonl"
        self.client = S3()
        self.purposes = dict.fromkeys(inventory.PURPOSES, "one-shared-bucket")

    def run_inventory(self, **kwargs):
        return inventory.write_inventory(
            self.path, self.client, self.purposes, **kwargs
        )

    def records(self, kind):
        return [
            row
            for row in map(json.loads, self.path.read_text().splitlines())
            if row["type"] == kind
        ]

    def assert_unverified(self, code):
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertEqual(tail["counts"]["verified_objects"], 0)
        result = self.records("object")[0]["verification"]
        self.assertEqual(result["code"], code)
        self.assertNotIn("sha256", result)
        self.assertTrue(self.client.bodies[0].closed)
        self.assertEqual(self.client.bodies[0].close_calls, 1)

    def test_shared_bucket_nine_purposes_and_private_integrity(self):
        item = self.client.add("中文/原件 %.png")
        tail = self.run_inventory(
            source={
                "label": "chosen",
                "endpoint": "https://user:secret@host:9000/path?X-Amz-Signature=secret",
            },
            handling={"datasets": "migrate"},
            observations={
                "secret_access_key": "must not appear",
                "all_clients": "unknown",
            },
        )
        self.assertTrue(tail["complete"])
        self.assertFalse(tail["migration_ready"])
        self.assertEqual(tail["counts"]["buckets"], 1)
        self.assertEqual(tail["counts"]["objects"], 1)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(inventory.verify_inventory(self.path), tail)
        header = self.records("header")[0]
        self.assertEqual(header["source"]["endpoint"], "https://host:9000/path")
        self.assertEqual(header["handling"]["datasets"], "migrate")
        self.assertEqual(header["handling"]["training"], "unreviewed")
        self.assertEqual(
            self.records("bucket")[0]["purposes"], sorted(inventory.PURPOSES)
        )
        observed = self.records("object")[0]
        self.assertEqual(
            observed["verification"]["sha256"], hashlib.sha256(item["data"]).hexdigest()
        )
        self.assertNotEqual(
            observed["etag"].strip('"'), observed["verification"]["sha256"]
        )
        self.assertNotIn("must not appear", self.path.read_text())
        self.assertNotIn("X-Amz", self.path.read_text())
        self.assertTrue(self.client.bodies[0].closed)
        self.assertEqual(
            {name for name, _ in self.client.calls},
            {
                "get_bucket_versioning",
                "get_bucket_lifecycle_configuration",
                "list_objects_v2",
                "head_object",
                "get_object",
                "list_multipart_uploads",
            },
        )

    def test_exact_nine_purposes_and_handling_validate_before_file_creation(self):
        for purposes in ({"datasets": "a"}, {**self.purposes, "unknown": "b"}):
            with (
                self.subTest(purposes=purposes),
                self.assertRaisesRegex(inventory.InventoryError, "invalid_purpose_map"),
            ):
                inventory.write_inventory(self.path, self.client, purposes)
        with self.assertRaisesRegex(inventory.InventoryError, "invalid_handling"):
            self.run_inventory(handling={"training": "discard"})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.calls, [])

    def test_version_history_null_versions_delete_markers_and_multipart_pagination(
        self,
    ):
        self.client.status = "Suspended"
        self.client.add("same key", b"old", "null", False)
        self.client.add("same key", b"new", "version-new", True)
        self.client.markers = [
            {"Key": "deleted", "VersionId": "marker-id", "IsLatest": True}
        ]
        self.client.uploads = [
            {"Key": f"pending-{i}", "UploadId": f"upload-{i}"} for i in range(3)
        ]
        tail = self.run_inventory(limits=inventory.InventoryLimits(page_size=1))
        self.assertTrue(tail["complete"])
        self.assertEqual(tail["counts"]["objects"], 2)
        self.assertEqual(tail["counts"]["delete_markers"], 1)
        self.assertEqual(tail["counts"]["multipart"], 3)
        self.assertEqual(
            {row["version_id"] for row in self.records("object")},
            {"null", "version-new"},
        )
        self.assertEqual(self.records("delete_marker")[0]["version_id"], "marker-id")
        for name, request in self.client.calls:
            if name in ("get_object", "head_object"):
                self.assertIn(request["VersionId"], ("null", "version-new"))
                self.assertEqual(request["IfMatch"], '"opaque-not-an-md5"')
            if name == "list_object_versions" and "KeyMarker" in request:
                self.assertEqual(request["VersionIdMarker"], "exact-token")
        self.assertFalse(
            any(name == "list_objects_v2" for name, _ in self.client.calls)
        )

    def test_unversioned_multiple_pages(self):
        for number in range(5):
            self.client.add(f"key-{number}")
        tail = self.run_inventory(limits=inventory.InventoryLimits(page_size=2))
        self.assertTrue(tail["complete"])
        self.assertEqual(tail["counts"]["verified_objects"], 5)
        self.assertEqual(
            [
                request.get("ContinuationToken")
                for name, request in self.client.calls
                if name == "list_objects_v2"
            ],
            [None, "2", "4"],
        )

    def test_repeated_pagination_token_is_incomplete(self):
        self.client.add("one")
        self.client.list_hook = lambda _: {
            "Contents": [],
            "IsTruncated": True,
            "NextContinuationToken": "0",
        }
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertIn("pagination_loop", [row["code"] for row in self.records("issue")])
        self.assertEqual(tail["counts"]["pages"], 3)

    def test_duplicate_listing_is_incomplete(self):
        self.client.add("same")
        self.client.add("same")
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertIn("duplicate_entry", [row["code"] for row in self.records("issue")])

    def test_absent_lifecycle_known_empty_but_denied_unknown(self):
        self.client.lifecycle_error = "AccessDenied"
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertEqual(
            self.records("bucket")[0]["lifecycle"],
            {"state": "unknown", "code": "AccessDenied"},
        )
        self.assertEqual(tail["counts"]["objects"], 0)

    def test_bucket_configuration_change_is_incomplete(self):
        self.client.change_config = True
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertIn(
            "configuration_changed", [row["code"] for row in self.records("issue")]
        )

    def test_exact_version_response_mismatch_closes_body(self):
        self.client.status = "Enabled"
        self.client.add("key", version="exact")
        self.client.get_hook = lambda result: {**result, "VersionId": "different"}
        self.assert_unverified("version_changed")

    def test_conditional_get_etag_mismatch_closes_body(self):
        self.client.add("key")
        self.client.get_hook = lambda result: {**result, "ETag": '"changed"'}
        self.assert_unverified("etag_changed")

    def test_precondition_failure_does_not_leak_raw_exception(self):
        self.client.add("key")

        def failed(_):
            raise S3Error("PreconditionFailed")

        self.client.head_hook = failed
        tail = self.run_inventory()
        self.assertFalse(tail["complete"])
        self.assertEqual(
            self.records("object")[0]["verification"]["code"], "PreconditionFailed"
        )
        self.assertEqual(self.client.bodies, [])

    def test_truncated_body_closes_without_digest(self):
        self.client.add("key", b"expected bytes")
        self.client.body_override = b"short"
        self.assert_unverified("body_length_mismatch")

    def test_oversized_body_closes_without_digest(self):
        self.client.add("key", b"x")
        self.client.body_override = b"too many bytes"
        self.assert_unverified("body_length_mismatch")

    def test_partial_response_is_not_full_verification(self):
        self.client.add("key")
        self.client.get_hook = lambda result: {**result, "ContentRange": "bytes 0-3/10"}
        self.assert_unverified("unexpected_partial_or_deleted")

    def test_close_failure_never_publishes_digest(self):
        self.client.add("key")
        self.client.close_error = True
        self.assert_unverified("body_close_failed")
        self.assertNotIn("credential", self.path.read_text())

    def test_metadata_change_after_get_is_incomplete(self):
        self.client.add("key")
        count = 0

        def changed(result):
            nonlocal count
            count += 1
            return {**result, "Metadata": {"owner": "changed"}} if count > 1 else result

        self.client.head_hook = changed
        self.assert_unverified("object_changed")

    def test_get_metadata_must_match_preceding_head(self):
        self.client.add("key")
        self.client.get_hook = lambda result: {
            **result,
            "ContentType": "different/content-type",
        }
        self.assert_unverified("response_metadata_changed")

    def test_redacted_metadata_change_still_invalidates_identity(self):
        self.client.add("key")
        count = 0

        def changed(result):
            nonlocal count
            count += 1
            return {
                **result,
                "Metadata": {"token": "first" if count == 1 else "second"},
            }

        self.client.head_hook = changed
        self.client.get_hook = lambda result: {**result, "Metadata": {"token": "first"}}
        self.assert_unverified("object_changed")
        self.assertNotIn('"first"', self.path.read_text())
        self.assertNotIn('"second"', self.path.read_text())

    def test_unexpected_http_status_closes_without_digest(self):
        self.client.add("key")
        self.client.get_hook = lambda result: {
            **result,
            "ResponseMetadata": {"HTTPStatusCode": 206},
        }
        self.assert_unverified("unexpected_status")

    def test_deadline_includes_streaming_and_still_closes_body(self):
        self.client.add("key")
        self.client.body_hook = lambda: None
        with patch.object(inventory.time, "monotonic", side_effect=[0, 0, 0, 301]):
            self.assert_unverified("read_timeout")

    def test_limits_reject_nonpositive_boolean_and_nonfinite(self):
        for arguments in (
            {"max_pages": 0},
            {"max_entries": True},
            {"max_total_bytes": -1},
            {"read_timeout_seconds": float("nan")},
            {"page_size": 1001},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(inventory.InventoryError, "invalid_limits"),
            ):
                inventory.InventoryLimits(**arguments)

    def test_entry_budget_counts_delete_markers_and_multipart(self):
        self.client.status = "Enabled"
        self.client.markers = [
            {"Key": "deleted", "VersionId": "marker", "IsLatest": True}
        ]
        self.client.uploads = [{"Key": "pending", "UploadId": "pending"}]
        tail = self.run_inventory(limits=inventory.InventoryLimits(max_entries=1))
        self.assertFalse(tail["complete"])
        self.assertEqual(tail["counts"]["entries"], 1)
        self.assertEqual(tail["counts"]["delete_markers"], 1)
        self.assertIn("entry_budget", [row["code"] for row in self.records("issue")])

    def test_page_budget_is_global(self):
        tail = self.run_inventory(limits=inventory.InventoryLimits(max_pages=1))
        self.assertFalse(tail["complete"])
        self.assertEqual(tail["counts"]["pages"], 1)
        self.assertIn("page_budget", [row["code"] for row in self.records("issue")])

    def test_object_budget_skips_get_without_success_digest(self):
        self.client.add("key", b"too large")
        tail = self.run_inventory(limits=inventory.InventoryLimits(max_object_bytes=1))
        self.assertFalse(tail["complete"])
        self.assertEqual(self.client.bodies, [])
        self.assertEqual(
            self.records("object")[0]["verification"]["code"], "object_byte_budget"
        )

    def test_total_budget_does_not_open_next_object(self):
        self.client.add("first", b"abc")
        self.client.add("second", b"abc")
        tail = self.run_inventory(limits=inventory.InventoryLimits(max_total_bytes=3))
        self.assertFalse(tail["complete"])
        self.assertEqual(tail["counts"]["verified_objects"], 1)
        self.assertEqual(tail["counts"]["bytes_read"], 3)
        self.assertEqual(len(self.client.bodies), 1)

    def test_exclusive_creation_never_overwrites(self):
        self.path.write_text("existing private inventory")
        with self.assertRaises(FileExistsError):
            self.run_inventory()
        self.assertEqual(self.path.read_text(), "existing private inventory")
        self.assertEqual(self.client.calls, [])

    def test_truncation_tampering_and_appended_data_are_rejected(self):
        self.run_inventory()
        original = self.path.read_bytes()
        cases = [
            original.rsplit(b"\n", 2)[0] + b"\n",
            original.replace(b"unreviewed", b"unrevieWed", 1),
            original + b"{}\n",
            original[:-1],
        ]
        for contents in cases:
            with self.subTest(contents=len(contents)):
                self.path.write_bytes(contents)
                with self.assertRaises(inventory.InventoryError):
                    inventory.verify_inventory(self.path)

    def test_interrupted_report_is_unsealed_and_body_closed(self):
        self.client.add("key")
        self.client.body_hook = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_inventory()
        self.assertTrue(self.client.bodies[0].closed)
        with self.assertRaisesRegex(inventory.InventoryError, "unsealed_report"):
            inventory.verify_inventory(self.path)


if __name__ == "__main__":
    unittest.main()
