"""Task-original recovery fixtures preserve current locators and historical proof."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from worktree_recovery_fixture import (
    TASK_ORIGINAL_CASES,
    seed_task_originals,
    task_original_content,
    task_original_identifier,
    verify_fixed_task_image,
    verify_task_originals,
)
from worktree_recovery_mapping import identity


class ExportMediaProtectedFacts(unittest.IsolatedAsyncioTestCase):
    async def test_only_media_cache_indexes_are_disposable_in_protected_fingerprint(
        self,
    ):
        from sqlalchemy.sql.elements import TextClause
        from worktree_recovery_fixture import database_facts

        identifier = str(uuid.uuid4())
        rows = {
            "export_artifacts": [
                {"id": identifier, "output_id": "bundle-zip", "cache_key": "g1"},
                {"id": "legacy", "output_id": "ordinary-zip", "cache_key": "keep"},
            ],
            "export_media_bundles": [
                {
                    "id": "owner",
                    "storage_ref": {
                        "bucket": "source",
                        "storage_identity": "mapped",
                        "key_prefix": "immutable/",
                    },
                    "inventory": {"private": "unchanged"},
                }
            ],
        }

        async def scalars(statement):
            return list(rows) if isinstance(statement, TextClause) else [identifier]

        async def stream(statement):
            table = str(statement).split('"')[1]

            async def values():
                for row in rows[table]:
                    yield row

            return SimpleNamespace(scalars=values)

        db = SimpleNamespace(scalars=scalars, stream=stream)
        protected = await database_facts(db, protected=True)
        full = await database_facts(db)
        self.assertEqual(protected["export_artifacts"]["count"], 1)
        self.assertEqual(full["export_artifacts"]["count"], 2)
        rows["export_artifacts"].pop(0)
        rows["export_media_bundles"][0]["storage_ref"].update(
            bucket="target", storage_identity="new"
        )
        self.assertEqual(await database_facts(db, protected=True), protected)
        self.assertNotEqual(await database_facts(db), full)
        rows["export_artifacts"][0]["cache_key"] = "unexpected"
        self.assertNotEqual(await database_facts(db, protected=True), protected)
        rows["export_artifacts"][0]["cache_key"] = "keep"
        rows["export_media_bundles"][0]["inventory"]["private"] = "changed"
        self.assertNotEqual(await database_facts(db, protected=True), protected)

    async def test_disposable_indexes_still_count_toward_raw_table_budget(self):
        from worktree_env import WorktreeError
        from worktree_recovery_fixture import database_facts

        async def stream(_statement):
            async def values():
                for _ in range(10_001):
                    yield {"id": "g1-index"}

            return SimpleNamespace(scalars=values)

        db = SimpleNamespace(
            scalars=AsyncMock(return_value=["export_artifacts"]), stream=stream
        )
        with patch(
            "worktree_recovery_export.media_cache_ids",
            AsyncMock(return_value={"g1-index"}),
        ):
            with self.assertRaises(WorktreeError):
                await database_facts(db, protected=True)


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
            patch(
                "worktree_recovery_fixture.verify_fixed_task_image",
                new_callable=AsyncMock,
            ) as business_read,
        ):
            actor = SimpleNamespace(id=uuid.uuid4())
            result = await verify_task_originals(db, storage=storage, actor=actor)
        self.assertEqual((source_rows, owners), before)
        self.assertEqual(result["pinned_task_originals"], 2)
        self.assertEqual(result["unfinished_task_creations"], 1)
        self.assertEqual(result["verified_task_original_content"], 1)
        business_read.assert_awaited_once_with(
            db,
            storage=storage,
            task=current,
            actor=actor,
            sha256=sha,
            raw=raw,
        )
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

    async def test_fixed_task_content_uses_business_reader_with_bucket_traps(self):
        from app.services import dataset_content

        raw, task, actor, db = (
            b"restored image",
            SimpleNamespace(id=uuid.uuid4()),
            object(),
            object(),
        )
        sha = hashlib.sha256(raw).hexdigest()
        storage = SimpleNamespace(
            bucket="target-annotations",
            datasets_bucket="target-datasets",
            import_bucket="target-import",
        )
        file = io.BytesIO(raw)
        snapshot = SimpleNamespace(
            file=file, size=len(raw), sha256=sha, close=Mock(side_effect=file.close)
        )

        async def read(*args):
            self.assertEqual(args, (db, task.id, actor, sha))
            self.assertEqual(
                (storage.bucket, storage.datasets_bucket), (storage.import_bucket,) * 2
            )
            return snapshot

        with patch.object(
            dataset_content, "task_snapshot", side_effect=read
        ) as capture:
            await verify_fixed_task_image(
                db, storage=storage, task=task, actor=actor, sha256=sha, raw=raw
            )
        capture.assert_awaited_once()
        snapshot.close.assert_called_once()
        self.assertTrue(file.closed)
        self.assertEqual(
            (storage.bucket, storage.datasets_bucket),
            ("target-annotations", "target-datasets"),
        )

    async def test_fixed_task_content_restores_buckets_and_closes_failed_image(self):
        from app.services import dataset_content

        for failure in ("reader", "bytes"):
            with self.subTest(failure=failure):
                storage = SimpleNamespace(
                    bucket="annotations",
                    datasets_bucket="datasets",
                    import_bucket="import",
                )
                file = io.BytesIO(b"wrong")
                snapshot = SimpleNamespace(
                    file=file,
                    size=5,
                    sha256="a" * 64,
                    close=Mock(side_effect=file.close),
                )
                read = (
                    AsyncMock(side_effect=RuntimeError("unavailable"))
                    if failure == "reader"
                    else AsyncMock(return_value=snapshot)
                )
                with (
                    patch.object(dataset_content, "task_snapshot", read),
                    self.assertRaises((RuntimeError, AssertionError)),
                ):
                    await verify_fixed_task_image(
                        db=object(),
                        storage=storage,
                        task=SimpleNamespace(id=uuid.uuid4()),
                        actor=object(),
                        sha256="a" * 64,
                        raw=b"right",
                    )
                self.assertEqual(
                    (storage.bucket, storage.datasets_bucket),
                    ("annotations", "datasets"),
                )
                if failure == "bytes":
                    self.assertTrue(file.closed)
                    snapshot.close.assert_called_once()
                else:
                    file.close()


if __name__ == "__main__":
    unittest.main()
