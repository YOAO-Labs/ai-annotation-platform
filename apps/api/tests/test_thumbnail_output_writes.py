"""Conditional creation and verified receipts without services or database writes."""

from contextlib import contextmanager
import hashlib
import io
from types import SimpleNamespace
import unittest

from botocore.exceptions import ClientError

from app.services import storage_access, thumbnail_outputs as thumbnails
from app.services.content_snapshot import ContentError
from tests import test_thumbnail_output_reads as reads


class ThumbnailWrites(unittest.TestCase):
    def fixture(
        self, *, failure=None, tamper=False, bad_receipt=False, close_error=False
    ):
        output, _, _ = reads.ThumbnailReads().fixture()
        state = SimpleNamespace(calls=[], closed=False, body=None)

        class Client:
            def put_object(self, **params):
                state.calls.append(("put", params))
                if failure:
                    raise failure
                return {
                    "ETag": None if bad_receipt else '"own-etag"',
                    "VersionId": "own-version",
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                }

            def get_object(self, **params):
                state.calls.append(("get", params))
                state.body = io.BytesIO(b"fake" if tamper else b"webp")
                return {
                    "Body": state.body,
                    "ETag": '"own-etag"',
                    "VersionId": "own-version",
                    "ContentLength": 4,
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                }

        class Target:
            @contextmanager
            def client(self, **kwargs):
                try:
                    yield Client()
                finally:
                    state.closed = True
                    if close_error:
                        raise storage_access.mark_unsettled(OSError("close failed"))

        return Target(), output.storage_ref, state

    def test_receipt_only_after_conditional_write_full_read_and_close(self):
        target, reference, state = self.fixture()
        result = thumbnails._write(target, reference, b"webp")
        self.assertEqual(
            result,
            {
                "sha256": hashlib.sha256(b"webp").hexdigest(),
                "size_bytes": 4,
                "etag": '"own-etag"',
                "version_id": "own-version",
            },
        )
        self.assertTrue(state.closed and state.body.closed)
        self.assertEqual([op for op, _ in state.calls], ["put", "get"])
        self.assertEqual(state.calls[0][1]["IfNoneMatch"], "*")
        self.assertEqual(state.calls[1][1]["VersionId"], "own-version")
        self.assertEqual(state.calls[1][1]["IfMatch"], '"own-etag"')
        self.assertEqual(
            state.calls[0][1]["Key"], reference["prefix"] + "thumbnail.webp"
        )

    def test_provider_refusal_does_not_leave_an_unsettled_operation(self):
        for code in (
            "AccessDenied",
            "PreconditionFailed",
            "ConditionalRequestConflict",
            "NoSuchBucket",
        ):
            with self.subTest(code=code):
                target, reference, state = self.fixture(
                    failure=ClientError({"Error": {"Code": code}}, "PutObject")
                )
                with self.assertRaises(ContentError) as caught:
                    thumbnails._write(target, reference, b"webp")
                self.assertTrue(caught.exception.thumbnail_write_refused)
                self.assertFalse(
                    getattr(caught.exception, "storage_operation_unsettled", False)
                )
                self.assertTrue(state.closed)
                self.assertEqual(len(state.calls), 1)

    def test_unknown_put_outcome_is_retained(self):
        target, reference, state = self.fixture(failure=OSError("lost response"))
        with self.assertRaises(OSError) as caught:
            thumbnails._write(target, reference, b"webp")
        self.assertTrue(caught.exception.storage_operation_unsettled)
        self.assertTrue(state.closed)

    def test_malformed_put_receipt_is_retained(self):
        target, reference, _ = self.fixture(bad_receipt=True)
        with self.assertRaises(ContentError) as caught:
            thumbnails._write(target, reference, b"webp")
        self.assertTrue(caught.exception.storage_operation_unsettled)

    def test_complete_tampered_read_refuses_without_leaking_operation(self):
        target, reference, state = self.fixture(tamper=True)
        with self.assertRaises(ContentError) as caught:
            thumbnails._write(target, reference, b"webp")
        self.assertFalse(
            getattr(caught.exception, "storage_operation_unsettled", False)
        )
        self.assertTrue(state.closed and state.body.closed)

    def test_close_failure_cannot_publish_a_receipt(self):
        target, reference, _ = self.fixture(close_error=True)
        with self.assertRaises(OSError) as caught:
            thumbnails._write(target, reference, b"webp")
        self.assertTrue(caught.exception.storage_operation_unsettled)

    def test_pre_constructor_policy_denial_proves_no_write(self):
        class Target:
            @contextmanager
            def client(self, **kwargs):
                raise ContentError("connection_unavailable", 503)
                yield

        _, reference, _ = self.fixture()
        with self.assertRaises(ContentError) as caught:
            thumbnails._write(Target(), reference, b"webp")
        self.assertTrue(caught.exception.thumbnail_write_refused)
        self.assertFalse(
            getattr(caught.exception, "storage_operation_unsettled", False)
        )

    def test_unknown_constructor_remains_uncertain(self):
        class Target:
            @contextmanager
            def client(self, **kwargs):
                raise storage_access.mark_unsettled(OSError("constructor failed"))
                yield

        _, reference, _ = self.fixture()
        with self.assertRaises(OSError) as caught:
            thumbnails._write(Target(), reference, b"webp")
        self.assertFalse(getattr(caught.exception, "thumbnail_write_refused", False))
        self.assertTrue(caught.exception.storage_operation_unsettled)


if __name__ == "__main__":
    unittest.main()
