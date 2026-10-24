"""Pure, directly selected recovery nodes; no infrastructure launcher or pytest."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from worktree_recovery_image_export import (
    FIXTURE_KEY,
    OPTIONS,
    QUALIFICATION,
    STATES,
    TARGETS,
    _assert_input,
    _expected,
    _historical_url,
    _interrupt_after_pending_zip,
    _new_job,
    attempt_identifier,
    job_identifier,
    verify_image_exports,
)
from worktree_recovery_export import typed_media_cache_indexes
from worktree_recovery_mapping import identity


def inputs():
    """A fresh schema3 transport model, not a relabelled LiDAR snapshot."""
    from PIL import Image
    from app.services.exporting.image_inputs import digest
    from app.services.exporting.lidar_sources import canonical_identity
    from tests.test_export_media_bundle_images import image_inputs

    project, snapshot, _ = image_inputs(
        managed=2, references=0, repeat=False, targets=TARGETS
    )
    snapshot.update(
        options=deepcopy(OPTIONS),
        batch=None,
        links=[],
        datasets=[],
        items=[],
        annotations=[],
        predictions=[],
        scenes=[],
        tracks=[],
        intervals=[],
    )
    snapshot["project"].update(
        name="Recovery transport",
        display_id="P-REC-IMG",
        type_key="image",
        tool_bindings={"bbox": {"enabled": True, "classes": [{"name": "car"}]}},
    )
    raw = {}
    for index, task in enumerate(snapshot["tasks"]):
        identifier = uuid.UUID(task["id"])
        with (
            io.BytesIO() as output,
            Image.new("RGB", (2, 2), "green" if index == 0 else "purple") as image,
        ):
            image.save(output, format="PNG")
            raw[identifier] = output.getvalue()
        task.update(
            display_id=f"T-REC-IMG-{index}", file_name=f"image-{index}.png", version=1
        )
        binding = snapshot["media"][index]
        source = binding["source"]
        source.update(
            sha256=hashlib.sha256(raw[identifier]).hexdigest(),
            size_bytes=len(raw[identifier]),
        )
        if index:
            source.update(owner_kind="task_original", item_id=None)
            task["dataset_item_id"] = None
        binding["source_key"] = digest(canonical_identity(source))
    snapshot["task_ids"] = sorted(str(identifier) for identifier in raw)
    return project, snapshot, raw


class RecoveryImageContracts(unittest.TestCase):
    def test_exact_known_selection_and_canonical_owner_kinds(self):
        _, snapshot, raw = inputs()
        expected = _expected(raw)
        _assert_input(snapshot, expected)
        for change in ("unknown_task", "references", "owner", "receipt", "scope"):
            with self.subTest(change=change):
                changed = deepcopy(snapshot)
                if change == "unknown_task":
                    changed["tasks"].append({"id": str(uuid.uuid4())})
                elif change == "references":
                    changed["references"] = {changed["tasks"][0]["id"]: {"size": 1}}
                elif change == "owner":
                    changed["media"][1]["source"]["owner_kind"] = "dataset_asset"
                elif change == "receipt":
                    changed["media"][0]["source"]["sha256"] = "0" * 64
                else:
                    changed["task_ids"] = None
                with self.assertRaises(AssertionError):
                    _assert_input(changed, expected)

    def test_historical_root_relative_or_absolute_url_uses_saved_bucket(self):
        metadata = {
            "source_public_path_prefix": "/minio",
            "source_export_bucket": "source",
        }
        for url in (
            "/minio/source/owned/object?signature=unchanged",
            "https://platform.invalid/minio/source/owned/object?signature=unchanged",
        ):
            _historical_url(url, metadata, "owned/object")
        with self.assertRaises(AssertionError):
            _historical_url(
                "/minio/target/owned/object?signature=renewed", metadata, "owned/object"
            )


class RecoveryImageSeed(unittest.IsolatedAsyncioTestCase):
    async def test_capture_has_explicit_scope_and_commit_before_external_sessions(self):
        from app.services.exporting import image_jobs

        project_id, snapshot, raw = inputs()
        project = SimpleNamespace(
            id=project_id, display_id="P-REC-IMG", data_type="image"
        )
        actor = SimpleNamespace(id=uuid.uuid4())
        db = SimpleNamespace(add=Mock(), flush=AsyncMock(), commit=AsyncMock())
        receipt = SimpleNamespace(input_snapshot=snapshot)
        with patch.object(
            image_jobs, "capture_job", AsyncMock(return_value=receipt)
        ) as capture:
            result = await _new_job(
                db,
                state="building",
                project=project,
                actor=actor,
                storage=SimpleNamespace(export_bucket="source-export"),
                expected=_expected(raw),
            )
        self.assertIs(result, receipt)
        job = db.add.call_args.args[0]
        self.assertEqual(job.id, job_identifier("building"))
        self.assertEqual(job.celery_task_id, attempt_identifier("building"))
        self.assertEqual(
            job.payload["scope"],
            {"task_ids": sorted(str(identifier) for identifier in raw)},
        )
        self.assertEqual(job.payload["output_authority"]["actor_id"], str(actor.id))
        self.assertEqual(job.payload[FIXTURE_KEY]["qualification"], QUALIFICATION)
        self.assertEqual(capture.await_args.kwargs["task_ids"], sorted(raw, key=str))
        self.assertEqual(capture.await_args.kwargs["opts"], OPTIONS)
        db.commit.assert_awaited_once()

    async def interrupted(self, *, wrong_candidate=False):
        from app.services import async_job as jobs, export_media_bundles as bundles
        from app.services import export_outputs, export_storage
        from app.services.exporting import image_delivery, image_inputs, packaging
        from app.workers import lidar_export as publisher

        _, snapshot, _ = inputs()
        receipt = SimpleNamespace(
            id=uuid.uuid4(),
            project_id=uuid.UUID(snapshot["project_id"]),
            job_id=job_identifier("building"),
            zip_output_id=uuid.uuid4(),
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
            input_snapshot=snapshot,
        )
        candidate = SimpleNamespace(
            id=receipt.zip_output_id, state="ready" if wrong_candidate else "pending"
        )
        db = SimpleNamespace(
            commit=AsyncMock(), scalar=AsyncMock(return_value=candidate)
        )
        phases = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.zip"
            path.write_bytes(b"owned temporary format output")

            async def prepare(owned, claimed, *, guard):
                self.assertIs(owned, db)
                self.assertIs(claimed, receipt)
                self.assertGreaterEqual(db.commit.await_count, 1)
                await guard(owned)
                phases.append("copy")
                return SimpleNamespace()

            async def io(owned, claimed, call, *, guard):
                await guard(owned)
                self.assertTrue(path.exists())
                result = call()
                await guard(owned)
                phases.append("written")
                return result, 1

            with (
                patch.object(bundles, "claim", AsyncMock(return_value=receipt)),
                patch.object(bundles, "check_claim", AsyncMock()),
                patch.object(jobs, "mark_running", AsyncMock()) as running,
                patch.object(jobs, "mark_complete", AsyncMock()) as complete,
                patch.object(image_delivery, "prepare", prepare),
                patch.object(
                    packaging,
                    "build_export_zip",
                    AsyncMock(return_value=(str(path), 6, path.stat().st_size)),
                ),
                patch.object(
                    export_outputs, "create", AsyncMock(return_value=candidate)
                ),
                patch.object(export_outputs, "same", return_value=True),
                patch.object(export_storage, "write", return_value={}) as write,
                patch.object(publisher, "_guard", AsyncMock()),
                patch.object(publisher, "_io", io),
                patch.object(image_inputs, "check", AsyncMock()) as check,
                patch.object(bundles, "publish", AsyncMock()) as publish,
                patch.object(export_outputs, "publish", AsyncMock()) as publish_zip,
            ):
                if wrong_candidate:
                    with self.assertRaises(AssertionError):
                        await _interrupt_after_pending_zip(db, receipt)
                    write.assert_not_called()
                else:
                    await _interrupt_after_pending_zip(db, receipt)
                    self.assertEqual(phases, ["copy", "written"])
                    running.assert_awaited_once_with(
                        db,
                        receipt.job_id,
                        celery_task_id=attempt_identifier("building"),
                    )
                    check.assert_awaited_once_with(db, snapshot, lock=True)
                    write.assert_called_once_with(candidate, str(path))
                complete.assert_not_awaited()
                publish.assert_not_awaited()
                publish_zip.assert_not_awaited()
                self.assertFalse(path.exists())

    async def test_interrupted_copy_and_pending_zip_keep_running_identity_without_publish(
        self,
    ):
        await self.interrupted()

    async def test_changed_pending_zip_refuses_write_and_cleans_temporary(self):
        await self.interrupted(wrong_candidate=True)


class RecoveryImageTransport(unittest.IsolatedAsyncioTestCase):
    """Synthetic transport facts qualify validator behavior, never source admission."""

    async def asyncSetUp(self):
        from app.config import settings
        from app.db.models.async_job import AsyncJob
        from app.db.models.export_artifact import ExportArtifact
        from app.db.models.export_output import ExportOutput
        from app.services import export_media_bundles as bundles
        from app.services import export_media_storage as physical, export_storage
        from app.services.exporting.image_delivery import Delivery
        from app.services.exporting.image_inputs import digest
        from app.services.exporting.image_media_paths import logical_sources
        from app.services.exporting.packaging import build_export_zip
        from test_worktree_recovery_export import RecoveryDB, Objects

        self.db, self.client = RecoveryDB(), Objects()
        self.project, self.snapshot, self.raw = inputs()
        self.expected = _expected(self.raw)
        self.endpoint = physical._runtime_endpoint()
        self.storage = SimpleNamespace(
            client=self.client, export_bucket="source-image-export"
        )
        self.bundles, self.outputs, self.zip_bytes, self.caches = [], [], {}, []
        self.patch = patch.object(
            settings, "minio_export_bucket", self.storage.export_bucket
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.public = patch.object(settings, "minio_public_url", None)
        self.public.start()
        self.addCleanup(self.public.stop)
        for state in STATES:
            receipt = await bundles.allocate(
                self.db,
                project_id=self.project,
                job_id=job_identifier(state),
                input_snapshot=self.snapshot,
                input_digest=digest(self.snapshot),
                logical_sources=logical_sources(self.snapshot),
            )
            receipt = await bundles.claim(self.db, receipt.id)
            row = self.db.rows[receipt.id]
            keys = bundles.source_objects(receipt, logical_sources(self.snapshot))
            context = Delivery(receipt)
            owner = physical.resolve(receipt)
            for binding in self.snapshot["media"]:
                key = keys[binding["source_key"]]
                raw = self.raw[uuid.UUID(binding["task_id"])]
                self.client.add(key, raw)
                context._receipts[uuid.UUID(binding["task_id"])] = SimpleNamespace(
                    original_sha=hashlib.sha256(raw).hexdigest(),
                    size=len(raw),
                    url=physical.signed_url(
                        self.client, receipt, key[len(owner.prefix) :]
                    ),
                )
            path, count, _ = await build_export_zip(
                self.db,
                self.project,
                batch_id=None,
                targets=TARGETS,
                include_attributes=True,
                video_frame_mode="keyframes",
                image_media=context,
            )
            try:
                raw = Path(path).read_bytes()
            finally:
                Path(path).unlink(missing_ok=True)
            saved = dict(receipt.storage_ref)
            saved.pop("key_prefix")
            key = export_storage.key_for(
                "image", self.project, receipt.job_id, receipt.zip_output_id
            )
            saved["object_key"] = key
            output = ExportOutput(
                id=receipt.zip_output_id,
                project_id=self.project,
                job_id=receipt.job_id,
                media_bundle_id=receipt.id,
                media="image",
                format=",".join(TARGETS),
                file_count=count,
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                storage_ref=saved,
                created_at=receipt.created_at,
                expires_at=receipt.expires_at,
                state="ready" if state == "ready" else "pending",
            )
            self.db.add(output)
            self.client.add(key, raw)
            self.zip_bytes[key] = raw
            if state == "ready":
                await bundles.publish(self.db, receipt)
                cache = ExportArtifact(
                    id=uuid.uuid4(),
                    cache_key="c" * 64,
                    project_id=self.project,
                    batch_id=None,
                    output_id=output.id,
                    format=output.format,
                    object_key=key,
                    file_count=count,
                    size_bytes=len(raw),
                    expires_at=receipt.expires_at,
                )
                self.db.add(cache)
                self.caches.append(cache)
            result = (
                {}
                if state == "building"
                else {
                    "expires_at": receipt.expires_at.isoformat(),
                    "object_key": key,
                    "size_bytes": len(raw),
                    "download_url": self.client.generate_presigned_url(
                        "get_object", Params={"Bucket": owner.bucket, "Key": key}
                    ),
                }
            )
            self.db.add(
                AsyncJob(
                    id=receipt.job_id,
                    status="completed" if state == "ready" else "running",
                    celery_task_id=attempt_identifier(state),
                    completed_at=None
                    if state == "building"
                    else datetime.now(timezone.utc),
                    payload={
                        "image_media": {
                            "bundle_id": str(row.id),
                            "input_digest": row.input_digest,
                        },
                        FIXTURE_KEY: {
                            "schema": 1,
                            "qualification": QUALIFICATION,
                            "state": state,
                            "source_export_bucket": owner.bucket,
                            "source_public_path_prefix": "",
                            "expected": self.expected,
                        },
                    },
                    result=result,
                )
            )
            self.bundles.append(row)
            self.outputs.append(output)

    def relocate(self, *, invalidate=True):
        self.storage.export_bucket = "target-image-export"
        for row in [*self.bundles, *self.outputs]:
            row.storage_ref = {
                **row.storage_ref,
                "bucket": self.storage.export_bucket,
                "storage_identity": identity(self.endpoint, self.storage.export_bucket),
            }
        if invalidate:
            for cache in self.caches:
                self.db.rows.pop(cache.id)

    async def test_ready_and_building_keep_snapshots_bytes_urls_expiry_and_invalidate_schema3_indexes(
        self,
    ):
        before = [
            (
                deepcopy(row.input_snapshot),
                deepcopy(row.inventory),
                row.expires_at,
                row.published_at,
            )
            for row in self.bundles
        ]
        self.assertEqual(
            await typed_media_cache_indexes(self.db, self.bundles, self.outputs),
            self.caches,
        )
        self.relocate()
        result = await verify_image_exports(
            self.db, storage=self.storage, raw_by_task=self.raw
        )
        self.assertEqual(result["verified_image_export_media_objects"], 4)
        self.assertEqual(result["invalidated_schema3_image_export_cache_indexes"], 1)
        self.assertFalse(result["image_export_http_or_celery_dispatch_qualified"])
        self.assertEqual(
            before,
            [
                (row.input_snapshot, row.inventory, row.expires_at, row.published_at)
                for row in self.bundles
            ],
        )
        for key, expected in self.zip_bytes.items():
            self.assertEqual(self.client.current(key)["data"], expected)
        self.assertTrue(
            all(
                options["Bucket"] == self.storage.export_bucket
                for operation, options in self.client.calls
                if operation in {"get", "head"}
            )
        )

    async def test_copy_same_length_tamper_fails_full_hash(self):
        from app.services import export_media_storage as physical

        self.relocate()
        key = next(iter(self.bundles[0].inventory["objects"]))
        raw = self.client.current(key)["data"]
        self.client.add(key, bytes([raw[0] ^ 1]) + raw[1:])
        with self.assertRaises(physical.ExportMediaStorageError):
            await verify_image_exports(
                self.db, storage=self.storage, raw_by_task=self.raw
            )

    async def test_pending_zip_same_length_tamper_is_not_accepted(self):
        self.relocate()
        key = next(iter(self.zip_bytes))
        raw = self.zip_bytes[key]
        self.client.add(key, bytes([raw[0] ^ 1]) + raw[1:])
        with self.assertRaises(AssertionError):
            await verify_image_exports(
                self.db, storage=self.storage, raw_by_task=self.raw
            )

    async def test_schema3_cache_index_must_be_invalidated_after_mapping(self):
        self.relocate(invalidate=False)
        with self.assertRaises(AssertionError):
            await verify_image_exports(
                self.db, storage=self.storage, raw_by_task=self.raw
            )


if __name__ == "__main__":
    unittest.main()
