"""New import addresses are refused before the old nine-bucket mapper mutates."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import remap_storage, prediction_input_changes
import test_worktree_recovery_prediction_inputs as prediction_tests


class ImportRecoveryGuards(unittest.IsolatedAsyncioTestCase):
    async def test_each_new_staging_owner_refuses_before_first_mapping(self):
        for index in range(3):
            with self.subTest(owner=index):
                db = SimpleNamespace(
                    scalar=AsyncMock(side_effect=[None] * index + [object()]),
                    scalars=AsyncMock(),
                    get=AsyncMock(),
                    flush=AsyncMock(),
                    execute=AsyncMock(),
                )
                source = {
                    purpose: "source-" + suffix for purpose, suffix in BUCKETS.items()
                }
                target = {
                    purpose: "target-" + suffix for purpose, suffix in BUCKETS.items()
                }
                before = deepcopy((source, target))
                with self.assertRaisesRegex(WorktreeError, "导入暂存含新归属"):
                    await remap_storage(db, source, target, "http://saved.test:9000")
                self.assertEqual((source, target), before)
                db.scalars.assert_not_called()
                db.get.assert_not_called()
                db.flush.assert_not_called()
                db.execute.assert_not_called()

    async def test_saved_schema2_lease_is_not_reinterpreted_as_legacy(self):
        row = prediction_tests.PredictionInputMapping().row()
        row.storage_ref.update(schema=2, binding_generation=2)
        before = deepcopy(vars(row))
        with (
            patch(
                "app.services.import_storage._runtime_endpoint",
                return_value="http://saved.test:9000",
            ),
            self.assertRaisesRegex(WorktreeError, "预测输入端点与备份不一致"),
        ):
            prediction_input_changes(
                row,
                source_bucket=row.bucket,
                target_bucket="target-import",
                endpoint_url="http://saved.test:9000",
            )
        self.assertEqual(vars(row), before)
