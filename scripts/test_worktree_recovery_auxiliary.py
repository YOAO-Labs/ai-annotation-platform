"""Pure checks for bounded auxiliary recovery reads; never connect to resources."""

from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from worktree_recovery_auxiliary import MAX_BYTES, _artifact, _digest, _png_facts, _read


class RecoveryAuxiliaryBytes(unittest.TestCase):
    def setUp(self):
        self.raw = b"retained exact copy"
        self.key = "projects/p/previews/id/report.json"
        self.receipt = _artifact(self.key, self.raw)
        self.metadata = {"sha256": self.receipt["sha256"], "preview-id": "id"}
        self.body = io.BytesIO(self.raw)
        self.head = {
            "ETag": '"retained-etag"',
            "ContentLength": len(self.raw),
            "Metadata": self.metadata,
        }
        self.client = Mock()
        self.client.head_object.return_value = deepcopy(self.head)
        self.client.get_object.return_value = {**deepcopy(self.head), "Body": self.body}
        self.storage = SimpleNamespace(
            client=self.client, training_bucket="unrelated-current-bucket"
        )
        self.owner = SimpleNamespace(
            bucket="saved-owner-bucket", contains=Mock(return_value=True)
        )

    def tearDown(self):
        self.body.close()

    def test_read_uses_saved_bucket_conditional_get_and_closes_complete_bytes(self):
        self.assertEqual(
            _read(self.storage, self.owner, self.receipt, self.metadata), self.raw
        )
        self.assertTrue(self.body.closed)
        self.client.get_object.assert_called_once_with(
            Bucket="saved-owner-bucket", Key=self.key, IfMatch='"retained-etag"'
        )
        self.assertEqual(self.client.head_object.call_count, 2)
        self.owner.contains.assert_called_once_with(self.key)

    def test_changed_bytes_with_same_size_close_before_rejection(self):
        changed = io.BytesIO(b"x" * len(self.raw))
        self.client.get_object.return_value["Body"] = changed
        try:
            with self.assertRaises(AssertionError):
                _read(self.storage, self.owner, self.receipt, self.metadata)
            self.assertTrue(changed.closed)
        finally:
            changed.close()

    def test_truncated_bytes_close_before_rejection(self):
        self.body.truncate(3)
        with self.assertRaises(AssertionError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.assertTrue(self.body.closed)

    def test_extra_bytes_close_before_rejection(self):
        self.body.seek(0, io.SEEK_END)
        self.body.write(b"extra")
        self.body.seek(0)
        with self.assertRaises(AssertionError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.assertTrue(self.body.closed)

    def test_head_identity_mismatch_refuses_before_get(self):
        self.client.head_object.return_value["Metadata"] = {"sha256": "different"}
        with self.assertRaises(AssertionError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.client.get_object.assert_not_called()

    def test_foreign_key_refuses_before_any_io(self):
        self.owner.contains.side_effect = ValueError("foreign namespace")
        with self.assertRaises(ValueError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.client.head_object.assert_not_called()
        self.client.get_object.assert_not_called()

    def test_invalid_size_budget_refuses_before_any_io(self):
        for size in [True, None, -1, 0, MAX_BYTES + 1]:
            with self.subTest(size=size), self.assertRaises(AssertionError):
                _read(
                    self.storage,
                    self.owner,
                    {**self.receipt, "size_bytes": size},
                    self.metadata,
                )
        self.client.head_object.assert_not_called()
        self.client.get_object.assert_not_called()

    def test_response_identity_drift_closes_before_rejection(self):
        self.client.get_object.return_value["ETag"] = '"changed"'
        with self.assertRaises(AssertionError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.assertTrue(self.body.closed)

    def test_post_read_head_drift_refuses(self):
        self.client.head_object.side_effect = [
            self.head,
            {**self.head, "ETag": '"changed"'},
        ]
        with self.assertRaises(AssertionError):
            _read(self.storage, self.owner, self.receipt, self.metadata)
        self.assertTrue(self.body.closed)

    def test_png_identity_includes_dimensions_and_complete_hash(self):
        image = io.BytesIO()
        Image.new("RGB", (48, 32), "green").save(image, "PNG")
        raw = image.getvalue()
        image.close()
        self.assertEqual(
            _png_facts(raw),
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "width": 48,
                "height": 32,
            },
        )

    def test_wrong_image_format_refuses(self):
        image = io.BytesIO()
        Image.new("RGB", (48, 32), "green").save(image, "JPEG")
        raw = image.getvalue()
        image.close()
        with self.assertRaises(AssertionError):
            _png_facts(raw)

    def test_nested_frozen_fact_digest_is_order_independent_and_content_sensitive(self):
        left = {"fixture": "isolated", "evidence": {"nested": [{"sha256": "a" * 64}]}}
        reordered = {"evidence": deepcopy(left["evidence"]), "fixture": "isolated"}
        self.assertEqual(_digest(left), _digest(reordered))
        reordered["evidence"]["nested"][0]["sha256"] = "b" * 64
        self.assertNotEqual(_digest(left), _digest(reordered))
        with self.assertRaises(ValueError):
            _digest({"not-json": float("nan")})


if __name__ == "__main__":
    unittest.main()
