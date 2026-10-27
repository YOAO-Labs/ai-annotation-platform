"""Check restored avatar bytes and states without connecting to resources."""

from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from worktree_recovery_avatar import STATES, content_for, token_for, verify_avatars
from worktree_recovery_mapping import identity


class RecoveryAvatarBytes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        self.owner_id = uuid.UUID(int=21)
        self.endpoint, self.bucket = "http://127.0.0.1:9000", "restored-avatars"
        self.rows, self.objects, self.bodies = {}, {}, []
        for state in STATES:
            token, raw = token_for(state), content_for(state)
            self.objects[token + ".webp"] = raw
            self.rows[token] = SimpleNamespace(
                token=token,
                owner_id=self.owner_id,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                state=state,
                retired_at="retained-time" if state == "retired" else None,
                storage_ref={
                    "schema": 1,
                    "purpose": "avatars",
                    "location_id": str(LEGACY_LOCATION_ID),
                    "config_revision": 1,
                    "binding_generation": 1,
                    "endpoint_url": self.endpoint,
                    "bucket": self.bucket,
                    "object_key": token + ".webp",
                    "storage_identity": identity(self.endpoint, self.bucket),
                },
            )

        def references(statement):
            ref = statement.compile().params["avatar_ref_1"]
            return (
                [SimpleNamespace(id=self.owner_id)]
                if ref.endswith(token_for("ready"))
                else []
            )

        self.db = SimpleNamespace(
            get=AsyncMock(side_effect=lambda _, token: self.rows.get(token)),
            scalars=AsyncMock(side_effect=references),
        )

        def read(**kwargs):
            self.assertEqual(kwargs["Bucket"], self.bucket)
            body = io.BytesIO(self.objects[kwargs["Key"]])
            self.bodies.append(body)
            return {"Body": body}

        self.client = SimpleNamespace(get_object=Mock(side_effect=read))
        self.storage = SimpleNamespace(client=self.client, avatars_bucket=self.bucket)
        endpoint = patch(
            "app.services.avatar_storage._runtime_endpoint", return_value=self.endpoint
        )
        endpoint.start()
        self.addCleanup(endpoint.stop)

    async def test_all_three_states_and_bytes_remain_separate_after_restore(self):
        before = deepcopy(self.rows)
        self.assertEqual(
            await verify_avatars(self.db, storage=self.storage),
            {"pinned_avatar_uploads": 3, "verified_avatar_objects": 3},
        )
        self.assertEqual(self.rows, before)
        self.assertEqual(self.client.get_object.call_count, 3)
        self.assertTrue(all(body.closed for body in self.bodies))

    async def test_changed_bytes_close_before_rejection(self):
        key = token_for("pending") + ".webp"
        self.objects[key] = b"x" * len(self.objects[key])
        with self.assertRaises(AssertionError):
            await verify_avatars(self.db, storage=self.storage)
        self.assertTrue(self.bodies[0].closed)

    async def test_pending_cannot_be_silently_published_by_restore(self):
        self.rows[token_for("pending")].state = "ready"
        with self.assertRaises(AssertionError):
            await verify_avatars(self.db, storage=self.storage)
        self.client.get_object.assert_not_called()

    async def test_wrong_content_receipt_refuses_before_storage(self):
        self.rows[token_for("pending")].sha256 = "a" * 64
        with self.assertRaises(AssertionError):
            await verify_avatars(self.db, storage=self.storage)
        self.client.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
