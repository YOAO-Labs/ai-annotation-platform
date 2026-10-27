"""Exact saved-key, full-content and actual-close thumbnail transport checks."""

from contextlib import contextmanager
import hashlib
import io
from types import SimpleNamespace
import unittest
import uuid

from app.services import media_cache_storage, thumbnail_outputs as thumbnails
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity


class ThumbnailReads(unittest.TestCase):
    def fixture(
        self, data=b"webp", *, stored=None, version="owned-version", close_error=False
    ):
        identifier = uuid.uuid4()
        endpoint = media_cache_storage._runtime_endpoint()
        reference = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": "owned-cache",
            "key_prefix": "",
            "prefix": thumbnails.prefix_for(identifier),
            "storage_identity": managed_storage_identity(endpoint, "owned-cache"),
        }
        output = SimpleNamespace(
            id=identifier,
            storage_ref=reference,
            version_id=version,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )

        class Body(io.BytesIO):
            def close(self):
                super().close()
                if close_error:
                    raise RuntimeError("owned close failed")

        body = Body(data if stored is None else stored)
        state = SimpleNamespace(body=body, client_closed=False, params=None)

        class Client:
            def get_object(self, **params):
                state.params = params
                return {
                    "Body": body,
                    "VersionId": version,
                    "ContentLength": len(data),
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                }

        class Target:
            @contextmanager
            def client(self, **kwargs):
                try:
                    yield Client()
                finally:
                    state.client_closed = True

        return output, Target(), state

    def test_complete_bytes_and_precise_saved_version(self):
        output, target, state = self.fixture()
        thumbnails._verified_bytes(target, output)
        self.assertEqual(
            state.params,
            {
                "Bucket": "owned-cache",
                "Key": output.storage_ref["prefix"] + "thumbnail.webp",
                "VersionId": "owned-version",
            },
        )
        self.assertTrue(state.body.closed)
        self.assertTrue(state.client_closed)

    def test_same_length_tamper_refuses_and_closes(self):
        output, target, state = self.fixture(stored=b"fake")
        with self.assertRaises(ContentError):
            thumbnails._verified_bytes(target, output)
        self.assertTrue(state.body.closed)
        self.assertTrue(state.client_closed)

    def test_trailing_bytes_refuse_and_close(self):
        output, target, state = self.fixture(stored=b"webp-extra")
        with self.assertRaises(ContentError):
            thumbnails._verified_bytes(target, output)
        self.assertTrue(state.body.closed)
        self.assertTrue(state.client_closed)

    def test_unversioned_receipt_does_not_guess_another_version(self):
        output, target, state = self.fixture(version=None)
        thumbnails._verified_bytes(target, output)
        self.assertNotIn("VersionId", state.params)

    def test_close_failure_retains_unsettled_responsibility(self):
        output, target, state = self.fixture(close_error=True)
        with self.assertRaises(RuntimeError) as raised:
            thumbnails._verified_bytes(target, output)
        self.assertTrue(getattr(raised.exception, "storage_operation_unsettled", False))
        self.assertTrue(state.client_closed)


if __name__ == "__main__":
    unittest.main()
