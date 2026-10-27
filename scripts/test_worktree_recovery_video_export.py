"""Direct pure unittest nodes; no pytest launcher, database or infrastructure."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid
import zipfile

import worktree_recovery_video_export as fixture


def inputs():
    # Synthetic transport bytes only. The actual seed uses make_videos + the
    # real canonical creation/copy/probe/serializer chain.
    expected = fixture.expected_sources(
        {"dataset": b"dataset-video", "standalone": b"task-video"}
    )
    data = {
        "schema": 4,
        "probe_contract": 1,
        "project_id": str(fixture.identifier("project")),
        "project": {"id": str(fixture.identifier("project")), "data_type": "video"},
        "task_ids": sorted(expected),
        "references": {},
        "segments": [],
        "quality_runs": [],
        "targets": fixture.TARGETS,
        "options": fixture.OPTIONS,
        "tasks": [],
        "media": [],
    }
    for task_id, receipt in expected.items():
        data["tasks"].append(
            {"id": task_id, "file_name": "same.mp4", "file_type": "video"}
        )
        data["media"].append(
            {
                "task_id": task_id,
                "role": "video",
                "link_id": None,
                "source": {
                    "owner_kind": receipt["owner_kind"],
                    "media_type": "video",
                    **fixture._receipt(receipt),
                },
            }
        )
    return data, expected


def archive_input():
    snapshot, expected = inputs()
    started = datetime.now(timezone.utc)
    bundle = SimpleNamespace(
        input_snapshot=snapshot,
        expires_at=started + timedelta(days=7),
        inventory={"objects": {}},
        logical_paths={"targets": {target: {} for target in fixture.TARGETS}},
    )
    metadata = {
        "source_public_path_prefix": "/minio",
        "source_export_bucket": "source-export",
    }
    entries = []
    for task_id, receipt in expected.items():
        relative, sequence = f"tasks/{task_id}/same.mp4", f"tasks/{task_id}/same"
        key = f"lidar-export-media/objects/{uuid.uuid4()}/{uuid.uuid4()}.bin"
        for target in fixture.TARGETS:
            bundle.logical_paths["targets"][target][relative] = key
        bundle.inventory["objects"][key] = fixture._receipt(receipt)
        entries.append(
            {
                "task_id": task_id,
                "rel_path": relative,
                "sequence": sequence,
                "expires_at": bundle.expires_at.isoformat(),
                **fixture._receipt(receipt),
                "source_sha256": receipt["sha256"],
                "source_size_bytes": receipt["size_bytes"],
                "metadata_receipt": {
                    "probe_contract": 1,
                    "video": deepcopy(fixture.VIDEO),
                    **fixture._receipt(receipt),
                },
                "video_metadata": deepcopy(fixture.VIDEO),
                "grid_source_frames": [0, 2, 4],
                "presigned_url": f"/minio/source-export/{key}?signature=historical",
                "frame_outputs": [
                    {"dir": f"mot/{sequence}/img1", "start_number": 1},
                    {"dir": f"kitti/{sequence}/img1", "start_number": 0},
                ],
            }
        )
    manifest = {
        "targets": fixture.TARGETS,
        "expires_at": bundle.expires_at.isoformat(),
        "videos": entries,
    }
    return bundle, metadata, expected, manifest


def archive_bytes(manifest):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        tasks = []
        for entry in manifest["videos"]:
            sequence = entry["sequence"]
            archive.writestr(
                f"mot/{sequence}/gt/gt.txt", "1,1,0,0,1,1\n2,1,0,0,1,1\n3,1,0,0,1,1\n"
            )
            archive.writestr(
                f"kitti/labels/{sequence}.txt", "0 1 Car\n1 1 Car\n2 1 Car\n"
            )
            tasks.append(
                {"id": entry["task_id"], "file_path": "videos/" + entry["rel_path"]}
            )
        for target in ("aap_json", "video_json"):
            archive.writestr(target + "/annotations.json", json.dumps({"tasks": tasks}))
    return output.getvalue()


class RecoveryVideoContracts(unittest.TestCase):
    def test_seed_annotation_uses_mapped_author_and_temporal_fields(self):
        task = SimpleNamespace(id=uuid.uuid4(), project_id=uuid.uuid4())
        actor = SimpleNamespace(id=uuid.uuid4())
        row = fixture.video_annotation(task=task, actor=actor, case="dataset")
        self.assertEqual(row.user_id, actor.id)
        self.assertEqual(row.task_id, task.id)
        self.assertEqual(row.project_id, task.project_id)
        self.assertEqual(row.annotation_type, "video_track_bbox")
        self.assertEqual(row.track_id, row.geometry["track_id"])
        self.assertEqual(
            [key["frame_index"] for key in row.geometry["keyframes"]], [0, 5]
        )

    def test_expected_bytes_are_retained_in_receipt_not_regenerated(self):
        snapshot, expected = inputs()
        fixture._assert_input(snapshot, expected)
        self.assertEqual(
            set(fixture._content(expected).values()), {b"dataset-video", b"task-video"}
        )
        for fault in (
            "sha",
            "size",
            "content",
            "owner",
            "unknown",
            "schema",
            "probe",
            "references",
        ):
            with self.subTest(fault=fault):
                changed, data = deepcopy(expected), deepcopy(snapshot)
                receipt = next(iter(changed.values()))
                if fault == "sha":
                    receipt["sha256"] = "0" * 64
                elif fault == "size":
                    receipt["size_bytes"] = True
                elif fault == "content":
                    receipt["content_base64"] = ""
                elif fault == "owner":
                    data["media"][0]["source"]["owner_kind"] = "dataset_auxiliary"
                elif fault == "unknown":
                    data["task_ids"].append(str(uuid.uuid4()))
                elif fault == "schema":
                    data["schema"] = 3
                elif fault == "probe":
                    data["probe_contract"] = True
                else:
                    data["references"] = {"unknown": {"size": 1}}
                with self.assertRaises(AssertionError):
                    fixture._assert_input(data, changed)

    def test_generator_owns_and_cleans_two_same_named_tiny_files(self):
        paths = []

        def encode(command, **kwargs):
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(command[0], "ffmpeg")
            self.assertIn("libx264", command)
            path = Path(command[-1])
            paths.append(path)
            self.assertEqual(path.name, "same.mp4")
            path.write_bytes(command[command.index("-i") + 1].encode())

        with patch.object(fixture.subprocess, "run", side_effect=encode):
            raw = fixture.make_videos()
        self.assertNotEqual(raw["dataset"], raw["standalone"])
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertFalse(paths[0].parents[1].exists())

    def test_encoder_failure_does_not_echo_native_details_or_leave_files(self):
        paths = []

        def fail(command, **kwargs):
            paths.append(Path(command[-1]))
            raise subprocess.CalledProcessError(1, command, stderr=b"private detail")

        with patch.object(fixture.subprocess, "run", side_effect=fail):
            with self.assertRaises(RuntimeError) as failure:
                fixture.make_videos()
        self.assertNotIn("private detail", str(failure.exception))
        self.assertFalse(paths[0].parents[1].exists())

    def test_archive_receipts_numbering_paths_and_historical_urls(self):
        bundle, metadata, expected, manifest = archive_input()
        before = deepcopy((bundle.input_snapshot, expected, manifest))
        proof = fixture._archive(
            archive_bytes(manifest), bundle=bundle, metadata=metadata, expected=expected
        )
        self.assertEqual(len(proof["metadata_receipts"]), 2)
        self.assertEqual(len(set(proof["media_urls"].values())), 2)
        self.assertEqual(before, (bundle.input_snapshot, expected, manifest))

    def test_archive_refuses_probe_byte_receipt_url_deadline_or_frame_base_tamper(self):
        for fault in (
            "probe",
            "digest",
            "size",
            "url",
            "deadline",
            "frame_base",
            "grid",
        ):
            with self.subTest(fault=fault):
                bundle, metadata, expected, manifest = archive_input()
                entry = manifest["videos"][0]
                if fault == "probe":
                    entry["metadata_receipt"]["video"]["frame_count"] = 5
                elif fault == "digest":
                    entry["sha256"] = "b" * 64
                elif fault == "size":
                    entry["metadata_receipt"]["size_bytes"] += 1
                elif fault == "url":
                    entry["presigned_url"] = "/minio/target/changed?signature=renewed"
                elif fault == "deadline":
                    entry["expires_at"] = datetime.now(timezone.utc).isoformat()
                elif fault == "frame_base":
                    entry["frame_outputs"][0]["start_number"] = 0
                else:
                    entry["grid_source_frames"] = [1, 3, 5]
                with self.assertRaises(AssertionError):
                    fixture._archive(
                        archive_bytes(manifest),
                        bundle=bundle,
                        metadata=metadata,
                        expected=expected,
                    )


class RecoveryVideoSeed(unittest.IsolatedAsyncioTestCase):
    async def test_source_owner_and_creation_facts_only_allow_the_actual_restore_fields(
        self,
    ):
        from app.db.models.dataset import DatasetItem
        from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
        from app.db.models.managed_original_creation import ManagedOriginalCreation
        from app.db.models.task import Task
        from app.db.models.task_original import TaskOriginal
        from app.services.exporting.lidar_inputs import row_value

        _, expected = inputs()
        rows, facts, locator = {}, {}, None
        now = datetime.now(timezone.utc)
        for case in fixture.CASES:
            task_id = fixture.identifier("task:" + case)
            spec = expected[str(task_id)]
            creation_id = fixture.identifier("creation:" + case)
            creation = ManagedOriginalCreation(
                id=creation_id,
                owner_kind="dataset" if case == "dataset" else "task",
                dataset_id=fixture.identifier("dataset") if case == "dataset" else None,
                task_id=task_id if case == "standalone" else None,
                sha256=spec["sha256"],
                size_bytes=spec["size_bytes"],
                status="verified",
                object_key=f"originals/{case}/same.mp4",
                receipt={"full": "receipt"},
                verification={"full": "verification"},
                settled_at=now,
            )
            task = Task(
                id=task_id,
                project_id=fixture.identifier("project"),
                status="pending",
                file_name="same.mp4",
                file_path=creation.object_key,
                file_type="video",
            )
            rows[ManagedOriginalCreation, creation_id] = creation
            rows[Task, task_id] = task
            if case == "dataset":
                item = DatasetItem(
                    id=fixture.identifier("item"), file_path=creation.object_key
                )
                owner = DatasetAsset(
                    id=fixture.identifier("asset"),
                    dataset_item_id=item.id,
                    media_type="video",
                    checksum_algorithm="sha256",
                    checksum_value=creation.sha256,
                    size_bytes=creation.size_bytes,
                    verification_state="verified",
                )
                locator = DatasetAssetLocation(
                    asset_id=owner.id,
                    creation_id=creation.id,
                    bucket="source-datasets",
                    object_key=creation.object_key,
                    role="managed_original",
                    reference_state="present",
                )
                task.dataset_item_id = item.id
                rows[DatasetItem, item.id], rows[DatasetAsset, owner.id] = item, owner
            else:
                owner = TaskOriginal(
                    id=creation_id,
                    creation_id=creation_id,
                    task_id=task_id,
                    sha256=creation.sha256,
                    size_bytes=creation.size_bytes,
                    bucket="source-annotations",
                    storage_identity="a" * 64,
                )
                rows[TaskOriginal, creation_id] = owner
                task.original_id = owner.id
            facts[str(task_id)] = {
                "creation": row_value(creation),
                "owner": row_value(owner),
                "locator": row_value(locator) if case == "dataset" else None,
            }
        locator.bucket = "target-datasets"
        standalone = rows[TaskOriginal, fixture.identifier("creation:standalone")]
        standalone.bucket, standalone.storage_identity = "target-annotations", "b" * 64
        db = SimpleNamespace(
            get=AsyncMock(side_effect=lambda model, key: rows.get((model, key))),
            scalar=AsyncMock(return_value=locator),
        )
        storage = SimpleNamespace(
            bucket="target-annotations",
            datasets_bucket="target-datasets",
            client=Mock(),
        )
        await fixture._verify_sources(db, facts, expected, storage)
        self.assertEqual(storage.client.mock_calls, [])
        for row, field, value in (
            (standalone, "sha256", "0" * 64),
            (locator, "creation_id", uuid.uuid4()),
            (
                rows[ManagedOriginalCreation, fixture.identifier("creation:dataset")],
                "receipt",
                None,
            ),
        ):
            old = getattr(row, field)
            setattr(row, field, value)
            try:
                with self.assertRaises(AssertionError):
                    await fixture._verify_sources(db, facts, expected, storage)
            finally:
                setattr(row, field, old)

    async def test_job_capture_keeps_explicit_scope_and_commits_before_transfer(self):
        from app.services.exporting import video_jobs

        snapshot, expected = inputs()
        project = SimpleNamespace(
            id=fixture.identifier("project"), display_id="P-REC-VIDEO"
        )
        actor = SimpleNamespace(id=uuid.uuid4())
        db = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
        receipt = SimpleNamespace(input_snapshot=snapshot)
        with patch.object(
            video_jobs, "capture_job", AsyncMock(return_value=receipt)
        ) as capture:
            result = await fixture._new_job(
                db,
                state="building",
                project=project,
                actor=actor,
                storage=SimpleNamespace(export_bucket="source-export"),
                expected=expected,
                source_facts={"fixture": "actual seed owns creation receipts"},
            )
        self.assertIs(result, receipt)
        job = db.add.call_args.args[0]
        self.assertEqual(job.id, fixture.job_identifier("building"))
        self.assertEqual(job.celery_task_id, fixture.attempt_identifier("building"))
        self.assertEqual(job.payload["scope"], {"task_ids": sorted(expected)})
        self.assertEqual(job.payload[fixture.FIXTURE_KEY]["expected"], expected)
        self.assertEqual(job.payload["output_authority"]["actor_id"], str(actor.id))
        self.assertEqual(
            capture.await_args.kwargs["task_ids"],
            sorted([uuid.UUID(k) for k in expected], key=str),
        )
        db.commit.assert_awaited_once()

    async def test_pending_zip_is_saved_under_running_attempt_without_publication(self):
        from app.db.models.export_output import ExportOutput
        from app.services import async_job as jobs, export_media_bundles as bundles
        from app.services import export_outputs, export_storage
        from app.services.exporting import video_delivery, video_inputs, packaging
        from app.workers import lidar_export as publisher

        snapshot, _ = inputs()
        receipt = SimpleNamespace(
            id=uuid.uuid4(),
            project_id=fixture.identifier("project"),
            job_id=fixture.job_identifier("building"),
            zip_output_id=uuid.uuid4(),
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            input_snapshot=snapshot,
        )
        candidate = SimpleNamespace(id=receipt.zip_output_id)
        current = SimpleNamespace(state="pending")
        db = SimpleNamespace(commit=AsyncMock(), scalar=AsyncMock(return_value=current))
        events = []
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as temporary:
            temporary.write(b"fixture pending ZIP")
            path = Path(temporary.name)

        async def prepare(db, claimed, *, guard):
            self.assertGreaterEqual(db.commit.await_count, 1)
            await guard(db)
            events.append("copy/probe")
            return object()

        async def io(db, claimed, call, *, guard):
            await guard(db)
            call()
            events.append("saved-pending-zip")

        try:
            with (
                patch.object(bundles, "claim", AsyncMock(return_value=receipt)),
                patch.object(jobs, "mark_running", AsyncMock()) as running,
                patch.object(publisher, "_guard", AsyncMock()),
                patch.object(video_delivery, "prepare", AsyncMock(side_effect=prepare)),
                patch.object(
                    packaging,
                    "build_export_zip",
                    AsyncMock(return_value=(str(path), 4, path.stat().st_size)),
                ),
                patch.object(bundles, "check_claim", AsyncMock()),
                patch.object(
                    export_outputs, "create", AsyncMock(return_value=candidate)
                ) as create,
                patch.object(export_outputs, "same", Mock(return_value=True)),
                patch.object(publisher, "_io", AsyncMock(side_effect=io)),
                patch.object(export_storage, "write", Mock()) as write,
                patch.object(video_inputs, "check", AsyncMock()) as check,
                patch.object(bundles, "publish", AsyncMock()) as publish_bundle,
                patch.object(export_outputs, "publish", AsyncMock()) as publish_zip,
            ):
                await fixture._interrupt_after_pending_zip(db, receipt)
            self.assertEqual(events, ["copy/probe", "saved-pending-zip"])
            self.assertEqual(
                running.await_args.kwargs["celery_task_id"],
                fixture.attempt_identifier("building"),
            )
            self.assertEqual(create.await_args.kwargs["media"], "video")
            self.assertEqual(
                create.await_args.kwargs["output_id"], receipt.zip_output_id
            )
            self.assertEqual(create.await_args.kwargs["expires_at"], receipt.expires_at)
            write.assert_called_once_with(candidate, str(path))
            check.assert_awaited_once_with(db, snapshot, lock=True)
            publish_bundle.assert_not_awaited()
            publish_zip.assert_not_awaited()
            self.assertFalse(path.exists())
            self.assertEqual(ExportOutput.__tablename__, "export_outputs")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
