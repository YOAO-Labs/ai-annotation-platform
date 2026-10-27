"""New annotation targets must refuse before any legacy restore mapping starts."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.dialects import postgresql

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import remap_storage


class AnnotationsRecoveryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_saved_targets_refuse_before_mapping_in_every_owner_state(self):
        families = {
            "task_originals": (None,),
            "managed_original_creations": (
                "allocated",
                "preparing",
                "prepared",
                "writing",
                "written",
                "verified",
                "failed",
                "conflict",
                "unknown",
            ),
            "comment_attachment_uploads": ("pending", "ready", "retired"),
            "guide_asset_uploads": ("pending", "ready", "retired"),
            "raster_mask_objects": ("pending", "ready", "retired"),
        }
        for table, states in families.items():
            for state in states:
                with self.subTest(table=table, state=state):
                    row = SimpleNamespace(
                        id=uuid.uuid4(),
                        state=state,
                        storage_ref={"schema": 2, "bucket": "source-annotations"},
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
                        if table == "managed_original_creations":
                            # Earlier import/dataset guards query the same table.
                            if "owner_kind = 'task'" not in sql:
                                return None
                            self.assertIn("original_ref", sql)
                            self.assertNotIn("status", sql)
                        else:
                            self.assertNotIn("state", sql)
                            self.assertIn(
                                "storage_ref IS NOT NULL"
                                if table == "task_originals"
                                else "= 2",
                                sql,
                            )
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
                        self.assertRaisesRegex(WorktreeError, "标注文件含新归属"),
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
