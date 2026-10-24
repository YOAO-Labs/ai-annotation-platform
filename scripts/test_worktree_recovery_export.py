"""Recovery ownership, exact media bytes and historical embedded URL boundaries."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from worktree_recovery_export import (
    MEDIA_CACHE_KEY,
    MEDIA_STATES,
    media_bytes,
    seed_media_bundles,
    seed_media_sources,
    typed_media_cache_indexes,
    validate_media_pairs,
    verify_media_bundles,
)
from worktree_recovery_mapping import identity
from worktree_env import WorktreeError

from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import export_media_storage as physical, export_storage
from tests.test_export_media_bundles import FakeDB
from tests.test_export_media_storage import Client


class RecoveryDB(FakeDB):
    def add(self, row):
        if row.id is None:
            row.id = uuid.uuid4()
        super().add(row)

    async def get(self, model, identifier):
        result = self.rows.get(identifier)
        return result if isinstance(result, model) else None

    async def scalar(self, statement):
        params = statement.compile().params
        if "job_id_1" in params:
            return next(
                (
                    row
                    for row in self.rows.values()
                    if isinstance(row, ExportMediaBundle)
                    and row.job_id == params["job_id_1"]
                ),
                None,
            )
        return await super().scalar(statement)

    async def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows.values() if isinstance(row, model)]
        params = statement.compile().params
        if model is ExportArtifact and "output_id_1" in params:
            expected = params["output_id_1"]
            rows = (
                [row for row in rows if row.output_id in expected]
                if isinstance(expected, (list, tuple, set))
                else [row for row in rows if row.output_id == expected]
            )
        return rows


class Objects(Client):
    def __init__(self):
        super().__init__()
        self.metadata = {}

    def put_object(self, **params):
        self.metadata[params["Key"]] = params.get("Metadata", {})
        if isinstance(params["Body"], bytes):
            params["Body"] = io.BytesIO(params["Body"])
        return super().put_object(**params)

    def head_object(self, **params):
        return {
            **super().head_object(**params),
            "Metadata": self.metadata.get(params["Key"], {}),
        }

    def get_object(self, **params):
        return {
            **super().get_object(**params),
            "Metadata": self.metadata.get(params["Key"], {}),
        }

    def generate_presigned_url(self, operation, **options):
        return f"{self.meta.endpoint_url}/{options['Params']['Bucket']}/{options['Params']['Key']}?X-Amz-Signature=source-only"


class ExportMediaRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db, self.client = RecoveryDB(), Objects()
        self.endpoint = physical._runtime_endpoint()
        self.storage = SimpleNamespace(
            client=self.client, export_bucket="source-export-media"
        )
        self.project = SimpleNamespace(id=uuid.uuid4(), owner_id=uuid.uuid4())
        self.sources = {
            role: {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "media_type": "point_cloud" if role == "primary_lidar" else "image",
                "relation": None,
            }
            for role, raw in media_bytes().items()
        }

        async def allocate(_db, row):
            return {
                "schema": 1,
                "purpose": "export",
                "location_id": str(physical.LEGACY_LOCATION_ID),
                "config_revision": 1,
                "binding_generation": 1,
                "endpoint_url": self.endpoint,
                "bucket": self.storage.export_bucket,
                "object_key": export_storage.key_for(
                    row.media, row.project_id, row.job_id, row.id
                ),
                "storage_identity": identity(self.endpoint, self.storage.export_bucket),
            }

        for context in (
            patch(
                "worktree_recovery_export.seed_media_sources",
                AsyncMock(return_value=self.sources),
            ),
            patch.object(export_storage, "allocate", allocate),
            patch.object(
                export_storage, "_runtime_endpoint", return_value=self.endpoint
            ),
            patch.object(
                physical.settings, "minio_export_bucket", self.storage.export_bucket
            ),
        ):
            context.start()
            self.addCleanup(context.stop)
        await seed_media_bundles(self.db, project=self.project, storage=self.storage)
        self.owners = [
            row for row in self.db.rows.values() if isinstance(row, ExportMediaBundle)
        ]
        self.outputs = [
            row for row in self.db.rows.values() if isinstance(row, ExportOutput)
        ]
        self.caches = [
            row for row in self.db.rows.values() if isinstance(row, ExportArtifact)
        ]

    def relocate(self):
        self.storage.export_bucket = "target-export-media"
        for row in [*self.owners, *self.outputs]:
            row.storage_ref = {
                **row.storage_ref,
                "bucket": self.storage.export_bucket,
                "storage_identity": identity(self.endpoint, self.storage.export_bucket),
            }
        for row in self.caches:
            self.db.rows.pop(row.id)

    async def test_seed_claims_explicit_owners_publishes_pair_and_preserves_real_bytes(
        self,
    ):
        self.assertEqual(len(self.owners), len(MEDIA_STATES))
        self.assertEqual({row.state for row in self.outputs}, {"pending", "ready"})
        self.assertEqual(len(self.caches), 1)
        self.assertEqual(self.caches[0].cache_key, MEDIA_CACHE_KEY)
        validate_media_pairs(self.owners, self.outputs)
        self.assertEqual(
            await typed_media_cache_indexes(self.db, self.owners, self.outputs),
            self.caches,
        )
        for row in self.owners:
            self.assertIsNotNone(row.lease_token)
            self.assertEqual(row.object_count, 2)
            for key, expected in row.inventory["objects"].items():
                raw = self.client.current(key)["data"]
                self.assertIn(raw, media_bytes().values())
                self.assertEqual(hashlib.sha256(raw).hexdigest(), expected["sha256"])
        self.db.commit.assert_not_awaited()

    async def test_restore_verifies_saved_copy_and_zip_without_resigning_embedded_urls(
        self,
    ):
        zip_bytes = {
            row.id: self.client.current(row.storage_ref["object_key"])["data"]
            for row in self.outputs
        }
        self.relocate()
        before = deepcopy((self.owners, self.outputs))
        result = await verify_media_bundles(self.db, storage=self.storage)
        self.assertEqual(result["verified_export_media_objects"], 4)
        self.assertFalse(result["export_media_embedded_urls_remapped"])
        self.assertEqual(
            result["export_media_embedded_url_delivery"], "requires-new-bundle-and-zip"
        )
        for row, saved in zip(self.owners, before[0]):
            self.assertEqual(row.input_snapshot, saved.input_snapshot)
            self.assertEqual(row.logical_paths, saved.logical_paths)
            self.assertEqual(row.inventory, saved.inventory)
            self.assertEqual(row.published_at, saved.published_at)
        for row in self.outputs:
            self.assertEqual(
                self.client.current(row.storage_ref["object_key"])["data"],
                zip_bytes[row.id],
            )
        self.assertTrue(all(body.closed for body in self.client.bodies))

    async def test_changed_copy_bytes_refuse_and_close_full_readback_body(self):
        self.relocate()
        key = next(iter(self.owners[0].inventory["objects"]))
        self.client.rows[key][-1]["data"] = (
            b"x" * self.owners[0].inventory["objects"][key]["size_bytes"]
        )
        with self.assertRaises(ValueError):
            await verify_media_bundles(self.db, storage=self.storage)
        self.assertTrue(all(body.closed for body in self.client.bodies))

    async def test_typed_metadata_purpose_is_verified_after_restore(self):
        self.relocate()
        key = next(iter(self.owners[0].inventory["objects"]))
        self.client.metadata[key]["purpose"] = "datasets"
        with self.assertRaises(AssertionError):
            await verify_media_bundles(self.db, storage=self.storage)

    async def test_wrong_runtime_bucket_or_zip_deadline_is_refused(self):
        self.relocate()
        self.storage.export_bucket = "unrelated-runtime-bucket"
        with self.assertRaises(AssertionError):
            await verify_media_bundles(self.db, storage=self.storage)
        self.storage.export_bucket = "target-export-media"
        self.outputs[0].expires_at = datetime.now(timezone.utc)
        with self.assertRaises(WorktreeError):
            await verify_media_bundles(self.db, storage=self.storage)

    async def test_existing_media_cache_cannot_survive_restore(self):
        self.relocate()
        self.db.add(self.caches[0])
        with self.assertRaises(AssertionError):
            await verify_media_bundles(self.db, storage=self.storage)


class ExportMediaSourceRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_only_known_put_and_full_readback_are_registered(self):
        from app.db.models.dataset import DatasetItem
        from app.services import original_sources

        db, client = RecoveryDB(), Objects()
        project = SimpleNamespace(id=uuid.uuid4(), owner_id=uuid.uuid4())
        binding = SimpleNamespace(bucket="owned-source-datasets")
        registered = []

        async def register(_db, item, *, sha256, size_bytes, binding):
            self.assertEqual(
                client.current(item.file_path)["data"],
                media_bytes()[
                    "primary_lidar"
                    if item.file_type == "point_cloud"
                    else "camera_front"
                ],
            )
            self.assertTrue(
                any(
                    method == "get" and values["Key"] == item.file_path
                    for method, values in client.calls
                )
            )
            registered.append(item.id)

        async def resolve(_db, item_id):
            self.assertIn(item_id, registered)
            item = await db.get(DatasetItem, item_id)
            raw = client.current(item.file_path)["data"]
            return original_sources.OriginalSource(
                "dataset_asset",
                uuid.uuid5(item_id, "asset"),
                hashlib.sha256(raw).hexdigest(),
                len(raw),
                "point_cloud" if item.file_type == "point_cloud" else "image",
                item.id,
                None,
                binding.bucket,
                item.file_path,
                physical._runtime_endpoint(),
                physical.LEGACY_LOCATION_ID,
                1,
                1,
                {},
                None,
                None,
                None,
            )

        with (
            patch(
                "app.services.dataset_assets.ensure_legacy_bindings",
                AsyncMock(return_value=binding),
            ),
            patch(
                "app.services.dataset_assets.register_item_asset", side_effect=register
            ),
            patch.object(original_sources, "resolve_item", side_effect=resolve),
        ):
            result = await seed_media_sources(
                db, project=project, storage=SimpleNamespace(client=client)
            )
        self.assertEqual(len(registered), 2)
        self.assertEqual(set(result), {"primary_lidar", "camera_front"})
        self.assertTrue(
            all(
                source["object_key"].startswith("lidar-export-originals/")
                for source in result.values()
            )
        )
        self.assertTrue(all(body.closed for body in client.bodies))
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
