"""Pure exact-object capability checks; no database or provider is contacted."""

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid

from jose import jwt

from app.services import media_cache_urls as urls


class MediaCacheUrls(unittest.TestCase):
    def setUp(self):
        self.secret = patch.object(
            urls.settings, "secret_key", "owned-capability-test-key"
        )
        self.secret.start()
        self.addCleanup(self.secret.stop)
        self.identifier = uuid.uuid4()
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.reference = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(uuid.uuid4()),
            "config_revision": 1,
            "binding_generation": 2,
            "endpoint_url": "http://private.invalid:9000",
            "bucket": "private-cache",
            "key_prefix": "公司/_aap_managed/media-cache/",
            "prefix": f"公司/_aap_managed/media-cache/thumbnails/objects/{self.identifier}/",
            "storage_identity": "b" * 64,
        }
        self.key = self.reference["prefix"] + "thumbnail.webp"
        self.receipt = {"sha256": "a" * 64, "size_bytes": 123}

    def token(self, **kwargs):
        url = urls.sign(
            "thumbnail",
            self.identifier,
            self.key,
            self.reference,
            self.receipt,
            kwargs.get("expires", 900),
            now=self.now,
        )
        return parse_qs(urlsplit(url).query)["token"][0]

    def decode(self, token=None, **kwargs):
        return urls.decode(
            token or self.token(),
            kind=kwargs.get("kind", "thumbnail"),
            output_id=kwargs.get("output_id", self.identifier),
            relative=kwargs.get("relative", "thumbnail.webp"),
            now=kwargs.get("now", self.now),
        )

    def forged(self, **updates):
        claims = jwt.get_unverified_claims(self.token())
        claims.update(updates)
        return jwt.encode(claims, urls.settings.secret_key, algorithm="HS256")

    def test_same_origin_exact_object_without_private_topology(self):
        url = urls.sign(
            "thumbnail",
            self.identifier,
            self.key,
            self.reference,
            self.receipt,
            900,
            now=self.now,
        )
        parsed = urlsplit(url)
        self.assertEqual(parsed.netloc, "")
        self.assertEqual(
            parsed.path,
            f"/api/v1/media-cache/thumbnail/{self.identifier}/thumbnail.webp",
        )
        claims = jwt.get_unverified_claims(self.token())
        self.assertNotIn("private.invalid", json.dumps(claims))
        self.assertNotIn("private-cache", json.dumps(claims))
        self.assertEqual(self.decode().reference, urls.reference_digest(self.reference))
        self.assertEqual(self.decode().size_bytes, 123)

    def test_expiry_is_exact_and_does_not_extend_on_read(self):
        token = self.token()
        self.assertEqual(
            self.decode(token, now=self.now + timedelta(seconds=899)).expires_at,
            int(self.now.timestamp()) + 900,
        )
        with self.assertRaises(urls.MediaCacheCapabilityError):
            self.decode(token, now=self.now + timedelta(seconds=900))

    def test_route_cannot_change_kind_owner_or_relative(self):
        for values in (
            {"kind": "image-pyramid"},
            {"output_id": uuid.uuid4()},
            {"relative": "different.webp"},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(urls.MediaCacheCapabilityError),
            ):
                self.decode(**values)

    def test_traversal_and_encoded_keys_are_rejected(self):
        for relative in (
            "",
            "/thumbnail.webp",
            "../thumbnail.webp",
            "a/../b",
            "a//b",
            "a%2fb",
            "a?b",
            "a\\b",
        ):
            with (
                self.subTest(relative=relative),
                self.assertRaises(urls.MediaCacheCapabilityError),
            ):
                urls.sign(
                    "thumbnail",
                    self.identifier,
                    self.reference["prefix"] + relative,
                    self.reference,
                    self.receipt,
                    900,
                    now=self.now,
                )

    def test_key_outside_prefix_is_rejected(self):
        with self.assertRaises(urls.MediaCacheCapabilityError):
            urls.sign(
                "thumbnail",
                self.identifier,
                "other/thumbnail.webp",
                self.reference,
                self.receipt,
                900,
                now=self.now,
            )

    def test_tamper_and_login_audience_are_rejected(self):
        token = self.token()
        with self.assertRaises(urls.MediaCacheCapabilityError):
            self.decode(token[:-8] + "00000000")
        with self.assertRaises(urls.MediaCacheCapabilityError):
            self.decode(self.forged(aud="login", sub="user"))

    def test_closed_typed_claims_and_maximum_lifetime(self):
        for updates in (
            {"extra": "claim"},
            {"schema": True},
            {"size_bytes": True},
            {"sha256": "not-a-digest"},
            {"reference": "invalid"},
            {"iat": int(self.now.timestamp()) + 10},
            {"exp": int(self.now.timestamp()) + 3601},
        ):
            with (
                self.subTest(updates=updates),
                self.assertRaises(urls.MediaCacheCapabilityError),
            ):
                self.decode(self.forged(**updates))

    def test_signer_rejects_invalid_lifetime_and_receipt(self):
        for expires in (0, -1, 3601, True, 1.5):
            with (
                self.subTest(expires=expires),
                self.assertRaises(urls.MediaCacheCapabilityError),
            ):
                self.token(expires=expires)
        with self.assertRaises(urls.MediaCacheCapabilityError):
            urls.sign(
                "thumbnail",
                self.identifier,
                self.key,
                self.reference,
                {"sha256": "a" * 64, "size_bytes": True},
                900,
                now=self.now,
            )

    def test_reference_hash_is_order_independent_and_content_sensitive(self):
        self.assertEqual(
            urls.reference_digest(self.reference),
            urls.reference_digest(dict(reversed(list(self.reference.items())))),
        )
        self.assertNotEqual(
            urls.reference_digest(self.reference),
            urls.reference_digest(self.reference | {"binding_generation": 3}),
        )


if __name__ == "__main__":
    unittest.main()
