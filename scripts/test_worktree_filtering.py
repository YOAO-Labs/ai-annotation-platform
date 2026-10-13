"""Manual fixture retention and refusal checks; never connect to a database."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))

from worktree_filtering import prepare_filtering  # noqa: E402
from worktree_env import WorktreeError  # noqa: E402
from app.api.v1._test_seed_filters import FilteringSeedManifest  # noqa: E402


def fixture_manifest():
    identifier = "11111111-1111-4111-8111-111111111111"
    return FilteringSeedManifest.model_validate(
        {
            "users": {},
            "user_emails": {
                "admin": "admin@e2e.test",
                "anno": "anno@e2e.test",
                "rev": "rev@e2e.test",
            },
            "image": {
                "project_id": identifier,
                "task_ids": {},
                "object_ids": {},
                "batch_ids": {},
                "schema": {},
                "expected": {},
                "saved_view_ids": {},
            },
            "video": {
                "project_id": identifier,
                "task_ids": {"both": identifier},
                "candidate_ids": {key: identifier for key in ("low", "at", "above")},
                "tracker_job_ids": {},
                "expected": {},
            },
            "paging": {
                "project_id": identifier,
                "task_id": identifier,
                "object_ids": [],
                "expected_page_one_object_ids": [],
                "expected_page_two_object_ids": [],
            },
            "lidar": {
                "project_id": identifier,
                "scene_id": identifier,
                "task_ids": [],
                "track_refs": [],
                "hidden_track_ref": "hidden",
                "expected_visible_track_refs": [],
                "saved_view_ids": {},
            },
            "operations": {
                **{
                    key: []
                    for key in (
                        "project_ids",
                        "dataset_ids",
                        "template_ids",
                        "user_ids",
                        "user_emails",
                        "invitation_ids",
                        "job_ids",
                        "bug_ids",
                        "audit_ids",
                    )
                },
                "display_names": {},
                "invitation_emails": {},
                "search_keys": {},
                "audit_scopes": {},
            },
        }
    )


class ManualFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "filtering.json"
        self.resources = {"mode": "e2e", "database": "fixture_e2e", "owner": "owned"}
        self.manifest = fixture_manifest()
        self.db = AsyncMock()
        self.db.execute.return_value = Mock(one=lambda: ("fixture_e2e", "owned"))
        self.db.scalar.return_value = 0
        self.db.scalars.return_value = Mock(
            all=lambda: [UUID(self.manifest.image.project_id)]
        )
        session = AsyncMock()
        session.__aenter__.return_value = self.db
        self.seed = AsyncMock(return_value=self.manifest)
        for patcher in (
            patch.dict(os.environ, {"AAP_WORKTREE_MODE": "e2e"}),
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine", return_value=AsyncMock()
            ),
            patch(
                "sqlalchemy.ext.asyncio.async_sessionmaker",
                return_value=Mock(return_value=session),
            ),
            patch("app.api.v1._test_seed._require_e2e_seed_database", new=AsyncMock()),
            patch("app.api.v1._test_seed.seed_filtering", new=self.seed),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_manifest(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "owned",
                    "manifest": self.manifest.model_dump(mode="json", by_alias=True),
                }
            )
        )

    async def test_existing_untracked_data_is_never_reset(self):
        self.db.scalar.return_value = 1
        with self.assertRaisesRegex(WorktreeError, "已有数据"):
            await prepare_filtering(self.resources, self.path)
        self.seed.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_foreign_database_is_rejected_before_seed(self):
        self.db.execute.return_value = Mock(one=lambda: ("fixture_e2e", "foreign"))
        with self.assertRaisesRegex(WorktreeError, "归属不匹配"):
            await prepare_filtering(self.resources, self.path)
        self.seed.assert_not_awaited()

    async def test_restart_preserves_manifest_and_never_reseeds(self):
        self.write_manifest()
        result = await prepare_filtering(self.resources, self.path)
        self.seed.assert_not_awaited()
        self.db.commit.assert_not_awaited()
        self.assertEqual(
            result["manifest"], self.manifest.model_dump(mode="json", by_alias=True)
        )

    async def test_missing_project_is_reported_without_repairing_user_data(self):
        self.write_manifest()
        original = self.path.read_bytes()
        self.db.scalars.return_value = Mock(all=lambda: [])
        with self.assertRaisesRegex(WorktreeError, "显式 reset"):
            await prepare_filtering(self.resources, self.path)
        self.seed.assert_not_awaited()
        self.assertEqual(self.path.read_bytes(), original)

    async def test_first_seed_supplies_two_renderable_frames_and_records_ownership(
        self,
    ):
        predictions = [
            SimpleNamespace(result=[{"score": score}]) for score in (0.2, 0.5, 0.9)
        ]
        self.db.scalar.side_effect = [0, 0, 0, *predictions]
        result = await prepare_filtering(self.resources, self.path)
        self.seed.assert_awaited_once_with(self.db)
        self.assertEqual(
            [p.result[0]["geometry"]["frame_index"] for p in predictions], [0, 0, 10]
        )
        self.assertEqual(
            [p.result[0]["confidence"] for p in predictions], [0.2, 0.5, 0.9]
        )
        self.assertEqual(result["owner"], "owned")
        self.assertEqual(json.loads(self.path.read_text()), result)


if __name__ == "__main__":
    unittest.main()
