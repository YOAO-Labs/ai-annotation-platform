"""Refuse new cache address owners before any nine-bucket mapping begins."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.dialects import postgresql

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import remap_storage


class MediaCacheRecoveryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_each_saved_owner_refuses_in_all_states_even_after_return_to_a(self):
        families = {
            "thumbnail_outputs": ("building", "ready", "failed", "retiring", "deleted"),
            "original_cache_entries": ("uploading", "ready", "deleting"),
            "image_pyramid_outputs": ("building", "ready", "retiring", "deleted"),
            "video_derivative_outputs": ("building", "ready", "retiring", "deleted"),
        }
        for table, states in families.items():
            for state in states:
                with self.subTest(table=table, state=state):
                    row = SimpleNamespace(
                        id=uuid.uuid4(),
                        state=state,
                        storage_ref={"schema": 2, "bucket": "source-media-cache"},
                    )
                    before_row = deepcopy(vars(row))

                    async def scalar(query):
                        entity = query.column_descriptions[0]["entity"]
                        if entity.__tablename__ != table:
                            return None
                        sql = str(
                            query.compile(
                                dialect=postgresql.dialect(),
                                compile_kwargs={"literal_binds": True},
                            )
                        )
                        self.assertNotIn("state", sql)
                        if table == "original_cache_entries":
                            self.assertIn("storage_ref IS NOT NULL", sql)
                        elif table.endswith("outputs") and table != "thumbnail_outputs":
                            self.assertIn("= 2", sql)
                        return row.id

                    db = SimpleNamespace(
                        scalar=AsyncMock(side_effect=scalar),
                        scalars=AsyncMock(),
                        get=AsyncMock(),
                        flush=AsyncMock(),
                        execute=AsyncMock(),
                    )
                    source = {key: "source-" + value for key, value in BUCKETS.items()}
                    target = {key: "target-" + value for key, value in BUCKETS.items()}
                    before = deepcopy((source, target))
                    with (
                        patch("worktree_recovery_mapping.bucket_mapping") as mapping,
                        self.assertRaisesRegex(WorktreeError, "媒体缓存含新归属"),
                    ):
                        await remap_storage(
                            db, source, target, "http://saved.test:9000"
                        )
                    mapping.assert_not_called()
                    self.assertEqual((source, target), before)
                    self.assertEqual(vars(row), before_row)
                    db.scalars.assert_not_called()
                    db.get.assert_not_called()
                    db.flush.assert_not_called()
                    db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
