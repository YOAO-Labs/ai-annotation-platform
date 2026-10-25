"""Refuse saved training targets before touching any legacy restore mapping."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from sqlalchemy.dialects import postgresql

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import remap_storage


class TrainingRecoveryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_every_saved_training_owner_refuses_before_mapping(self):
        families = {
            "training_dataset_versions": (
                "pending",
                "running",
                "ready",
                "failed",
                "cancelled",
            ),
            "training_runs": (
                "queued",
                "preparing",
                "training",
                "evaluating",
                "uploading",
                "cancelling",
                "lost",
                "succeeded",
                "failed",
                "cancelled",
            ),
            "training_previews": (
                "queued",
                "running",
                "cancelling",
                "lost",
                "succeeded",
                "failed",
                "cancelled",
                "expired",
            ),
            "sample_evaluation_runs": (
                "queued",
                "running",
                "cancelling",
                "lost",
                "succeeded",
                "failed",
                "cancelled",
            ),
            "active_learning_rounds": (
                "queued",
                "running",
                "cancelling",
                "lost",
                "succeeded",
                "failed",
                "cancelled",
            ),
            "training_storage_deletions": (None,),
        }
        for table, states in families.items():
            for state in states:
                with self.subTest(table=table, state=state):
                    row = SimpleNamespace(
                        id=uuid.uuid4(), status=state, storage_ref={"schema": 2}
                    )
                    before = deepcopy(vars(row))

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
                        self.assertIn("= 2", sql)
                        self.assertNotIn("status", sql)
                        return row.id

                    db = SimpleNamespace(
                        scalar=AsyncMock(side_effect=scalar),
                        scalars=AsyncMock(),
                        get=AsyncMock(),
                        execute=AsyncMock(),
                        flush=AsyncMock(),
                    )
                    source = {k: "source-" + v for k, v in BUCKETS.items()}
                    target = {k: "target-" + v for k, v in BUCKETS.items()}
                    with (
                        patch("worktree_recovery_mapping.bucket_mapping") as mapping,
                        self.assertRaisesRegex(WorktreeError, "训练文件含新归属"),
                    ):
                        await remap_storage(
                            db, source, target, "http://source.test:9000"
                        )
                    mapping.assert_not_called()
                    self.assertEqual(vars(row), before)
                    db.scalars.assert_not_called()
                    db.get.assert_not_called()
                    db.execute.assert_not_called()
                    db.flush.assert_not_called()

    async def test_changed_training_binding_refuses_even_without_an_owner(self):
        for generation in (2, 3):
            with self.subTest(generation=generation):

                async def scalar(query):
                    entity = query.column_descriptions[0]["entity"]
                    if entity.__tablename__ != "storage_bindings":
                        return None
                    sql = str(
                        query.compile(
                            dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True},
                        )
                    )
                    self.assertIn("purpose = 'training'", sql)
                    self.assertIn("generation != 1", sql)
                    self.assertIn("location_id !=", sql)
                    return "training"

                db = SimpleNamespace(
                    scalar=AsyncMock(side_effect=scalar),
                    scalars=AsyncMock(),
                    get=AsyncMock(),
                    execute=AsyncMock(),
                    flush=AsyncMock(),
                )
                source = {k: "source-" + v for k, v in BUCKETS.items()}
                target = {k: "target-" + v for k, v in BUCKETS.items()}
                with (
                    patch("worktree_recovery_mapping.bucket_mapping") as mapping,
                    self.assertRaisesRegex(WorktreeError, "训练用途含新绑定"),
                ):
                    await remap_storage(db, source, target, "http://source.test:9000")
                mapping.assert_not_called()
                db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
