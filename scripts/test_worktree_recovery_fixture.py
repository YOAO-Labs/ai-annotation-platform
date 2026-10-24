"""Task-original recovery fixtures preserve current locators and historical proof."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from worktree_recovery_fixture import (
    TASK_ORIGINAL_CASES,
    seed_task_originals,
    task_original_content,
    task_original_identifier,
    verify_task_originals,
)
from worktree_recovery_mapping import identity


class TaskOriginalRecoveryFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_uses_creation_boundary_and_keeps_deleted_task_responsibility(
        self,
    ):
        from app.db.models.task import Task
        from app.services import managed_original_creation as creations, task_originals

        project, actor = (
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4()),
        )
        active, owners, allocations = {}, {}, {}
        db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())

        def add(task):
            self.assertIsInstance(task, Task)
            active[task.id] = task

        async def delete(task):
            active.pop(task.id)

        async def allocate(_db, **values):
            self.assertEqual(values["project_id"], project.id)
            self.assertNotIn("dataset_id", values)
            task_id = values["task_id"]
            creation_id = uuid.uuid5(task_id, "recovery-creation")
            row = {
                "id": creation_id,
                "task_id": task_id,
                "object_key": f"__aap_task_originals__/{task_id}/{creation_id}/payload",
                "staging_bucket": "source-import",
                "staging_key": f"__aap_upload_staging__/{creation_id}/payload",
            }
            allocations[task_id] = row
            return row

        async def complete(_db, allocation):
            return SimpleNamespace(creation_id=allocation["id"])

        async def publish(_db, task, allocation, created):
            self.assertEqual(created.creation_id, allocation["id"])
            task.original_id = created.creation_id
            owners[task.id] = created.creation_id

        db.add, db.delete = Mock(side_effect=add), AsyncMock(side_effect=delete)
        storage = SimpleNamespace(bucket="source-annotations", client=Mock())
        with (
            patch.object(
                creations, "allocate_task", side_effect=allocate
            ) as allocate_mock,
            patch.object(
                creations, "complete_direct", side_effect=complete
            ) as complete_mock,
            patch.object(
                task_originals, "publish", side_effect=publish
            ) as publish_mock,
        ):
            await seed_task_originals(db, actor=actor, project=project, storage=storage)
        self.assertEqual(allocate_mock.await_count, 4)
        self.assertEqual(complete_mock.await_count, 3)
        self.assertEqual(publish_mock.await_count, 2)
        self.assertEqual(
            set(active),
            {task_original_identifier("current"), task_original_identifier("legacy")},
        )
        self.assertEqual(
            set(owners),
            {task_original_identifier("current"), task_original_identifier("deleted")},
        )
        current = active[task_original_identifier("current")]
        self.assertEqual(current.original_id, owners[current.id])
        self.assertEqual(current.status, "pending")
        self.assertIsNone(active[task_original_identifier("legacy")].original_id)
        self.assertEqual(db.delete.await_count, 3)
        db.commit.assert_not_awaited()
        puts = [call.kwargs for call in storage.client.put_object.call_args_list]
        self.assertEqual(len(puts), 5)
        self.assertTrue(all(call["Bucket"] == "source-import" for call in puts[:4]))
        self.assertTrue(all(call["Body"] == task_original_content() for call in puts))
        self.assertEqual(puts[-1]["Key"], "recovery/unknown-task-original.png")

    async def test_verify_reads_current_target_without_rewriting_source_receipts(self):
        from app.db.models.managed_original_creation import ManagedOriginalCreation
        from app.db.models.task_original import TaskOriginal
        from app.services import (
            managed_original_creation as creations,
            managed_original_writer,
        )
        from app.services.content_snapshot import ContentError
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        raw = task_original_content()
        sha = hashlib.sha256(raw).hexdigest()
        endpoint = "http://recovery.invalid:9000"
        stamp = datetime(2026, 10, 24, tzinfo=timezone.utc)
        project_id = uuid.uuid4()
        source_rows, owners = [], []
        for case in TASK_ORIGINAL_CASES:
            task_id = task_original_identifier(case)
            creation_id = uuid.uuid5(task_id, "recovery-creation")
            source = SimpleNamespace(
                id=creation_id,
                owner_kind="task",
                dataset_id=None,
                task_id=task_id,
                project_id=project_id,
                status="allocated" if case == "unfinished" else "verified",
                bucket="historical-source-annotations",
                staging_bucket="historical-source-import",
                object_key=f"__aap_task_originals__/{task_id}/{creation_id}/payload",
                staging_key=f"__aap_upload_staging__/{creation_id}/payload",
                sha256=None if case == "unfinished" else sha,
                size_bytes=None if case == "unfinished" else len(raw),
                etag=None if case == "unfinished" else '"historical-source-etag"',
                version_id=None
                if case == "unfinished"
                else "historical-source-version",
                receipt=None
                if case == "unfinished"
                else {"ETag": '"historical-source-etag"'},
                verification=None if case == "unfinished" else {"sha256": sha},
                settled_at=None if case == "unfinished" else stamp,
                completed_at=None if case == "unfinished" else stamp,
                operation_id=None,
                claimed_at=None,
            )
            source_rows.append(source)
            if case in ("current", "deleted"):
                owners.append(
                    SimpleNamespace(
                        id=creation_id,
                        creation_id=creation_id,
                        task_id=task_id,
                        project_id=project_id,
                        location_id=LEGACY_LOCATION_ID,
                        bucket="target-annotations",
                        storage_identity=identity(endpoint, "target-annotations"),
                        object_key=source.object_key,
                        sha256=sha,
                        size_bytes=len(raw),
                        etag=source.etag,
                        version_id=source.version_id,
                        created_at=stamp,
                    )
                )
        before = deepcopy((source_rows, owners))
        current = SimpleNamespace(
            status="pending", original_id=owners[0].id, file_path=owners[0].object_key
        )
        legacy = SimpleNamespace(
            id=task_original_identifier("legacy"),
            original_id=None,
            dataset_item_id=None,
            file_path="recovery/unknown-task-original.png",
        )
        db = SimpleNamespace(
            scalars=AsyncMock(
                side_effect=lambda stmt: (
                    source_rows
                    if stmt.column_descriptions[0]["entity"] is ManagedOriginalCreation
                    else owners
                )
            ),
            get=AsyncMock(
                side_effect=lambda model, key: {
                    task_original_identifier("current"): current,
                    legacy.id: legacy,
                }.get(key)
            ),
        )
        storage = SimpleNamespace(
            bucket="target-annotations",
            import_bucket="target-import",
            client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint)),
        )
        with (
            patch.object(
                managed_original_writer,
                "inspect_staging",
                return_value=SimpleNamespace(sha256=sha, size_bytes=len(raw)),
            ) as inspect,
            patch.object(
                creations,
                "for_task",
                AsyncMock(
                    side_effect=ContentError("managed_original_configuration_changed")
                ),
            ) as historical,
        ):
            result = await verify_task_originals(db, storage=storage)
        self.assertEqual((source_rows, owners), before)
        self.assertEqual(result["pinned_task_originals"], 2)
        self.assertEqual(result["unfinished_task_creations"], 1)
        self.assertEqual(historical.await_count, 4)
        reads = inspect.call_args_list
        self.assertEqual(len(reads), 8)
        self.assertTrue(
            all(
                call.args[1] in {storage.bucket, storage.import_bucket}
                for call in reads
            )
        )
        self.assertTrue(
            all(
                "VersionId" not in call.kwargs and "IfMatch" not in call.kwargs
                for call in reads
            )
        )
        self.assertEqual(db.scalars.await_count, 2)
        self.assertIs(
            db.scalars.await_args_list[-1].args[0].column_descriptions[0]["entity"],
            TaskOriginal,
        )


if __name__ == "__main__":
    unittest.main()
