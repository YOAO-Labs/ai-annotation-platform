"""Reject dataset address facts before the legacy mapper changes any owner."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import remap_storage


class DatasetRecoveryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_each_dataset_address_owner_refuses_before_first_mapping(self):
        for index in range(3):
            with self.subTest(owner=index):
                db = SimpleNamespace(
                    scalar=AsyncMock(side_effect=[None] * (3 + index) + [object()]),
                    scalars=AsyncMock(),
                    get=AsyncMock(),
                    flush=AsyncMock(),
                    execute=AsyncMock(),
                )
                source = {key: "source-" + value for key, value in BUCKETS.items()}
                target = {key: "target-" + value for key, value in BUCKETS.items()}
                before = deepcopy((source, target))
                with self.assertRaisesRegex(WorktreeError, "数据集原件含新归属"):
                    await remap_storage(db, source, target, "http://saved.test:9000")
                self.assertEqual((source, target), before)
                db.scalars.assert_not_called()
                db.get.assert_not_called()
                db.flush.assert_not_called()
                db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
