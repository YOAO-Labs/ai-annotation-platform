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
    AUXILIARY_CASES,
    TASK_ORIGINAL_CASES,
    auxiliary_content,
    auxiliary_contract,
    auxiliary_identifier,
    seed_auxiliary_originals,
    seed_task_originals,
    task_original_content,
    task_original_identifier,
    verify_fixed_task_image,
    verify_task_originals,
    verify_auxiliary_originals,
)
from worktree_recovery_mapping import identity


class ExternalProtectionFacts(unittest.TestCase):
    def test_source_responsibility_is_entirely_protected(self):
        from worktree_recovery_fixture import digest, protected_row

        row = {
            "id": "source",
            "external_protection_revision": 1,
            "external_protection": {
                "responsible_party": "External owner",
                "recovery_method": "external_backup",
                "recovery_reference": "OPS:restore",
                "recorded_at": "2026-10-25T00:00:00Z",
                "recorded_by_id": "actor",
            },
        }
        original = digest(protected_row("dataset_sources", row))
        for key in row["external_protection"]:
            changed = deepcopy(row)
            changed["external_protection"][key] = "changed"
            self.assertNotEqual(
                digest(protected_row("dataset_sources", changed)), original
            )
        changed = {**row, "external_protection_revision": 2}
        self.assertNotEqual(digest(protected_row("dataset_sources", changed)), original)


class PredictionInputProtectedFacts(unittest.TestCase):
    def test_prediction_input_mapping_preserves_inventory_and_source_proof(self):
        from worktree_recovery_fixture import digest, protected_row

        table = "prediction_input_leases"
        legacy = {
            "id": "legacy",
            "contract_version": 1,
            "bucket": "source",
            "storage_identity": "old",
            "storage_ref": None,
            "inventory": None,
            "source_ref": None,
        }
        current = {
            "id": "independent",
            "contract_version": 2,
            "bucket": "source",
            "storage_identity": "old",
            "storage_ref": {
                "bucket": "source",
                "storage_identity": "old",
                "key_prefix": "prediction-inputs/immutable-uuid/",
                "purpose": "import",
                "binding_generation": 1,
            },
            "inventory": {
                "schema": 1,
                "objects": {
                    "crop": {
                        "state": "planned",
                        "write_nonce": "immutable-write",
                        "sha256": "frozen",
                    },
                },
            },
            "source_ref": {
                "original": {"bucket": "historical", "facts_digest": "frozen"}
            },
            "state": "cleanup_pending",
            "expires_at": "fixed",
            "retain_until": "fixed-later",
        }
        for before in (legacy, current):
            after = deepcopy(before)
            after.update(bucket="target", storage_identity="new")
            if after["storage_ref"] is not None:
                after["storage_ref"].update(bucket="target", storage_identity="new")
            self.assertEqual(protected_row(table, before), protected_row(table, after))
            self.assertNotEqual(digest(before), digest(after))
        frozen = digest(protected_row(table, current))
        for path in (
            ("inventory", "objects", "crop", "write_nonce"),
            ("inventory", "objects", "crop", "state"),
            ("inventory", "objects", "crop", "sha256"),
            ("source_ref", "original", "bucket"),
            ("source_ref", "original", "facts_digest"),
            ("storage_ref", "key_prefix"),
            ("storage_ref", "binding_generation"),
            ("expires_at",),
            ("retain_until",),
            ("id",),
            ("state",),
        ):
            changed = deepcopy(current)
            target = changed
            for name in path[:-1]:
                target = target[name]
            target[path[-1]] = "forbidden-change"
            self.assertNotEqual(digest(protected_row(table, changed)), frozen, path)


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


class AuxiliaryOriginalRecoveryFixtureTests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_raw_is_five_channel_data_distinct_from_platform_pcd(self):
        import struct
        from PIL import Image

        raw = auxiliary_content("raw_lidar")
        self.assertEqual(len(raw), 40)
        self.assertEqual(
            struct.unpack("<10f", raw), (1, 2, 3, 0.25, 7, 4, 5, 6, 0.5, 8)
        )
        with Image.open(io.BytesIO(auxiliary_content("map"))) as image:
            self.assertEqual(image.format, "PNG")
        self.assertNotEqual(auxiliary_content("map"), raw)
        raw_contract = auxiliary_contract("raw_lidar")
        self.assertEqual(
            set(raw_contract),
            {"sample", "sample_data", "calibrated_sensor", "sensor", "ego_pose"},
        )
        self.assertNotIn("source_storage_key", raw_contract)
        self.assertEqual(auxiliary_content("raw_lidar", "unbound_raw"), b"")
        self.assertEqual(auxiliary_content("raw_lidar", "current_raw"), raw)
        self.assertEqual(
            auxiliary_content("map", "shared_map"), auxiliary_content("map")
        )

    async def test_seed_uses_real_creation_and_binding_boundaries(self):
        from app.db.models.dataset import Dataset, DatasetItem, Scene
        from app.services import (
            auxiliary_originals,
            managed_original_creation as creations,
        )
        from app.services import dataset_assets

        db = SimpleNamespace(
            add=Mock(), flush=AsyncMock(), delete=AsyncMock(), commit=AsyncMock()
        )
        actor, project = (
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4()),
        )
        allocations, streams, publications, written = {}, [], {}, {}

        async def allocate(_db, **options):
            case = options["authority"].get("recovery_case", "platform_pcd")
            identifier = uuid.uuid5(uuid.NAMESPACE_URL, "fixture-allocation:" + case)
            result = {
                **options,
                "id": identifier,
                "object_key": f"reserved/{identifier}/payload",
                "content_type": options.get("content_type", "application/octet-stream"),
            }
            allocations[case] = result
            return result

        async def create(_db, allocation, stream, **options):
            self.assertFalse(stream.closed)
            data = stream.read()
            streams.append(stream)
            case = allocation["authority"].get("recovery_case", "platform_pcd")
            written[case] = data
            self.assertEqual(len(data), allocation["max_bytes"])
            self.assertEqual(options["content_type"], allocation["content_type"])
            return SimpleNamespace(
                creation_id=allocation["id"],
                sha256=hashlib.sha256(data).hexdigest(),
                etag='"receipt"',
            )

        async def publish(_db, allocation, created, *, kind, source_contract):
            self.assertEqual(created.creation_id, allocation["id"])
            self.assertEqual(source_contract, auxiliary_contract(kind))
            case = allocation["authority"]["recovery_case"]
            owner = SimpleNamespace(
                id=created.creation_id,
                object_key=allocation["object_key"],
                sha256=created.sha256,
                size_bytes=allocation["max_bytes"],
            )
            publications[case] = owner
            return owner

        with (
            patch.object(
                creations, "allocate", side_effect=allocate
            ) as primary_allocate,
            patch.object(
                creations, "allocate_auxiliary", side_effect=allocate
            ) as auxiliary_allocate,
            patch.object(creations, "create_stream", side_effect=create) as write,
            patch.object(creations, "attach", new_callable=AsyncMock) as attach,
            patch.object(
                dataset_assets, "register_item_asset", new_callable=AsyncMock
            ) as register,
            patch.object(auxiliary_originals, "publish", side_effect=publish),
            patch.object(
                auxiliary_originals, "bind_raw", new_callable=AsyncMock
            ) as bind_raw,
            patch.object(
                auxiliary_originals, "bind_map", new_callable=AsyncMock
            ) as bind_map,
        ):
            await seed_auxiliary_originals(db, actor=actor, project=project)
        primary_allocate.assert_awaited_once()
        self.assertEqual(auxiliary_allocate.await_count, 5)
        self.assertEqual(write.await_count, 5)
        self.assertEqual(written["unbound_raw"], b"")
        self.assertEqual(allocations["unbound_raw"]["max_bytes"], 0)
        self.assertEqual(
            written["current_raw"], auxiliary_content("raw_lidar", "current_raw")
        )
        self.assertEqual(
            {case for case, raw in written.items() if not raw}, {"unbound_raw"}
        )
        self.assertNotIn("unfinished_raw", written)
        self.assertEqual(set(allocations), {*AUXILIARY_CASES, "platform_pcd"})
        self.assertEqual(
            set(publications), {"current_raw", "shared_map", "deleted_map"}
        )
        self.assertTrue(all(stream.closed for stream in streams))
        register.assert_awaited_once()
        attach.assert_awaited_once()
        bind_raw.assert_awaited_once()
        self.assertEqual(bind_map.await_count, 3)
        self.assertIs(
            bind_map.await_args_list[0].args[2], bind_map.await_args_list[1].args[2]
        )
        for call in bind_map.await_args_list:
            scene, owner = call.args[1:]
            source = scene.source_metadata["nuscenes_export"]
            self.assertEqual(source["map_storage_key"], owner.object_key)
            self.assertEqual(source["map_sha256"], owner.sha256)
            self.assertEqual(source["map_file_size"], owner.size_bytes)
        db.delete.assert_awaited_once()
        self.assertIsInstance(db.delete.await_args.args[0], Dataset)
        self.assertEqual(
            len(
                [
                    call.args[0]
                    for call in db.add.call_args_list
                    if isinstance(call.args[0], DatasetItem)
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                [
                    call.args[0]
                    for call in db.add.call_args_list
                    if isinstance(call.args[0], Scene)
                ]
            ),
            3,
        )
        db.commit.assert_not_awaited()

    async def test_verify_keeps_histories_and_reads_exact_saved_auxiliary_sources(self):
        from app.db.models.dataset import Dataset, DatasetItem, Scene
        from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
        from app.db.models.managed_original_creation import ManagedOriginalCreation
        from app.services import (
            auxiliary_originals,
            managed_original_creation as creations,
        )
        from app.services.content_snapshot import ContentError
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        stamp = datetime(2026, 10, 24, tzinfo=timezone.utc)
        endpoint, target = "http://recovery.invalid:9000", "target-datasets"
        dataset_id = auxiliary_identifier("dataset")
        histories, owners, cases = [], [], {}
        for case in AUXILIARY_CASES:
            kind = "map" if case.endswith("map") else "raw_lidar"
            identifier = uuid.uuid5(uuid.NAMESPACE_URL, "fixture-creation:" + case)
            raw = auxiliary_content(kind, case)
            row = SimpleNamespace(
                id=identifier,
                owner_kind="dataset_auxiliary",
                dataset_id=dataset_id,
                project_id=None,
                task_id=None,
                bucket="historical-datasets",
                authority={"recovery_case": case},
                object_key=f"__aap_auxiliary_originals__/{dataset_id}/{identifier}/payload",
                status="allocated" if case == "unfinished_raw" else "verified",
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                etag='"source-etag"',
                version_id="source-version",
                completed_at=stamp,
                receipt=None
                if case == "unfinished_raw"
                else {"sha256": hashlib.sha256(raw).hexdigest()},
                verification=None if case == "unfinished_raw" else {"matched": True},
                claimed_at=None,
                operation_id=None,
            )
            histories.append(row)
            cases[case] = row
            if case not in {"unbound_raw", "unfinished_raw"}:
                owners.append(
                    SimpleNamespace(
                        id=identifier,
                        creation_id=identifier,
                        dataset_id=dataset_id,
                        kind=kind,
                        location_id=LEGACY_LOCATION_ID,
                        bucket=target,
                        storage_identity=identity(endpoint, target),
                        object_key=row.object_key,
                        sha256=row.sha256,
                        size_bytes=len(raw),
                        etag=row.etag,
                        version_id=row.version_id,
                        created_at=stamp,
                        source_contract=auxiliary_contract(kind),
                    )
                )
        shared_id = cases["shared_map"].id
        item = SimpleNamespace(nuscenes_raw_original_id=cases["current_raw"].id)
        scene = SimpleNamespace(
            nuscenes_map_original_id=shared_id,
            source_metadata={"nuscenes_export": {"map": auxiliary_contract("map")}},
        )

        async def get(model, identifier):
            if model is DatasetItem and identifier == auxiliary_identifier("item"):
                return item
            if model is Scene and identifier in {
                auxiliary_identifier("first"),
                auxiliary_identifier("second"),
            }:
                return scene
            self.assertIn(model, {Dataset, Scene})
            return None

        db = SimpleNamespace(
            scalars=AsyncMock(
                side_effect=lambda stmt: (
                    histories
                    if stmt.column_descriptions[0]["entity"] is ManagedOriginalCreation
                    else owners
                )
            ),
            get=AsyncMock(side_effect=get),
        )
        storage = SimpleNamespace(
            datasets_bucket=target,
            import_bucket="trap-import",
            client=SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint)),
        )
        before = deepcopy((histories, owners))
        by_id = {row.id: row for row in owners}

        async def source(_db, identifier, *, kind):
            self.assertEqual(storage.datasets_bucket, storage.import_bucket)
            self.assertEqual(identifier, auxiliary_identifier("task"))
            owner = (
                by_id[cases["current_raw"].id]
                if kind == "raw_lidar"
                else by_id[shared_id]
            )
            return SimpleNamespace(
                **{
                    field: getattr(owner, field)
                    for field in ("bucket", "object_key", "sha256", "size_bytes")
                },
                content_id=owner.id,
                owner_kind="dataset_auxiliary",
                ownership_facts=owner,
            )

        async def raw_source(db, identifier):
            return await source(db, identifier, kind="raw_lidar")

        async def map_source(db, identifier):
            return await source(db, identifier, kind="map")

        def read(_storage, bucket, key, sha, size):
            self.assertEqual(bucket, target)
            row = next(row for row in histories if row.object_key == key)
            case = row.authority["recovery_case"]
            raw = auxiliary_content(
                "map" if case.endswith("map") else "raw_lidar", case
            )
            self.assertEqual((hashlib.sha256(raw).hexdigest(), len(raw)), (sha, size))
            return raw

        with (
            patch.object(
                creations, "_read", AsyncMock(return_value={"historical": True})
            ),
            patch.object(
                creations,
                "_validate",
                AsyncMock(
                    side_effect=ContentError("managed_original_configuration_changed")
                ),
            ),
            patch.object(
                auxiliary_originals, "resolve_task_raw", side_effect=raw_source
            ),
            patch.object(
                auxiliary_originals, "resolve_scene_map", side_effect=map_source
            ),
            patch(
                "worktree_recovery_export._read_exact", side_effect=read
            ) as physical_read,
        ):
            result = await verify_auxiliary_originals(db, storage=storage)
        self.assertEqual(result["pinned_auxiliary_originals"], 3)
        self.assertEqual(result["verified_auxiliary_objects"], 4)
        self.assertEqual(result["verified_empty_auxiliary_objects"], 1)
        self.assertEqual(result["shared_auxiliary_map_references"], 2)
        self.assertEqual(
            result["auxiliary_recovery_qualification"], "offline-retained-bytes-only"
        )
        self.assertEqual(physical_read.call_count, 6)
        empty_reads = [
            call for call in physical_read.call_args_list if call.args[4] == 0
        ]
        self.assertEqual(len(empty_reads), 1)
        self.assertEqual(empty_reads[0].args[2], cases["unbound_raw"].object_key)
        self.assertEqual(empty_reads[0].args[3], hashlib.sha256(b"").hexdigest())
        self.assertEqual(storage.datasets_bucket, target)
        self.assertEqual((histories, owners), before)
        self.assertIs(
            db.scalars.await_args_list[-1].args[0].column_descriptions[0]["entity"],
            DatasetAuxiliaryOriginal,
        )


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
