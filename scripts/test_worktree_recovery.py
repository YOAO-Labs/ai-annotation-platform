"""Guard irreversible recovery boundaries without connecting to infrastructure."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from worktree_env import BUCKETS, WorktreeError
from worktree_recovery_mapping import (
    bucket_mapping,
    identity,
    remap_storage,
    storage_view,
)
from verify_worktree_recovery import (
    prepare_empty_target,
    read_key,
    require_empty_database,
    require_offline,
)


def roles(prefix):
    return {name: prefix + "-" + suffix for name, suffix in BUCKETS.items()}


class RecoveryGuards(unittest.TestCase):
    def test_optimized_python_refuses_before_resource_allocation(self):
        script = Path(__file__).with_name("verify_worktree_recovery.py")
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            result = subprocess.run(
                [sys.executable, "-O", str(script), "--report", str(report)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("必须保留全部校验", result.stderr)
            self.assertFalse(report.exists())

    def test_foreign_resource_and_existing_target_refuse_before_first_write(self):
        resources = {
            "owner": "this-owner",
            "database": "db",
            "redis_name": "redis",
            "buckets": roles("target"),
        }
        for inventory in (
            {
                "database": {"owner": "foreign", "sessions": 0},
                "redis": None,
                "buckets": {},
            },
            {
                "database": {"owner": "this-owner", "sessions": 0},
                "redis": None,
                "buckets": {},
            },
            {
                "database": None,
                "redis": None,
                "buckets": {"target-datasets": {"owner": "foreign"}},
            },
        ):
            backend = Mock(resources=resources)
            backend.inspect.return_value = inventory
            with self.assertRaises(WorktreeError):
                prepare_empty_target(backend)
            backend.database.assert_not_called()
            backend.s3.create_bucket.assert_not_called()
            backend.start_redis.assert_not_called()

    def test_an_unknown_client_blocks_offline_capture(self):
        backend = Mock(
            resources={
                "owner": "owner",
                "database": "db",
                "redis_name": "redis",
                "buckets": {},
            }
        )
        backend.inspect.return_value = {
            "database": {"owner": "owner", "sessions": 1},
            "redis": None,
            "buckets": {},
        }
        with self.assertRaisesRegex(WorktreeError, "仍有客户端"):
            require_offline(backend)

    def test_key_is_separate_private_and_matches_generation(self):
        from cryptography.fernet import Fernet

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "key.json"
            key = Fernet.generate_key().decode()
            content = json.dumps({"connector_encryption_key": key}).encode()
            path.write_bytes(content)
            path.chmod(0o600)
            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(read_key(path, digest), key)
            with self.assertRaises(WorktreeError):
                read_key(path, "0" * 64)
            path.chmod(0o644)
            with self.assertRaises(WorktreeError):
                read_key(path, digest)
            path.chmod(0o600)
            for value in (None, 42, [], {}):
                invalid = json.dumps({"connector_encryption_key": value}).encode()
                path.write_bytes(invalid)
                with self.assertRaisesRegex(WorktreeError, "格式不合法"):
                    read_key(path, hashlib.sha256(invalid).hexdigest())
            path.write_bytes(content)
            link = Path(temporary) / "linked-key"
            link.symlink_to(path)
            with self.assertRaises(WorktreeError):
                read_key(link, digest)
            path.unlink()
            with self.assertRaises(WorktreeError):
                read_key(path, digest)

    def test_all_purposes_and_alias_partition_are_required(self):
        source, target = roles("source"), roles("target")
        self.assertEqual(len(bucket_mapping(source, target)), 9)
        for changed in (
            {},
            {**target, "MINIO_BUCKET": target["MINIO_DATASETS_BUCKET"]},
            {**target, "MINIO_BUCKET": source["MINIO_BUCKET"]},
        ):
            with self.assertRaises(WorktreeError):
                bucket_mapping(source, changed)
        source["MINIO_BUCKET"] = source["MINIO_DATASETS_BUCKET"]
        with self.assertRaises(WorktreeError):
            bucket_mapping(source, target)
        target["MINIO_BUCKET"] = target["MINIO_DATASETS_BUCKET"]
        self.assertEqual(len(bucket_mapping(source, target)), 8)


class RecoveryDatabaseGuards(unittest.IsolatedAsyncioTestCase):
    async def test_empty_target_is_checked_again_before_restore(self):
        backend = SimpleNamespace(
            url="postgresql+asyncpg://unused/owned-target",
            resources={"database": "owned-target", "owner": "owner"},
            database=AsyncMock(return_value={"owner": "owner", "sessions": 0}),
        )
        connection = SimpleNamespace(scalar=AsyncMock(return_value=1))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=connection)
        context.__aexit__ = AsyncMock(return_value=False)
        engine = SimpleNamespace(
            connect=Mock(return_value=context), dispose=AsyncMock()
        )
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine):
            with self.assertRaisesRegex(WorktreeError, "非空恢复目标"):
                await require_empty_database(backend)
            connection.scalar.assert_awaited_once()
            engine.dispose.assert_awaited_once()
            connection.scalar.return_value = 0
            await require_empty_database(backend)
            backend.database.return_value = {"owner": "owner", "sessions": 1}
            engine.connect.reset_mock()
            with self.assertRaisesRegex(WorktreeError, "仍有客户端"):
                await require_empty_database(backend)
            engine.connect.assert_not_called()


class MappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.db.models.dataset_asset import (
            DatasetAssetLocation,
            StorageBinding,
            StorageLocation,
        )
        from app.db.models.dataset_source import DatasetSourcePreview
        from app.db.models.original_cache import OriginalCacheEntry
        from app.db.models.managed_original_retirement import ManagedOriginalRetirement
        from app.db.models.managed_original_creation import ManagedOriginalCreation
        from app.db.models.prediction_input import PredictionInputLease
        from app.db.models.training_storage_deletion import TrainingStorageDeletion
        from app.db.models.training_dataset import TrainingDatasetVersion
        from app.db.models.training_run import TrainingRun
        from app.db.models.training_preview import TrainingPreview
        from app.db.models.sample_evaluation import SampleEvaluationRun
        from app.db.models.active_learning import ActiveLearningRound
        from app.db.models.avatar_upload import AvatarUpload
        from app.db.models.export_output import ExportOutput
        from app.db.models.bug_attachment_upload import BugAttachmentUpload
        from app.db.models.comment_attachment_upload import CommentAttachmentUpload
        from app.db.models.guide_asset_upload import GuideAssetUpload
        from app.db.models.raster_mask_object import (
            RasterMaskObject,
            RasterMaskPublication,
        )
        from app.db.models.project_guide_asset import ProjectGuideAsset
        from app.db.models.audit_archive_output import AuditArchiveOutput
        from app.db.models.task_original import TaskOriginal
        from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        self.models = SimpleNamespace(
            asset=DatasetAssetLocation,
            binding=StorageBinding,
            location=StorageLocation,
            preview=DatasetSourcePreview,
            cache=OriginalCacheEntry,
            input=PredictionInputLease,
            deletion=TrainingStorageDeletion,
            retirement=ManagedOriginalRetirement,
            creation=ManagedOriginalCreation,
            task_original=TaskOriginal,
            auxiliary_original=DatasetAuxiliaryOriginal,
            version=TrainingDatasetVersion,
            run=TrainingRun,
            training_preview=TrainingPreview,
            evaluation=SampleEvaluationRun,
            learning=ActiveLearningRound,
            avatar=AvatarUpload,
            output=ExportOutput,
            bug_attachment=BugAttachmentUpload,
            comment_attachment=CommentAttachmentUpload,
            guide_asset=GuideAssetUpload,
            guide_reference=ProjectGuideAsset,
            raster_object=RasterMaskObject,
            raster_publication=RasterMaskPublication,
            archive=AuditArchiveOutput,
        )
        self.source, self.target, self.endpoint = (
            roles("source"),
            roles("target"),
            "http://127.0.0.1:9000",
        )
        self.location_id = LEGACY_LOCATION_ID
        self.endpoint_patch = patch(
            "app.services.training_dataset_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        self.endpoint_patch.start()
        self.addCleanup(self.endpoint_patch.stop)
        avatar_endpoint = patch(
            "app.services.avatar_storage._runtime_endpoint", return_value=self.endpoint
        )
        avatar_endpoint.start()
        self.addCleanup(avatar_endpoint.stop)
        export_endpoint = patch(
            "app.services.export_storage._runtime_endpoint", return_value=self.endpoint
        )
        export_endpoint.start()
        self.addCleanup(export_endpoint.stop)
        bug_endpoint = patch(
            "app.services.bug_attachment_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        bug_endpoint.start()
        self.addCleanup(bug_endpoint.stop)
        comment_endpoint = patch(
            "app.services.comment_attachment_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        comment_endpoint.start()
        self.addCleanup(comment_endpoint.stop)
        guide_endpoint = patch(
            "app.services.guide_asset_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        guide_endpoint.start()
        self.addCleanup(guide_endpoint.stop)
        raster_endpoint = patch(
            "app.services.raster_object_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        raster_endpoint.start()
        self.addCleanup(raster_endpoint.stop)
        archive_endpoint = patch(
            "app.services.audit_archive_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        archive_endpoint.start()
        self.addCleanup(archive_endpoint.stop)
        project_id, version_id = uuid.UUID(int=12), uuid.UUID(int=13)
        training_ref = {
            "schema": 1,
            "purpose": "training-dataset",
            "location_id": str(LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": self.endpoint,
            "bucket": self.source["MINIO_TRAINING_BUCKET"],
            "prefix": f"projects/{project_id}/datasets/{version_id}/",
            "storage_identity": identity(
                self.endpoint, self.source["MINIO_TRAINING_BUCKET"]
            ),
            "origin": "verified-retained",
            "evidence": {"inventory_digest": "a" * 64},
        }
        run_id = uuid.UUID(int=14)
        run_ref = {
            **training_ref,
            "purpose": "training-run",
            "prefix": f"projects/{project_id}/runs/{run_id}/",
        }
        self.rows = {
            AuditArchiveOutput: [
                SimpleNamespace(
                    id=uuid.UUID(int=26),
                    job_id=uuid.UUID(int=27),
                    year=2002,
                    month=1,
                    source_schema="public",
                    source_table="audit_logs_y2002m01",
                    parent_oid=123,
                    source_oid=456,
                    month_start=date(2002, 1, 1),
                    month_end=date(2002, 2, 1),
                    format_revision=1,
                    row_count=2,
                    source_size_bytes=456,
                    source_sha256="e" * 64,
                    sha256="f" * 64,
                    size_bytes=345,
                    state="ready",
                    created_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    published_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    storage_ref={
                        "schema": 1,
                        "purpose": "audit-archive",
                        "location_id": str(LEGACY_LOCATION_ID),
                        "config_revision": 1,
                        "binding_generation": 1,
                        "endpoint_url": self.endpoint,
                        "bucket": self.source["MINIO_AUDIT_ARCHIVE_BUCKET"],
                        "object_key": f"2002/01/{uuid.UUID(int=26)}.jsonl.gz",
                        "storage_identity": identity(
                            self.endpoint, self.source["MINIO_AUDIT_ARCHIVE_BUCKET"]
                        ),
                    },
                )
            ],
            BugAttachmentUpload: [
                SimpleNamespace(
                    id=uuid.UUID(int=23),
                    owner_id=uuid.UUID(int=24),
                    bound_report_id=uuid.UUID(int=25),
                    file_name="recovery.png",
                    mime_type="image/png",
                    sha256="d" * 64,
                    size_bytes=345,
                    state="ready",
                    created_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    expires_at=datetime(2027, 4, 22, tzinfo=timezone.utc),
                    storage_ref={
                        "schema": 1,
                        "purpose": "bug-reports",
                        "location_id": str(LEGACY_LOCATION_ID),
                        "config_revision": 1,
                        "binding_generation": 1,
                        "endpoint_url": self.endpoint,
                        "bucket": self.source["MINIO_BUG_REPORTS_BUCKET"],
                        "object_key": f"bug-report-attachments/{uuid.UUID(int=24)}/{uuid.UUID(int=23)}.png",
                        "storage_identity": identity(
                            self.endpoint, self.source["MINIO_BUG_REPORTS_BUCKET"]
                        ),
                    },
                )
            ],
            ExportOutput: [
                SimpleNamespace(
                    id=uuid.UUID(int=20),
                    project_id=project_id,
                    job_id=uuid.UUID(int=21),
                    media="image",
                    format="aap_json",
                    file_count=1,
                    sha256="c" * 64,
                    size_bytes=234,
                    state="pending",
                    created_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
                    expires_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
                    storage_ref={
                        "schema": 1,
                        "purpose": "export",
                        "location_id": str(LEGACY_LOCATION_ID),
                        "config_revision": 1,
                        "binding_generation": 1,
                        "endpoint_url": self.endpoint,
                        "bucket": self.source["MINIO_EXPORT_BUCKET"],
                        "object_key": f"image/{project_id}/{uuid.UUID(int=21)}/{uuid.UUID(int=20)}.zip",
                        "storage_identity": identity(
                            self.endpoint, self.source["MINIO_EXPORT_BUCKET"]
                        ),
                    },
                )
            ],
            AvatarUpload: [
                SimpleNamespace(
                    token="a" * 32,
                    owner_id=uuid.UUID(int=19),
                    sha256="b" * 64,
                    size_bytes=123,
                    state="retired",
                    storage_ref={
                        "schema": 1,
                        "purpose": "avatars",
                        "location_id": str(LEGACY_LOCATION_ID),
                        "config_revision": 1,
                        "binding_generation": 1,
                        "endpoint_url": self.endpoint,
                        "bucket": self.source["MINIO_AVATARS_BUCKET"],
                        "object_key": "a" * 32 + ".webp",
                        "storage_identity": identity(
                            self.endpoint, self.source["MINIO_AVATARS_BUCKET"]
                        ),
                    },
                )
            ],
            TrainingDatasetVersion: [
                SimpleNamespace(
                    id=version_id, project_id=project_id, storage_ref=dict(training_ref)
                )
            ],
            TrainingRun: [
                SimpleNamespace(id=run_id, project_id=project_id, storage_ref=run_ref)
            ],
            StorageBinding: [
                SimpleNamespace(
                    purpose=suffix,
                    location_id=LEGACY_LOCATION_ID,
                    generation=1,
                    bucket=self.source[role],
                )
                for role, suffix in BUCKETS.items()
            ],
            DatasetAssetLocation: [
                SimpleNamespace(
                    asset_id="asset",
                    location_id=LEGACY_LOCATION_ID,
                    bucket=self.source["MINIO_DATASETS_BUCKET"],
                )
            ],
            DatasetSourcePreview: [
                SimpleNamespace(
                    source_id="source",
                    path_digest="digest",
                    location_id=LEGACY_LOCATION_ID,
                    bucket=self.source["MINIO_MEDIA_CACHE_BUCKET"],
                )
            ],
            OriginalCacheEntry: [
                SimpleNamespace(
                    id="cache",
                    location_id=LEGACY_LOCATION_ID,
                    binding_generation=1,
                    bucket=self.source["MINIO_MEDIA_CACHE_BUCKET"],
                    storage_identity=identity(
                        self.endpoint, self.source["MINIO_MEDIA_CACHE_BUCKET"]
                    ),
                )
            ],
            PredictionInputLease: [
                SimpleNamespace(
                    id="input",
                    bucket=self.source["MINIO_IMPORT_BUCKET"],
                    storage_identity=identity(
                        self.endpoint, self.source["MINIO_IMPORT_BUCKET"]
                    ),
                )
            ],
            TrainingStorageDeletion: [
                SimpleNamespace(
                    id="deletion",
                    bucket=self.source["MINIO_TRAINING_BUCKET"],
                    prefix=training_ref["prefix"],
                    storage_ref=dict(training_ref),
                ),
                SimpleNamespace(
                    id="run-deletion",
                    bucket=self.source["MINIO_TRAINING_BUCKET"],
                    prefix=run_ref["prefix"],
                    storage_ref=dict(run_ref),
                ),
            ],
            ManagedOriginalRetirement: [
                SimpleNamespace(
                    id="retirement",
                    location_id=LEGACY_LOCATION_ID,
                    location_config_revision=1,
                    binding_generation=1,
                    identity_state="verified_binding",
                    role="managed_original",
                    bucket=self.source["MINIO_DATASETS_BUCKET"],
                    storage_identity=identity(
                        self.endpoint, self.source["MINIO_DATASETS_BUCKET"]
                    ),
                    assertion_digest="immutable-assertion",
                    retain_until=None,
                    dataset_id="historical-dataset",
                )
            ],
        }
        for model, purpose, namespace, identifier in [
            (TrainingPreview, "training-preview", "previews", uuid.UUID(int=15)),
            (
                SampleEvaluationRun,
                "sample-evaluation",
                "sample-evaluations",
                uuid.UUID(int=16),
            ),
            (
                ActiveLearningRound,
                "active-learning",
                "active-learning",
                uuid.UUID(int=17),
            ),
        ]:
            self.rows[model] = [
                SimpleNamespace(
                    id=identifier,
                    project_id=project_id,
                    storage_ref={
                        **training_ref,
                        "purpose": purpose,
                        "prefix": f"projects/{project_id}/{namespace}/{identifier}/",
                    },
                )
            ]
        comment = deepcopy(self.rows[BugAttachmentUpload][0])
        comment.id = uuid.UUID(int=30)
        comment.annotation_id = uuid.UUID(int=31)
        comment.task_id = uuid.UUID(int=32)
        comment.project_id = project_id
        comment.bound_comment_id = uuid.UUID(int=33)
        del comment.bound_report_id
        comment.file_name = "empty.dat"
        comment.mime_type = "application/octet-stream"
        comment.size_bytes = 0
        comment.sha256 = hashlib.sha256(b"").hexdigest()
        comment.expires_at = comment.created_at + timedelta(hours=2160)
        comment.storage_ref.update(
            purpose="annotations",
            bucket=self.source["MINIO_BUCKET"],
            object_key=f"comment-attachments/{comment.annotation_id}/{comment.id}",
            storage_identity=identity(self.endpoint, self.source["MINIO_BUCKET"]),
        )
        self.rows[CommentAttachmentUpload] = [comment]
        guide = SimpleNamespace(
            id=uuid.UUID(int=34),
            owner_id=uuid.UUID(int=35),
            project_id=project_id,
            sha256=hashlib.sha256(b"<svg/>").hexdigest(),
            size_bytes=6,
            mime_type="image/svg+xml",
            file_name="guide.svg",
            created_at=comment.created_at,
            state="ready",
            retired_at=None,
            last_attempted_at=comment.created_at,
            storage_ref={
                **comment.storage_ref,
                "object_key": f"projects/{project_id}/guide/{uuid.UUID(int=34)}",
            },
        )
        self.rows[GuideAssetUpload] = [guide]
        self.rows[ProjectGuideAsset] = [
            SimpleNamespace(
                project_id=project_id,
                upload_id=guide.id,
                created_at=comment.created_at,
            )
        ]
        from app.services import raster_object_storage
        from worktree_recovery_raster_objects import encoded

        reference, raw = encoded("ready-json")
        raster = SimpleNamespace(
            id=uuid.UUID(int=38),
            **raster_object_storage.content(reference, raw),
            state="ready",
            created_at=comment.created_at,
            published_at=comment.created_at,
            last_used_at=comment.created_at,
            retired_at=None,
            last_attempted_at=comment.created_at,
            storage_ref={
                **comment.storage_ref,
                "object_key": raster_object_storage.key_for(uuid.UUID(int=38)),
            },
        )
        self.rows[RasterMaskObject] = [raster]
        self.rows[RasterMaskPublication] = [
            SimpleNamespace(
                id=uuid.UUID(int=39),
                object_id=raster.id,
                gate_revision=1,
                created_at=comment.created_at,
            )
        ]
        task_original = SimpleNamespace(
            id=uuid.UUID(int=41),
            creation_id=uuid.UUID(int=41),
            task_id=uuid.UUID(int=42),
            project_id=project_id,
            location_id=LEGACY_LOCATION_ID,
            storage_identity=identity(self.endpoint, self.source["MINIO_BUCKET"]),
            bucket=self.source["MINIO_BUCKET"],
            object_key=f"__aap_task_originals__/{uuid.UUID(int=42)}/{uuid.UUID(int=41)}/payload",
            sha256=hashlib.sha256(b"original").hexdigest(),
            size_bytes=8,
            etag='"source-etag"',
            version_id="source-version",
            created_at=comment.created_at,
        )
        self.rows[TaskOriginal] = [task_original]
        from worktree_recovery_fixture import auxiliary_contract, auxiliary_content

        self.rows[DatasetAuxiliaryOriginal] = [
            SimpleNamespace(
                id=uuid.UUID(int=identifier),
                creation_id=uuid.UUID(int=identifier),
                dataset_id=uuid.UUID(int=45),
                kind=kind,
                location_id=LEGACY_LOCATION_ID,
                storage_identity=identity(
                    self.endpoint, self.source["MINIO_DATASETS_BUCKET"]
                ),
                bucket=self.source["MINIO_DATASETS_BUCKET"],
                object_key=f"__aap_auxiliary_originals__/{uuid.UUID(int=45)}/{uuid.UUID(int=identifier)}/payload",
                sha256=hashlib.sha256(auxiliary_content(kind)).hexdigest(),
                size_bytes=len(auxiliary_content(kind)),
                etag='"source-etag"',
                version_id="source-version",
                created_at=comment.created_at,
                source_contract=auxiliary_contract(kind),
            )
            for identifier, kind in ((43, "raw_lidar"), (44, "map"))
        ]
        self.rows[ManagedOriginalCreation] = [
            SimpleNamespace(
                id=task_original.creation_id,
                owner_kind="task",
                dataset_id=None,
                project_id=project_id,
                task_id=task_original.task_id,
                bucket=self.source["MINIO_BUCKET"],
                object_key=task_original.object_key,
                configuration={"endpoint": self.endpoint, "buckets": self.source},
                receipt={
                    "ETag": task_original.etag,
                    "VersionId": task_original.version_id,
                },
                status="verified",
            )
        ]
        self.db = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    kind="s3",
                    config_revision=1,
                    config={"endpoint": "127.0.0.1:9000", "use_ssl": False},
                )
            ),
            scalars=AsyncMock(
                side_effect=lambda statement: self.rows.get(
                    statement.column_descriptions[0]["entity"], []
                )
            ),
            flush=AsyncMock(),
            execute=AsyncMock(),
        )

    async def test_every_durable_physical_owner_is_relocated(self):
        from app.services import original_cache, prediction_inputs

        references = deepcopy(self.rows[self.models.guide_reference])
        publications = deepcopy(self.rows[self.models.raster_publication])
        creations = deepcopy(self.rows[self.models.creation])
        ledger = await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(self.rows[self.models.guide_reference], references)
        self.assertEqual(self.rows[self.models.raster_publication], publications)
        self.assertEqual(self.rows[self.models.creation], creations)
        self.assertEqual(
            {entry["table"] for entry in ledger},
            {
                "storage_bindings",
                "dataset_asset_locations",
                "dataset_source_previews",
                "original_cache_entries",
                "prediction_input_leases",
                "training_storage_deletions",
                "managed_original_retirements",
                "training_dataset_versions",
                "training_runs",
                "training_previews",
                "sample_evaluation_runs",
                "active_learning_rounds",
                "avatar_uploads",
                "export_outputs",
                "bug_attachment_uploads",
                "comment_attachment_uploads",
                "guide_asset_uploads",
                "raster_mask_objects",
                "task_originals",
                "dataset_auxiliary_originals",
                "audit_archive_outputs",
            },
        )
        target = storage_view(self.endpoint, self.target)
        self.assertEqual(
            self.rows[self.models.cache][0].storage_identity,
            original_cache.storage_identity(target),
        )
        self.assertEqual(
            self.rows[self.models.input][0].storage_identity,
            prediction_inputs.storage_identity(target),
        )
        for rows in self.rows.values():
            for row in rows:
                if row in self.rows[self.models.creation]:
                    self.assertEqual(row.bucket, self.source["MINIO_BUCKET"])
                    continue
                if hasattr(row, "bucket"):
                    self.assertIn(row.bucket, self.target.values())
                if getattr(row, "storage_ref", None) is not None:
                    if row.storage_ref["purpose"] == "audit-archive":
                        self.assertEqual(
                            row.storage_ref["bucket"],
                            self.target["MINIO_AUDIT_ARCHIVE_BUCKET"],
                        )
                        self.assertEqual(row.state, "ready")
                        self.assertEqual(row.parent_oid, 123)
                        self.assertEqual(row.source_oid, 456)
                        self.assertEqual(row.source_sha256, "e" * 64)
                        self.assertEqual(row.published_at, row.created_at)
                        continue
                    if row.storage_ref["purpose"] == "annotations":
                        self.assertEqual(
                            row.storage_ref["bucket"], self.target["MINIO_BUCKET"]
                        )
                        if row is self.rows[self.models.guide_asset][0]:
                            self.assertEqual(row.id, uuid.UUID(int=34))
                            self.assertEqual(row.owner_id, uuid.UUID(int=35))
                            self.assertEqual(row.project_id, uuid.UUID(int=12))
                            self.assertEqual(row.mime_type, "image/svg+xml")
                            self.assertEqual(row.size_bytes, 6)
                            self.assertEqual(row.state, "ready")
                            continue
                        if row is self.rows[self.models.raster_object][0]:
                            self.assertEqual(row.id, uuid.UUID(int=38))
                            self.assertEqual(row.storage_encoding, "identity")
                            self.assertEqual(row.state, "ready")
                            self.assertEqual(row.stored_sha256, row.canonical_sha256)
                            self.assertEqual(row.published_at, row.created_at)
                            self.assertEqual(row.last_used_at, row.created_at)
                            continue
                        self.assertEqual(row.bound_comment_id, uuid.UUID(int=33))
                        self.assertEqual(row.annotation_id, uuid.UUID(int=31))
                        self.assertEqual(
                            row.expires_at - row.created_at, timedelta(hours=2160)
                        )
                        self.assertEqual(row.size_bytes, 0)
                        continue
                    if row.storage_ref["purpose"] == "bug-reports":
                        self.assertEqual(
                            row.storage_ref["bucket"],
                            self.target["MINIO_BUG_REPORTS_BUCKET"],
                        )
                        self.assertEqual(row.state, "ready")
                        self.assertEqual(row.bound_report_id, uuid.UUID(int=25))
                        self.assertEqual(row.sha256, "d" * 64)
                        self.assertEqual(
                            row.expires_at - row.created_at, timedelta(days=180)
                        )
                        continue
                    if row.storage_ref["purpose"] == "export":
                        self.assertEqual(
                            row.storage_ref["bucket"],
                            self.target["MINIO_EXPORT_BUCKET"],
                        )
                        self.assertEqual(row.state, "pending")
                        self.assertEqual(row.sha256, "c" * 64)
                        self.assertEqual(
                            row.expires_at - row.created_at, timedelta(days=7)
                        )
                        continue
                    if row.storage_ref["purpose"] == "avatars":
                        self.assertEqual(
                            row.storage_ref["bucket"],
                            self.target["MINIO_AVATARS_BUCKET"],
                        )
                        self.assertEqual(row.state, "retired")
                        self.assertEqual(row.sha256, "b" * 64)
                        continue
                    self.assertEqual(
                        row.storage_ref["bucket"], self.target["MINIO_TRAINING_BUCKET"]
                    )
                    self.assertEqual(
                        row.storage_ref["evidence"], {"inventory_digest": "a" * 64}
                    )
        self.db.flush.assert_awaited_once()
        retirement = self.rows[self.models.retirement][0]
        self.assertEqual(
            retirement.storage_identity,
            identity(self.endpoint, self.target["MINIO_DATASETS_BUCKET"]),
        )
        self.assertEqual(retirement.assertion_digest, "immutable-assertion")
        self.assertEqual(retirement.dataset_id, "historical-dataset")
        self.assertIsNone(retirement.retain_until)

    async def test_unknown_training_owner_is_not_adopted_or_mapped(self):
        baseline = deepcopy(self.rows)
        for model in [
            self.models.version,
            self.models.run,
            self.models.deletion,
            self.models.training_preview,
            self.models.evaluation,
            self.models.learning,
        ]:
            for index in range(len(baseline[model])):
                for field, value in [
                    ("bucket", "foreign"),
                    ("endpoint_url", "http://other:9000"),
                    ("storage_identity", "b" * 64),
                    ("prefix", "unknown/"),
                ]:
                    self.rows = deepcopy(baseline)
                    self.rows[model][index].storage_ref[field] = value
                    with self.subTest(
                        model=model.__tablename__, index=index, field=field
                    ):
                        with self.assertRaises(WorktreeError):
                            await remap_storage(
                                self.db, self.source, self.target, self.endpoint
                            )
        self.db.flush.assert_not_awaited()

    async def test_run_and_dataset_ownership_cannot_be_interchanged(self):
        baseline = deepcopy(self.rows)
        for model, foreign in [
            (self.models.version, self.models.run),
            (self.models.run, self.models.version),
        ]:
            self.rows = deepcopy(baseline)
            self.rows[model][0].storage_ref = deepcopy(baseline[foreign][0].storage_ref)
            with (
                self.subTest(model=model.__tablename__),
                self.assertRaises(WorktreeError),
            ):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.db.flush.assert_not_awaited()

    async def test_unknown_legacy_run_remains_unpinned_during_restore(self):
        run = self.rows[self.models.run][0]
        run.storage_ref = None
        await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertIsNone(run.storage_ref)

    async def test_unknown_avatar_ref_or_receipt_refuses_the_entire_mapping(self):
        baseline = deepcopy(self.rows)
        for field, value in [
            ("purpose", "training-run"),
            ("bucket", self.source["MINIO_IMPORT_BUCKET"]),
            ("object_key", "b" * 32 + ".webp"),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
            ("extra", "unexpected"),
        ]:
            self.rows = deepcopy(baseline)
            self.rows[self.models.avatar][0].storage_ref[field] = value
            with self.subTest(field=field):
                with self.assertRaises(WorktreeError):
                    await remap_storage(
                        self.db, self.source, self.target, self.endpoint
                    )
                self.db.flush.assert_not_awaited()
                self.assertEqual(
                    self.rows[self.models.binding][0].bucket,
                    baseline[self.models.binding][0].bucket,
                )
        self.rows = deepcopy(baseline)
        self.rows[self.models.avatar][0].sha256 = "not-a-receipt"
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.db.flush.assert_not_awaited()

    async def test_avatar_version_receipt_refuses_unsupported_mapping_before_writes(
        self,
    ):
        baseline = deepcopy(self.rows)
        self.rows[self.models.avatar][0].object_version_id = "saved-source-version"
        with self.assertRaisesRegex(WorktreeError, "固定版本回执"):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.db.flush.assert_not_awaited()
        self.assertEqual(
            self.rows[self.models.binding][0].bucket,
            baseline[self.models.binding][0].bucket,
        )

    def test_protected_avatar_row_preserves_all_nonphysical_facts(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.avatar][0]))
        base = digest(protected_row("avatar_uploads", row))
        for field in ["bucket", "storage_identity"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(base, digest(protected_row("avatar_uploads", changed)))
        for field in ["token", "owner_id", "sha256", "size_bytes", "state"]:
            changed = {**row, field: "changed"}
            self.assertNotEqual(base, digest(protected_row("avatar_uploads", changed)))
        for field in ["object_key", "purpose", "location_id", "endpoint_url"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(base, digest(protected_row("avatar_uploads", changed)))

    async def test_unknown_export_owner_refuses_without_partially_mapping_others(self):
        original = deepcopy(self.rows)
        for field, value in [
            ("purpose", "avatars"),
            ("bucket", self.source["MINIO_TRAINING_BUCKET"]),
            ("object_key", "image/foreign.zip"),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
        ]:
            self.rows = deepcopy(original)
            self.rows[self.models.output][0].storage_ref[field] = value
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.assertEqual(
                self.rows[self.models.binding][0].bucket,
                original[self.models.binding][0].bucket,
            )

    def test_protected_export_output_keeps_receipt_deadline_and_source_identity(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.output][0]))
        base = digest(protected_row("export_outputs", row))
        for field in ["bucket", "storage_identity"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(base, digest(protected_row("export_outputs", changed)))
        for field in [
            "id",
            "project_id",
            "job_id",
            "sha256",
            "size_bytes",
            "file_count",
            "state",
            "created_at",
            "expires_at",
        ]:
            self.assertNotEqual(
                base, digest(protected_row("export_outputs", {**row, field: "changed"}))
            )
        for field in ["object_key", "purpose", "location_id", "endpoint_url"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(base, digest(protected_row("export_outputs", changed)))

    async def media_fixture(self):
        from app.db.models.export_artifact import ExportArtifact
        from app.db.models.export_media_bundle import ExportMediaBundle
        from app.services import export_media_bundles as bundles
        from tests.test_export_media_bundles import FakeDB
        from worktree_recovery_export import MEDIA_CACHE_KEY, media_bytes, media_inputs

        endpoint = patch(
            "app.services.export_media_storage._runtime_endpoint",
            return_value=self.endpoint,
        )
        endpoint.start()
        self.addCleanup(endpoint.stop)
        project_id = self.rows[self.models.output][0].project_id
        sources = {
            role: {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "media_type": "point_cloud" if role == "primary_lidar" else "image",
                "relation": None,
            }
            for role, raw in media_bytes().items()
        }
        frozen, logical = media_inputs(
            project_id, sources, self.source["MINIO_EXPORT_BUCKET"]
        )
        digest = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        fake = FakeDB()
        with patch.object(
            bundles.settings, "minio_export_bucket", self.source["MINIO_EXPORT_BUCKET"]
        ):
            allocated = await bundles.allocate(
                fake,
                project_id=project_id,
                job_id=uuid.uuid4(),
                input_snapshot=frozen,
                input_digest=digest,
                logical_sources=logical,
            )
        row = fake.rows[allocated.id]
        row.lease_token, row.lease_expires_at = (
            uuid.uuid4(),
            row.created_at + timedelta(hours=1),
        )
        row.state, row.published_at = "ready", row.created_at
        output = deepcopy(self.rows[self.models.output][0])
        output.id, output.job_id, output.media_bundle_id = (
            row.zip_output_id,
            row.job_id,
            row.id,
        )
        output.state, output.format = "ready", "pointmask"
        output.created_at, output.expires_at = row.created_at, row.expires_at
        output.storage_ref["object_key"] = (
            f"image/{project_id}/{row.job_id}/{output.id}.zip"
        )
        self.rows[self.models.output].append(output)
        self.rows[ExportMediaBundle] = [row]
        cache = SimpleNamespace(
            id=uuid.uuid4(),
            cache_key=MEDIA_CACHE_KEY,
            project_id=project_id,
            batch_id=None,
            output_id=output.id,
            format=output.format,
            object_key=output.storage_ref["object_key"],
            file_count=output.file_count,
            size_bytes=output.size_bytes,
            expires_at=output.expires_at,
        )
        self.rows[ExportArtifact] = [cache]
        self.db.delete = AsyncMock(
            side_effect=lambda value: self.rows[ExportArtifact].remove(value)
        )
        return row, output, cache

    async def test_media_mapping_changes_only_physical_ref_and_invalidates_typed_cache(
        self,
    ):
        row, output, cache = await self.media_fixture()
        before = {
            column.key: deepcopy(getattr(row, column.key))
            for column in type(row).__mapper__.column_attrs
        }
        output_before = deepcopy(vars(output))
        ledger = await remap_storage(self.db, self.source, self.target, self.endpoint)
        for field, value in before.items():
            if field != "storage_ref":
                self.assertEqual(getattr(row, field), value, field)
        self.assertEqual(
            row.storage_ref,
            {
                **before["storage_ref"],
                "bucket": self.target["MINIO_EXPORT_BUCKET"],
                "storage_identity": identity(
                    self.endpoint, self.target["MINIO_EXPORT_BUCKET"]
                ),
            },
        )
        self.assertEqual(output.sha256, output_before["sha256"])
        self.assertEqual(output.expires_at, row.expires_at)
        self.db.delete.assert_awaited_once_with(cache)
        self.assertTrue(
            any(entry["table"] == "export_media_bundles" for entry in ledger)
        )
        invalidated = [
            entry for entry in ledger if entry["table"] == "export_artifacts"
        ]
        self.assertEqual(len(invalidated), 1)
        self.assertEqual(invalidated[0]["reason"], "immutable_embedded_media_urls")
        self.assertIsNone(invalidated[0]["after"])

    async def test_invalid_media_owner_pair_or_cache_refuses_before_any_mapping(self):
        row, output, cache = await self.media_fixture()
        for subject, field, value in [
            (row.storage_ref, "purpose", "datasets"),
            (row.storage_ref, "bucket", self.source["MINIO_TRAINING_BUCKET"]),
            (row.storage_ref, "key_prefix", "lidar-export-media/objects/foreign/"),
            (row.storage_ref, "storage_identity", "f" * 64),
            (output, "expires_at", output.expires_at + timedelta(seconds=1)),
            (output, "media_bundle_id", uuid.uuid4()),
            (cache, "size_bytes", cache.size_bytes + 1),
            (cache, "format", "foreign"),
            (cache, "object_key", "foreign.zip"),
            (cache, "project_id", uuid.uuid4()),
        ]:
            original = (
                subject[field] if isinstance(subject, dict) else getattr(subject, field)
            )
            if isinstance(subject, dict):
                subject[field] = value
            else:
                setattr(subject, field, value)
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.db.delete.assert_not_awaited()
            self.assertEqual(
                self.rows[self.models.binding][0].bucket, self.source["MINIO_BUCKET"]
            )
            if isinstance(subject, dict):
                subject[field] = original
            else:
                setattr(subject, field, original)

    async def test_protected_media_excludes_only_bucket_and_storage_identity(self):
        from worktree_recovery_fixture import digest, protected_row

        owner, _, _ = await self.media_fixture()
        row = {
            column.key: deepcopy(getattr(owner, column.key))
            for column in type(owner).__mapper__.column_attrs
        }
        base = digest(protected_row("export_media_bundles", row))
        for field in ("bucket", "storage_identity"):
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("export_media_bundles", changed))
            )
        for field in (
            "id",
            "project_id",
            "job_id",
            "zip_output_id",
            "input_snapshot",
            "input_digest",
            "logical_paths",
            "inventory",
            "object_count",
            "size_bytes",
            "state",
            "lease_token",
            "lease_expires_at",
            "created_at",
            "expires_at",
            "published_at",
            "retired_at",
            "last_attempted_at",
        ):
            self.assertNotEqual(
                base,
                digest(
                    protected_row("export_media_bundles", {**row, field: "changed"})
                ),
                field,
            )
        for field in (
            "purpose",
            "location_id",
            "config_revision",
            "binding_generation",
            "endpoint_url",
            "key_prefix",
        ):
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(
                base, digest(protected_row("export_media_bundles", changed)), field
            )

    async def test_bug_attachment_new_schema_or_version_refuses_before_mapping(self):
        for kind in ("schema", "version"):
            row = self.rows[self.models.bug_attachment][0]
            saved = deepcopy(vars(row))
            if kind == "schema":
                row.storage_ref = {**row.storage_ref, "schema": 2}
            else:
                row.object_version_id = "actual-provider-version"
            with self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.assertTrue(
                all(
                    row.bucket
                    == self.source[
                        {
                            "annotations": "MINIO_BUCKET",
                            "datasets": "MINIO_DATASETS_BUCKET",
                            "bug-reports": "MINIO_BUG_REPORTS_BUCKET",
                            "media-cache": "MINIO_MEDIA_CACHE_BUCKET",
                            "audit-archive": "MINIO_AUDIT_ARCHIVE_BUCKET",
                            "import": "MINIO_IMPORT_BUCKET",
                            "export": "MINIO_EXPORT_BUCKET",
                            "avatars": "MINIO_AVATARS_BUCKET",
                            "training": "MINIO_TRAINING_BUCKET",
                        }[row.purpose]
                    ]
                    for row in self.rows[self.models.binding]
                )
            )
            vars(row).clear()
            vars(row).update(saved)

    async def test_unknown_bug_attachment_refuses_without_partially_mapping_others(
        self,
    ):
        original = deepcopy(self.rows)
        for field, value in [
            ("purpose", "avatars"),
            ("bucket", self.source["MINIO_EXPORT_BUCKET"]),
            ("object_key", "bug-report-attachments/foreign.png"),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
        ]:
            self.rows = deepcopy(original)
            self.rows[self.models.bug_attachment][0].storage_ref[field] = value
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.assertEqual(
                self.rows[self.models.binding][0].bucket,
                original[self.models.binding][0].bucket,
            )

    def test_protected_bug_attachment_keeps_actor_binding_receipt_deadline(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.bug_attachment][0]))
        base = digest(protected_row("bug_attachment_uploads", row))
        for field in ["bucket", "storage_identity"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("bug_attachment_uploads", changed))
            )
        for field in [
            "id",
            "owner_id",
            "bound_report_id",
            "file_name",
            "mime_type",
            "sha256",
            "size_bytes",
            "state",
            "created_at",
            "expires_at",
        ]:
            self.assertNotEqual(
                base,
                digest(
                    protected_row("bug_attachment_uploads", {**row, field: "changed"})
                ),
            )
        for field in [
            "object_key",
            "purpose",
            "location_id",
            "endpoint_url",
            "config_revision",
            "binding_generation",
        ]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(
                base, digest(protected_row("bug_attachment_uploads", changed))
            )

    async def test_unknown_comment_attachment_refuses_without_partial_mapping(self):
        original = deepcopy(self.rows)
        for field, value in [
            ("purpose", "avatars"),
            ("bucket", self.source["MINIO_EXPORT_BUCKET"]),
            ("object_key", "comment-attachments/foreign/unknown"),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
        ]:
            self.rows = deepcopy(original)
            self.rows[self.models.comment_attachment][0].storage_ref[field] = value
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.assertEqual(
                self.rows[self.models.binding][0].bucket,
                original[self.models.binding][0].bucket,
            )

    def test_protected_comment_attachment_keeps_context_binding_and_deadline(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.comment_attachment][0]))
        base = digest(protected_row("comment_attachment_uploads", row))
        for field in ["bucket", "storage_identity"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("comment_attachment_uploads", changed))
            )
        for field in [
            "id",
            "owner_id",
            "annotation_id",
            "task_id",
            "project_id",
            "bound_comment_id",
            "file_name",
            "mime_type",
            "sha256",
            "size_bytes",
            "state",
            "created_at",
            "expires_at",
        ]:
            self.assertNotEqual(
                base,
                digest(
                    protected_row(
                        "comment_attachment_uploads", {**row, field: "changed"}
                    )
                ),
                field,
            )
        for field in [
            "object_key",
            "purpose",
            "location_id",
            "endpoint_url",
            "config_revision",
            "binding_generation",
        ]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(
                base,
                digest(protected_row("comment_attachment_uploads", changed)),
                field,
            )

    async def test_unknown_guide_asset_refuses_without_any_partial_mutation(self):
        from worktree_recovery_fixture import digest

        original = deepcopy(self.rows)
        mutations = [
            ("schema", True),
            ("purpose", "avatars"),
            ("bucket", self.source["MINIO_EXPORT_BUCKET"]),
            ("object_key", "projects/foreign/guide/unknown"),
            (
                "object_key",
                f"comment-attachments/{uuid.UUID(int=12)}/{uuid.UUID(int=34)}",
            ),
            (
                "object_key",
                f"projects/{uuid.UUID(int=36)}/guide/{uuid.UUID(int=34)}",
            ),
            (
                "object_key",
                f"projects/{uuid.UUID(int=12)}/guide/{uuid.UUID(int=36)}",
            ),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
            ("location_id", str(uuid.UUID(int=37))),
            ("config_revision", 2),
            ("binding_generation", 2),
            ("unknown", "not-a-locator-fact"),
        ]
        for field, value in mutations:
            self.rows = deepcopy(original)
            self.rows[self.models.guide_asset][0].storage_ref[field] = value
            before = digest(
                {
                    model.__tablename__: [vars(row) for row in rows]
                    for model, rows in self.rows.items()
                }
            )
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(WorktreeError),
            ):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            after = digest(
                {
                    model.__tablename__: [vars(row) for row in rows]
                    for model, rows in self.rows.items()
                }
            )
            self.assertEqual(before, after)
            self.db.flush.assert_not_awaited()
            self.db.execute.assert_not_awaited()

    def test_protected_guide_owner_allows_only_bucket_and_identity_changes(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.guide_asset][0]))
        base = digest(protected_row("guide_asset_uploads", row))
        for field in ("bucket", "storage_identity"):
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("guide_asset_uploads", changed))
            )
        for field in row.keys() - {"storage_ref"}:
            with self.subTest(field=field):
                self.assertNotEqual(
                    base,
                    digest(
                        protected_row("guide_asset_uploads", {**row, field: "changed"})
                    ),
                )
        for field in row["storage_ref"].keys() - {"bucket", "storage_identity"}:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            with self.subTest(ref_field=field):
                self.assertNotEqual(
                    base, digest(protected_row("guide_asset_uploads", changed))
                )
        changed = deepcopy(row)
        changed["storage_ref"]["unknown"] = "extra fact"
        self.assertNotEqual(base, digest(protected_row("guide_asset_uploads", changed)))

    def test_guide_refs_and_project_revision_are_entirely_protected(self):
        from worktree_recovery_fixture import digest, protected_row
        from app.services import guide_asset_storage

        guide = self.rows[self.models.guide_asset][0]
        rows = {
            "project_guide_assets": vars(self.rows[self.models.guide_reference][0]),
            "projects": {
                "id": guide.project_id,
                "owner_id": guide.owner_id,
                "guide_assets": [guide_asset_storage.entry(guide)],
                "guide_assets_revision": 2,
                "annotation_guide": "![shared](guide image)",
            },
        }
        for table, row in rows.items():
            self.assertEqual(protected_row(table, row), row)
            for field in row:
                with self.subTest(table=table, field=field):
                    self.assertNotEqual(
                        digest(protected_row(table, row)),
                        digest(protected_row(table, {**row, field: "changed"})),
                    )
        self.assertNotEqual(
            digest(protected_row("project_guide_assets", rows["project_guide_assets"])),
            digest([]),
        )

    async def test_invalid_task_original_refuses_before_any_mapping_mutation(self):
        from worktree_recovery_fixture import digest

        original = deepcopy(self.rows)
        mutations = (
            ("bucket", self.source["MINIO_DATASETS_BUCKET"]),
            ("bucket", "outside-backup"),
            ("storage_identity", "f" * 64),
            ("location_id", uuid.UUID(int=99)),
            ("creation_id", uuid.UUID(int=99)),
            ("object_key", "recovery/legacy-task.png"),
            ("task_id", uuid.UUID(int=99)),
            ("sha256", "invalid"),
            ("size_bytes", True),
            ("etag", ""),
            ("version_id", "null"),
        )
        for field, value in mutations:
            self.rows = deepcopy(original)
            setattr(self.rows[self.models.task_original][0], field, value)
            before = digest(
                {
                    model.__tablename__: [vars(row) for row in rows]
                    for model, rows in self.rows.items()
                }
            )
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.assertEqual(
                before,
                digest(
                    {
                        model.__tablename__: [vars(row) for row in rows]
                        for model, rows in self.rows.items()
                    }
                ),
            )
            self.db.execute.assert_not_awaited()
            self.db.flush.assert_not_awaited()

    async def test_task_original_mapping_keeps_an_older_historical_creation_location(
        self,
    ):
        creation = self.rows[self.models.creation][0]
        creation.bucket = "historical-annotations-before-an-earlier-restore"
        before = deepcopy(creation)
        ledger = await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(creation, before)
        owner = self.rows[self.models.task_original][0]
        self.assertEqual(owner.bucket, self.target["MINIO_BUCKET"])
        self.assertEqual(owner.storage_identity, identity(self.endpoint, owner.bucket))
        self.assertEqual(
            {entry["field"] for entry in ledger if entry["table"] == "task_originals"},
            {"bucket", "storage_identity"},
        )

    async def test_auxiliary_owners_map_only_current_locators(self):
        from worktree_recovery_fixture import protected_row

        owners = self.rows[self.models.auxiliary_original]
        originals = deepcopy(owners)
        for state in ("allocated", "verified", "unknown"):
            self.rows[self.models.creation].append(
                SimpleNamespace(
                    id=uuid.uuid4(),
                    owner_kind="dataset_auxiliary",
                    status=state,
                    bucket="historical-datasets-before-an-earlier-restore",
                    configuration={"source": "immutable"},
                    authority={
                        "auxiliary": {"source_contract": {"token": "historical"}}
                    },
                    operation_id=uuid.uuid4() if state == "unknown" else None,
                )
            )
        history = deepcopy(self.rows[self.models.creation])
        ledger = await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(self.rows[self.models.creation], history)
        for before, after in zip(originals, owners, strict=True):
            self.assertEqual(after.bucket, self.target["MINIO_DATASETS_BUCKET"])
            self.assertEqual(
                after.storage_identity, identity(self.endpoint, after.bucket)
            )
            self.assertEqual(
                protected_row("dataset_auxiliary_originals", vars(before)),
                protected_row("dataset_auxiliary_originals", vars(after)),
            )
        entries = [
            entry for entry in ledger if entry["table"] == "dataset_auxiliary_originals"
        ]
        self.assertEqual(len(entries), 4)
        self.assertEqual(
            {entry["field"] for entry in entries}, {"bucket", "storage_identity"}
        )

    async def test_invalid_auxiliary_owner_refuses_before_any_mapping_mutation(self):
        original = deepcopy(self.rows)
        mutations = (
            ("bucket", self.source["MINIO_BUCKET"]),
            ("bucket", "outside-backup"),
            ("storage_identity", "f" * 64),
            ("location_id", uuid.UUID(int=99)),
            ("creation_id", uuid.UUID(int=99)),
            ("object_key", "recovery/legacy-auxiliary.bin"),
            ("dataset_id", uuid.UUID(int=99)),
            ("kind", "image"),
            ("sha256", "invalid"),
            ("size_bytes", True),
            ("etag", ""),
            ("version_id", "null"),
            ("source_contract", {"arbitrary": "metadata"}),
        )
        for field, value in mutations:
            self.rows = deepcopy(original)
            setattr(self.rows[self.models.auxiliary_original][0], field, value)
            before = deepcopy(self.rows)
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.assertEqual(self.rows, before)
            self.db.execute.assert_not_awaited()
            self.db.flush.assert_not_awaited()

    def test_auxiliary_mapping_protects_contract_pointers_and_creation_history(self):
        from worktree_recovery_fixture import digest, protected_row

        row = vars(self.rows[self.models.auxiliary_original][0])
        before = digest(protected_row("dataset_auxiliary_originals", row))
        for field in row:
            after = digest(
                protected_row("dataset_auxiliary_originals", {**row, field: "changed"})
            )
            with self.subTest(field=field):
                self.assertEqual(
                    before == after, field in {"bucket", "storage_identity"}
                )
        for table, row in (
            ("dataset_items", {"id": "item", "nuscenes_raw_original_id": "raw"}),
            (
                "scenes",
                {
                    "id": "scene",
                    "nuscenes_map_original_id": "map",
                    "source_metadata": {"contract": "fixed"},
                },
            ),
            (
                "managed_original_creations",
                {
                    "owner_kind": "dataset_auxiliary",
                    "bucket": "source",
                    "status": "unknown",
                    "operation_id": "unsettled",
                },
            ),
        ):
            self.assertEqual(protected_row(table, row), row)
            for field in row:
                self.assertNotEqual(
                    digest(protected_row(table, row)),
                    digest(protected_row(table, {**row, field: "changed"})),
                )

    async def test_auxiliary_wrong_purpose_with_valid_identity_is_not_remapped(self):
        row = self.rows[self.models.auxiliary_original][0]
        row.bucket = self.source["MINIO_BUCKET"]
        row.storage_identity = identity(self.endpoint, row.bucket)
        before = deepcopy(self.rows)
        with self.assertRaisesRegex(WorktreeError, "datasets"):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(self.rows, before)
        self.db.execute.assert_not_awaited()

    async def test_task_original_wrong_purpose_with_valid_identity_is_not_remapped(
        self,
    ):
        row = self.rows[self.models.task_original][0]
        row.bucket = self.source["MINIO_DATASETS_BUCKET"]
        row.storage_identity = identity(self.endpoint, row.bucket)
        before = deepcopy(self.rows)
        with self.assertRaisesRegex(WorktreeError, "annotations"):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(self.rows, before)
        self.db.execute.assert_not_awaited()
        self.db.flush.assert_not_awaited()

    def test_task_original_mapping_protects_receipt_and_all_historical_creation_facts(
        self,
    ):
        from worktree_recovery_fixture import digest, protected_row

        row = vars(self.rows[self.models.task_original][0])
        before = digest(protected_row("task_originals", row))
        for field in row:
            changed = {**row, field: "changed"}
            after = digest(protected_row("task_originals", changed))
            with self.subTest(field=field):
                if field in {"bucket", "storage_identity"}:
                    self.assertEqual(before, after)
                else:
                    self.assertNotEqual(before, after)
        creation = vars(self.rows[self.models.creation][0])
        self.assertEqual(
            protected_row("managed_original_creations", creation), creation
        )
        for field in creation:
            self.assertNotEqual(
                digest(protected_row("managed_original_creations", creation)),
                digest(
                    protected_row(
                        "managed_original_creations", {**creation, field: "changed"}
                    )
                ),
            )
        task = {
            "id": str(row["task_id"]),
            "original_id": str(row["id"]),
            "file_path": row["object_key"],
        }
        self.assertEqual(protected_row("tasks", task), task)

    async def test_unknown_raster_object_refuses_without_partial_mapping(self):
        from worktree_recovery_fixture import digest

        original = deepcopy(self.rows)
        mutations = [
            ("schema", True),
            ("purpose", "avatars"),
            ("bucket", self.source["MINIO_EXPORT_BUCKET"]),
            ("bucket", "outside-backup"),
            ("object_key", "raster-masks/sha256/old.json"),
            ("object_key", f"raster-masks/objects/{uuid.UUID(int=40)}.json"),
            ("storage_identity", "f" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
            ("location_id", str(uuid.UUID(int=40))),
            ("config_revision", 2),
            ("binding_generation", 2),
            ("unknown", "unrecognized-fact"),
        ]
        for field, value in mutations:
            self.rows = deepcopy(original)
            self.rows[self.models.raster_object][0].storage_ref[field] = value
            before = digest(
                {
                    model.__tablename__: [vars(row) for row in rows]
                    for model, rows in self.rows.items()
                }
            )
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            after = digest(
                {
                    model.__tablename__: [vars(row) for row in rows]
                    for model, rows in self.rows.items()
                }
            )
            self.assertEqual(before, after)
            self.db.execute.assert_not_awaited()
            self.db.flush.assert_not_awaited()

    def test_raster_owner_protects_both_receipts_generation_and_times(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.raster_object][0]))
        base = digest(protected_row("raster_mask_objects", row))
        for field in ("bucket", "storage_identity"):
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("raster_mask_objects", changed))
            )
        for field in row.keys() - {"storage_ref"}:
            with self.subTest(field=field):
                self.assertNotEqual(
                    base,
                    digest(
                        protected_row("raster_mask_objects", {**row, field: "changed"})
                    ),
                )
        for field in row["storage_ref"].keys() - {"bucket", "storage_identity"}:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            with self.subTest(ref_field=field):
                self.assertNotEqual(
                    base, digest(protected_row("raster_mask_objects", changed))
                )
        changed = deepcopy(row)
        changed["storage_ref"]["unknown"] = "extra fact"
        self.assertNotEqual(base, digest(protected_row("raster_mask_objects", changed)))

    def test_raster_publication_and_portable_references_are_fully_protected(self):
        from app.services import raster_object_storage
        from worktree_recovery_fixture import digest, protected_row

        reference = raster_object_storage.reference(
            self.rows[self.models.raster_object][0]
        )
        rows = {
            "raster_mask_publications": vars(
                self.rows[self.models.raster_publication][0]
            ),
            "annotations": {
                "id": "annotation",
                "geometry": {"type": "raster_mask", "mask": reference},
            },
            "training_dataset_versions": {
                "id": "version",
                "snapshot": {"mask": reference},
            },
        }
        for table, row in rows.items():
            self.assertEqual(protected_row(table, row), row)
            for field in row:
                with self.subTest(table=table, field=field):
                    self.assertNotEqual(
                        digest(protected_row(table, row)),
                        digest(protected_row(table, {**row, field: "changed"})),
                    )

    async def test_unknown_audit_archive_refuses_without_partial_mapping(self):
        original = deepcopy(self.rows)
        for field, value in [
            ("purpose", "export"),
            ("bucket", self.source["MINIO_EXPORT_BUCKET"]),
            ("object_key", "2002/01.jsonl.gz"),
            ("storage_identity", "a" * 64),
            ("endpoint_url", "https://elsewhere.invalid"),
            ("extra", "unknown"),
        ]:
            self.rows = deepcopy(original)
            self.rows[self.models.archive][0].storage_ref[field] = value
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.db.flush.assert_not_awaited()
            self.assertEqual(
                self.rows[self.models.binding][0].bucket,
                original[self.models.binding][0].bucket,
            )

    def test_protected_archive_preserves_source_receipt_and_publication_facts(self):
        from worktree_recovery_fixture import digest, protected_row

        row = deepcopy(vars(self.rows[self.models.archive][0]))
        base = digest(protected_row("audit_archive_outputs", row))
        for field in ["bucket", "storage_identity"]:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "mapped"
            self.assertEqual(
                base, digest(protected_row("audit_archive_outputs", changed))
            )
        for field in set(row) - {"storage_ref"}:
            self.assertNotEqual(
                base,
                digest(
                    protected_row("audit_archive_outputs", {**row, field: "changed"})
                ),
                field,
            )
        for field in set(row["storage_ref"]) - {"bucket", "storage_identity"}:
            changed = deepcopy(row)
            changed["storage_ref"][field] = "changed"
            self.assertNotEqual(
                base, digest(protected_row("audit_archive_outputs", changed)), field
            )

    def test_protected_training_ref_excludes_only_physical_subfields(self):
        from worktree_recovery_fixture import digest, protected_row

        for model in [
            self.models.version,
            self.models.run,
            self.models.deletion,
            self.models.training_preview,
            self.models.evaluation,
            self.models.learning,
        ]:
            table = model.__tablename__
            for row in self.rows[model]:
                original = {"id": str(row.id), "storage_ref": row.storage_ref}
                base = protected_row(table, original)
                for field in ["bucket", "storage_identity"]:
                    changed = {
                        **original,
                        "storage_ref": {**original["storage_ref"], field: "mapped"},
                    }
                    self.assertEqual(
                        digest(base), digest(protected_row(table, changed))
                    )
                for field in [
                    "prefix",
                    "origin",
                    "evidence",
                    "location_id",
                    "endpoint_url",
                ]:
                    changed = {
                        **original,
                        "storage_ref": {**original["storage_ref"], field: "changed"},
                    }
                    self.assertNotEqual(
                        digest(base), digest(protected_row(table, changed))
                    )

    async def test_unknown_retired_original_cannot_be_adopted_on_restore(self):
        retirement = self.rows[self.models.retirement][0]
        for field, value in [
            ("identity_state", "unknown"),
            ("storage_identity", "foreign"),
            ("location_config_revision", None),
            ("binding_generation", 2),
            ("location_id", "foreign"),
            ("bucket", "foreign"),
            ("role", "original_cache"),
        ]:
            original = getattr(retirement, field)
            setattr(retirement, field, value)
            with self.subTest(field=field), self.assertRaises(WorktreeError):
                await remap_storage(self.db, self.source, self.target, self.endpoint)
            self.assertEqual(
                self.rows[self.models.asset][0].bucket,
                self.source["MINIO_DATASETS_BUCKET"],
            )
            self.db.flush.assert_not_awaited()
            setattr(retirement, field, original)

    async def test_unknown_cleanup_owner_refuses_all_planned_changes(self):
        self.rows[self.models.deletion][0].bucket = "not-owned"
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertTrue(
            all(
                row.bucket in self.source.values()
                for row in self.rows[self.models.binding]
            )
        )
        self.db.flush.assert_not_awaited()

    async def test_cache_with_foreign_storage_identity_is_not_repaired_silently(self):
        self.rows[self.models.cache][0].storage_identity = "foreign"
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.assertEqual(
            self.rows[self.models.asset][0].bucket, self.source["MINIO_DATASETS_BUCKET"]
        )
        self.db.flush.assert_not_awaited()

    async def test_bad_legacy_binding_generation_cannot_be_adopted(self):
        self.rows[self.models.binding][-1].generation = 2
        with self.assertRaises(WorktreeError):
            await remap_storage(self.db, self.source, self.target, self.endpoint)
        self.db.flush.assert_not_awaited()


class RasterObjectFixtureTests(unittest.TestCase):
    def test_fixture_contains_exact_canonical_and_historical_gzip_bytes(self):
        import gzip
        from app.services import raster_object_storage
        from app.services.raster_mask_storage import canonical_rle_bytes
        from worktree_recovery_raster_objects import (
            CASES,
            HISTORICAL_GZIP_MTIME,
            encoded,
            rle_for,
        )

        self.assertEqual({case[1] for case in CASES}, {"pending", "ready", "retired"})
        self.assertEqual({case[2] for case in CASES}, {"identity", "gzip"})
        for case, _, encoding, index in CASES:
            reference, raw = encoded(case)
            facts = raster_object_storage.content(reference, raw)
            canonical = canonical_rle_bytes(rle_for(index))
            self.assertEqual(
                facts["canonical_sha256"], hashlib.sha256(canonical).hexdigest()
            )
            self.assertEqual(facts["stored_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(facts["stored_size_bytes"], len(raw))
            if encoding == "gzip":
                self.assertEqual(gzip.decompress(raw), canonical)
                self.assertEqual(
                    int.from_bytes(raw[4:8], "little"), HISTORICAL_GZIP_MTIME
                )
                self.assertNotEqual(facts["stored_sha256"], facts["canonical_sha256"])
            else:
                self.assertEqual(raw, canonical)

    def test_generations_share_only_logical_content_not_candidate_identity(self):
        from app.services import raster_object_storage
        from worktree_recovery_raster_objects import (
            CASES,
            GUARDED_CASES,
            encoded,
            identifier,
            legacy,
        )

        before, raw_before = encoded("retired-generation")
        after, raw_after = encoded("ready-generation")
        self.assertEqual(before, after)
        self.assertEqual(raw_before, raw_after)
        self.assertNotEqual(
            raster_object_storage.key_for(identifier("owner", "retired-generation")),
            raster_object_storage.key_for(identifier("owner", "ready-generation")),
        )
        ids = [identifier("owner", case[0]) for case in CASES]
        ids += [identifier("publication", case) for case in GUARDED_CASES]
        ids += [identifier("operation", "pending-write"), identifier("task", "deleted")]
        self.assertEqual(len(ids), len(set(ids)))
        legacy_ref, _ = legacy()
        self.assertNotIn(
            legacy_ref["object_key"],
            {encoded(case[0])[0]["object_key"] for case in CASES},
        )

    def test_object_independence_and_publication_restrict_are_model_facts(self):
        from app.db.models.raster_mask_object import (
            RasterMaskObject,
            RasterMaskPublication,
        )

        self.assertFalse(RasterMaskObject.__table__.foreign_keys)
        refs = list(RasterMaskPublication.__table__.foreign_keys)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].target_fullname, "raster_mask_objects.id")
        self.assertEqual(refs[0].ondelete, "RESTRICT")


class RasterObjectSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_source_removes_business_rows_without_retiring_owner(self):
        from app.db.models.annotation import Annotation
        from app.db.models.raster_mask_object import (
            RasterMaskObject,
            RasterMaskPublication,
        )
        from app.db.models.raster_mask_upload import RasterMaskUpload
        from app.db.models.storage_maintenance import StorageOperation
        from app.db.models.task import Task
        from worktree_recovery_raster_objects import identifier, seed_raster_objects

        actor, project = (
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4()),
        )
        task = SimpleNamespace(id=uuid.uuid4())
        db = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(revision=13)),
            scalar=AsyncMock(return_value=task),
            add=Mock(),
            flush=AsyncMock(),
            delete=AsyncMock(),
        )
        storage = SimpleNamespace(bucket="recovery-annotations", client=Mock())
        from app.services import raster_object_storage
        from app.services.dataset_assets import LEGACY_LOCATION_ID

        async def allocate(_db, row):
            return {
                "schema": 1,
                "purpose": "annotations",
                "location_id": str(LEGACY_LOCATION_ID),
                "config_revision": 1,
                "binding_generation": 1,
                "endpoint_url": "http://recovery.invalid:9000",
                "bucket": storage.bucket,
                "object_key": raster_object_storage.key_for(
                    row.id, row.storage_encoding
                ),
                "storage_identity": identity(
                    "http://recovery.invalid:9000", storage.bucket
                ),
            }

        with (
            patch.object(raster_object_storage, "allocate", side_effect=allocate),
            patch.object(
                raster_object_storage,
                "_runtime_endpoint",
                return_value="http://recovery.invalid:9000",
            ),
        ):
            await seed_raster_objects(
                db,
                actor=actor,
                project=project,
                storage=storage,
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
        removed = [call.args[0] for call in db.delete.await_args_list]
        self.assertEqual([type(row) for row in removed], [Annotation, Task])
        self.assertEqual(removed[0].task_id, removed[1].id)
        self.assertNotEqual(removed[1].id, task.id)
        added = [call.args[0] for call in db.add.call_args_list]
        owners = [row for row in added if isinstance(row, RasterMaskObject)]
        source_owner = next(
            row for row in owners if row.id == identifier("owner", "deleted-task")
        )
        self.assertEqual(source_owner.state, "ready")
        self.assertNotIn(source_owner, removed)
        self.assertEqual(len(owners), 7)
        self.assertEqual(sum(isinstance(row, RasterMaskUpload) for row in added), 1)
        guards = [row for row in added if isinstance(row, RasterMaskPublication)]
        self.assertEqual([row.gate_revision for row in guards], [13, 13])
        operation = next(row for row in added if isinstance(row, StorageOperation))
        self.assertEqual(operation.revision, 13)
        self.assertEqual(
            operation.purpose,
            "raster_object_write_" + identifier("owner", "pending-write").hex,
        )
        self.assertEqual(storage.client.put_object.call_count, 7)


class GuideAssetFixtureTests(unittest.TestCase):
    def test_fixture_bytes_really_cover_all_five_supported_image_types(self):
        import io
        from PIL import Image
        from app.services import guide_asset_storage
        from worktree_recovery_guide_assets import CASES, content_for

        self.assertEqual(
            {case[2] for case in CASES}, set(guide_asset_storage.ALLOWED_TYPES)
        )
        self.assertEqual({case[1] for case in CASES}, {"pending", "ready", "retired"})
        for case, _, _, image_format in CASES:
            raw = content_for(case)
            self.assertGreater(len(raw), 0)
            self.assertLessEqual(len(raw), guide_asset_storage.MAX_BYTES)
            if image_format is not None:
                with io.BytesIO(raw) as source, Image.open(source) as image:
                    self.assertEqual(image.format, image_format)
                    self.assertEqual(image.size, (2, 2))
                    image.verify()
            else:
                self.assertEqual(
                    raw,
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="blue"/></svg>\n',
                )

    def test_missing_origin_and_legacy_identities_cannot_collide_with_owners(self):
        from worktree_recovery_guide_assets import CASES, identifier, legacy_entry

        ids = [identifier("upload", case[0]) for case in CASES]
        ids += [
            identifier("original-project"),
            identifier("original-actor"),
            identifier("current-copy"),
            identifier("legacy"),
        ]
        self.assertEqual(len(ids), len(set(ids)))
        entry = legacy_entry(
            uuid.UUID(int=12), datetime(2026, 10, 24, 1, 2, 3, tzinfo=timezone.utc)
        )
        self.assertEqual(
            set(entry), {"key", "original_name", "content_type", "size", "uploaded_at"}
        )
        self.assertEqual(entry["uploaded_at"], "2026-10-24T01:02:03Z")
        self.assertTrue(entry["key"].endswith(str(identifier("legacy"))))
        self.assertNotIn(identifier("legacy"), ids[: len(CASES)])


if __name__ == "__main__":
    unittest.main()
