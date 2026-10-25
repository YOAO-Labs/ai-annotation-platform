"""Pure regressions for evidence, limits and the public/private boundary."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from storage_s3_contract import (
    CASES,
    DEFERRED,
    Budget,
    BudgetExceeded,
    ContractError,
    Recorder,
    collect_pages,
    load_lock,
    public_bytes,
    read_body,
    safe_error,
    summarize,
    validate_owner,
)

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_current_immutable_inputs(self):
        lock = load_lock(ROOT / "scripts/storage_s3_candidates.lock.json", ROOT)
        self.assertEqual(len(lock["candidates"]), 2)
        # Seaweed's upstream file deliberately has no trailing LF.
        self.assertFalse(
            (ROOT / lock["candidates"][0]["license_file"]).read_bytes().endswith(b"\n")
        )

    def test_bad_lock_and_license_fail_closed(self):
        original = json.loads(
            (ROOT / "scripts/storage_s3_candidates.lock.json").read_bytes()
        )
        mutations = [
            lambda lock: lock.update(schema="unknown"),
            lambda lock: lock.update(license_file_base="lock"),
            lambda lock: lock["candidates"].pop(),
            lambda lock: lock["candidates"][0].update(image="seaweedfs:latest"),
            lambda lock: lock["candidates"][0].update(platform="linux/arm64"),
            lambda lock: lock["candidates"][0].update(license_file="../outside"),
            lambda lock: lock["candidates"][0].update(license_sha256="0" * 64),
            lambda lock: lock["proxy"].update(image="nginx:latest"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            for mutate in mutations:
                with self.subTest(mutate=mutate):
                    lock = copy.deepcopy(original)
                    mutate(lock)
                    path.write_text(json.dumps(lock))
                    with self.assertRaises(ContractError):
                        load_lock(path, ROOT)

    def test_byte_and_object_budget_preserve_prior_counts(self):
        budget = Budget(objects=1, bytes_per_direction=3)
        budget.consume("upload", 3, ("b", "raw/../%2F"))
        with self.assertRaises(BudgetExceeded):
            budget.consume("upload", 1)
        with self.assertRaises(BudgetExceeded):
            budget.consume("download", 1, ("b", "other"))
        self.assertEqual(budget.bytes, {"upload": 3, "download": 0})
        self.assertIn(("b", "raw/../%2F"), budget.keys)

    def test_boto_converted_memory_body_is_accounted(self):
        budget = Budget()
        operation = type("Operation", (), {"name": "PutObject"})()
        budget.hook(
            {"Bucket": "b", "Key": "k", "Body": io.BytesIO(b"actual")}, operation
        )
        self.assertEqual(budget.bytes["upload"], 6)

    def test_invalid_and_expired_budgets(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ContractError):
                Budget(seconds=value)
        with patch("storage_s3_contract.time.monotonic", side_effect=[0, 2]):
            budget = Budget(seconds=1)
            with self.assertRaises(BudgetExceeded):
                budget.check()

    def test_upload_hook_rejects_unbounded_stream(self):
        budget = Budget()
        with self.assertRaises(ContractError):
            budget.hook(
                {"Body": io.StringIO("unbounded")},
                type("Operation", (), {"name": "PutObject"})(),
            )

    def test_body_checks_length_digest_and_metadata_and_closes(self):
        for response, expected, metadata, valid in [
            ({"ContentLength": 3, "Metadata": {"a": "b"}}, b"abc", {"a": "b"}, True),
            ({"ContentLength": 4}, b"abc", None, False),
            ({"ContentLength": 3}, b"abd", None, False),
            (
                {"ContentLength": 3, "Metadata": {"a": "wrong"}},
                b"abc",
                {"a": "b"},
                False,
            ),
        ]:
            with self.subTest(response=response):
                body = io.BytesIO(b"abc")
                response["Body"] = body
                if valid:
                    self.assertEqual(
                        read_body(
                            response, Budget(), expected=expected, metadata=metadata
                        ),
                        b"abc",
                    )
                else:
                    with self.assertRaises(ContractError):
                        read_body(
                            response, Budget(), expected=expected, metadata=metadata
                        )
                self.assertTrue(body.closed)

    def test_body_closes_on_read_failure(self):
        body = Mock()
        body.read.side_effect = OSError("private upstream failure")
        with self.assertRaises(OSError):
            read_body({"Body": body, "ContentLength": 3}, Budget())
        body.close.assert_called_once()

    def test_missing_repeated_or_duplicate_pagination_is_rejected(self):
        for responses in [
            [{"IsTruncated": True}],
            [{"IsTruncated": True, "NextContinuationToken": "same"}] * 2,
            [
                {
                    "Contents": [{"Key": "a"}],
                    "IsTruncated": True,
                    "NextContinuationToken": "next",
                },
                {"Contents": [{"Key": "a"}]},
            ],
        ]:
            client = Mock()
            client.list_objects_v2.side_effect = responses
            with self.assertRaises(ContractError):
                collect_pages(
                    client, "list_objects_v2", "Contents", Bucket="b", MaxKeys=1
                )

    def test_version_pagination_preserves_raw_keys_and_null(self):
        client = Mock()
        client.list_object_versions.side_effect = [
            {
                "Versions": [{"Key": "a/../%2F", "VersionId": "old"}],
                "IsTruncated": True,
                "NextKeyMarker": "a/../%2F",
                "NextVersionIdMarker": "old",
            },
            {
                "Versions": [{"Key": "a/../%2F", "VersionId": "null"}],
                "IsTruncated": False,
            },
        ]
        result = collect_pages(
            client, "list_object_versions", "Versions", Bucket="b", MaxKeys=1
        )
        self.assertEqual([item["VersionId"] for item in result], ["old", "null"])
        self.assertEqual(
            client.list_object_versions.call_args.kwargs["KeyMarker"], "a/../%2F"
        )

    def test_part_pagination(self):
        client = Mock()
        client.list_parts.side_effect = [
            {
                "Parts": [{"PartNumber": 1}],
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
            },
            {"Parts": [{"PartNumber": 2}]},
        ]
        self.assertEqual(len(collect_pages(client, "list_parts", "Parts")), 2)
        self.assertEqual(client.list_parts.call_args.kwargs["PartNumberMarker"], 1)

    def test_provider_messages_never_cross_boundary(self):
        class ProviderFailure(Exception):
            response = {
                "Error": {"Code": "arbitrary-secret", "Message": "private signed URL"}
            }

        self.assertEqual(safe_error(ProviderFailure("secret")), "unexpected_error")
        self.assertEqual(
            safe_error(ContractError("https://secret")), "assertion_failed"
        )
        for secret in (
            "X-Amz-Signature",
            "X-Amz-Credential",
            "secret_access_key",
            "Authorization",
            "provider_message",
        ):
            with self.subTest(secret=secret), self.assertRaises(ContractError):
                public_bytes({"leak": secret})

    def test_report_cannot_qualify_selection_from_local_success(self):
        recorder = Recorder()
        for name in CASES:
            if name not in DEFERRED:
                recorder.cases[name]["status"] = "passed"
        result = summarize({"cases": recorder.cases})
        self.assertTrue(result["local_baseline_passed"])
        self.assertFalse(result["selection_ready"])
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["counts"]["not_run"], len(DEFERRED))
        recorder.cases["versions"]["status"] = "failed"
        self.assertFalse(summarize(result)["local_baseline_passed"])

    def test_missing_case_is_not_complete(self):
        recorder = Recorder()
        del recorder.cases["disk_full"]
        with self.assertRaises(ContractError):
            summarize({"cases": recorder.cases})

    def test_recorder_retains_failed_attempt_and_stage(self):
        recorder = Recorder()
        with recorder.case("basic") as evidence:
            evidence["completed_before_failure"] = 1
            raise ContractError("body_digest")
        item = recorder.cases["basic"]
        self.assertEqual(
            (item["status"], item["stage"], item["error_code"]),
            ("failed", "basic", "body_digest"),
        )
        self.assertEqual(item["evidence"]["completed_before_failure"], 1)

    def test_owner_labels_checkout_exact_id_and_image(self):
        checkout = str(ROOT)
        manifest = {
            "schema": "aap.storage-poc-owner/v1",
            "nonce": "1" * 32,
            "checkout": checkout,
            "resources": [],
        }
        item = {"kind": "container", "id": "exact-id", "image": "pinned"}
        manifest["resources"].append(item)
        observed = {
            "Id": "exact-id",
            "Config": {
                "Image": "pinned",
                "Labels": {"aap.e07.owner": "1" * 32, "aap.e07.checkout": checkout},
            },
        }
        self.assertTrue(validate_owner(manifest, item, observed, ROOT))
        for mutate in [
            lambda value: value.update(Id="other"),
            lambda value: value["Config"].update(Image="other"),
            lambda value: value["Config"]["Labels"].update({"aap.e07.owner": "other"}),
            lambda value: value["Config"]["Labels"].update(
                {"aap.e07.checkout": "/other"}
            ),
        ]:
            value = copy.deepcopy(observed)
            mutate(value)
            with self.assertRaises(ContractError):
                validate_owner(manifest, item, value, ROOT)


if __name__ == "__main__":
    unittest.main()
