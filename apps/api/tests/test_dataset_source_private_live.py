"""Current ordinary PostgreSQL LOGIN, with real catalog, spatial and scan paths."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yoao_assets_catalog import CreateDatasetRequest, GetDatasetRequest
from yoao_assets_catalog.spatial_contract import (
    CreateSceneRequest,
    ReadPosesRequest,
    UpsertPosesRequest,
)
from yoao_assets_catalog.spatial_values import FramePose

from app.application.dataset_task_creation import DatasetTaskCreationWorkflow
from app.config import settings
from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.db.models.project import Project
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.user import User
from app.schemas.dataset_source import DatasetSourceCreate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import jobs_lifecycle_data as jobs
from app.services import dataset_scans as scans
from app.services import filesystem_storage as filesystem
from app.services import storage_location
from app.services.assets_catalog import AssetsCatalogGateway
from app.services.assets_dataset_data import project_dataset_exists
from app.services.assets_spatial import SameSessionAssetsSpatialGateway
from app.services.dataset_scan_data import read_scan
from app.services.dataset_workbench import require_filter_scene
from tests.conftest import _create_user
from tests.test_dataset_link import _seed_project
from tests.test_dataset_scans import drain, manifest, next_work
from tests.test_module_data_roles import _engine, _role_url, pytestmark as pytestmark


@asynccontextmanager
async def ordinary():
    role = "identity_embedded_api_consumer"
    engine = _engine(
        _role_url("AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL", role)
    )
    try:
        async with engine.connect() as conn, conn.begin() as tx:
            async with AsyncSession(bind=conn, expire_on_commit=False) as db:
                assert (
                    await db.execute(text("SELECT session_user, current_user"))
                ).one() == (role, role)
                yield db
                await tx.rollback()
    finally:
        await engine.dispose()


async def assert_denied(db, statement):
    with pytest.raises(DBAPIError) as error:
        async with db.begin_nested():
            await db.execute(text(statement))
    assert error.value.orig.sqlstate == "42501"


async def test_catalog_project_link_and_pose_evidence_under_ordinary_login(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as owner:
        actor, _ = await _create_user(
            owner, "super_admin", f"{uuid4().hex}@storage.local", "Catalog owner"
        )
        project = await _seed_project(owner, actor.id)
    try:
        async with ordinary() as db:
            for statement in (
                "SELECT id FROM public.datasets LIMIT 0",
                "SELECT item.dataset_id FROM public.dataset_items AS item LIMIT 0",
                "SELECT project_id FROM public.project_datasets LIMIT 0",
                "SELECT frame_index FROM public.scene_frame_poses LIMIT 0",
                "SELECT nextval('public.display_seq_datasets')",
                "SELECT nextval('public.display_seq_scenes')",
                "SELECT yoao_assets_v1._dataset_relationship_digest('test')",
                "SELECT yoao_assets_v1._lock_dataset_directory_source(NULL::uuid)",
            ):
                await assert_denied(db, statement)
            gateway = AssetsCatalogGateway(db)
            dataset = (
                await gateway.create(
                    CreateDatasetRequest(
                        operation_id=uuid4(),
                        actor_id=actor.id,
                        name=f"Catalog {uuid4().hex}",
                        data_type="image",
                    )
                )
            ).dataset
            assert dataset.display_id
            await DatasetTaskCreationWorkflow(db).link_project(
                dataset.id, project.id, actor_id=actor.id
            )
            assert await project_dataset_exists(db, project.id, dataset.id)
            fetched = await gateway.get(
                GetDatasetRequest(actor_id=actor.id, dataset_id=dataset.id)
            )
            assert fetched.dataset.id == dataset.id
            spatial = SameSessionAssetsSpatialGateway(db)
            created = await spatial.create_scene(
                CreateSceneRequest(
                    operation_id=uuid4(), dataset_id=dataset.id, name="Camera track"
                )
            )
            scene_id = created.scene_id
            choice = await require_filter_scene(db, dataset.id, scene_id)
            assert choice["name"] == "Camera track"
            pose = FramePose(
                frame_index=0,
                timestamp_us=-42,
                ego_translation=[1, 2, 3],
                ego_rotation=[0.0, 0.0, 1.0, 0.0],
                source_metadata={"sample": "camera-track-0"},
            )
            upserted = await spatial.upsert_poses(
                UpsertPosesRequest(
                    operation_id=uuid4(), scene_id=scene_id, poses=[pose]
                )
            )
            assert upserted.upserted == 1
            assert await spatial.read_frame_pose(scene_id, 0, actor_id=actor.id) == pose
            native_trajectory = await spatial.read_poses(
                ReadPosesRequest(actor_id=actor.id, scene_id=scene_id)
            )
            expected = [pose.model_dump()]
            assert [entry.model_dump() for entry in native_trajectory] == expected
    finally:
        async with maker.begin() as owner:
            await owner.execute(delete(Project).where(Project.id == project.id))
            await owner.execute(delete(User).where(User.id == actor.id))


async def test_real_directory_checkpoint_fences_old_attempt_under_ordinary_login(
    test_engine, tmp_path, monkeypatch
):
    root = tmp_path / "controlled"
    root.mkdir()
    (root / "child").mkdir()
    (root / "child" / "a.jpg").write_bytes(b"retained directory observation")
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as owner:
        actor, _ = await _create_user(
            owner, "super_admin", f"{uuid4().hex}@storage.local", "Scan owner"
        )
        location = await storage_location.create_filesystem(
            owner,
            FilesystemLocationCreate(name=f"Scan {uuid4().hex}", root_handle="test"),
        )
    try:
        async with maker.begin() as owner:
            await storage_location.store_probe(
                owner,
                location,
                "worker:scan@test",
                filesystem.probe_directory(location.config),
                datetime.now(timezone.utc),
            )
        async with ordinary() as db:
            await assert_denied(
                db, "SELECT status FROM public.dataset_scan_runs LIMIT 0"
            )
            await assert_denied(
                db, "SELECT path_digest FROM public.dataset_scan_entries LIMIT 0"
            )
            await assert_denied(
                db, "SELECT nextval('public.dataset_source_acceptance_order_seq')"
            )
            dataset = (
                await AssetsCatalogGateway(db).create(
                    CreateDatasetRequest(
                        operation_id=uuid4(),
                        actor_id=actor.id,
                        name=f"Inventory {uuid4().hex}",
                        data_type="image",
                    )
                )
            ).dataset
            source = await scans.create_source(
                db,
                DatasetSourceCreate(
                    dataset_id=dataset.id,
                    location_id=location.id,
                    name="Directory",
                    node_id="scan@test",
                ),
                actor,
            )
            run, created = await scans.start_scan(db, source.id, actor)
            assert created
            original = await jobs.get_job(db, run.async_job_id)
            fence = await scans.claim(db, run.id, original.celery_task_id, "scan@test")
            assert fence is not None
            first = await next_work(db, run, original, fence)
            assert first["relative_path"] == "" and first["phase"] == "enumerating"
            await scans.apply_directory(
                db, run.id, original.celery_task_id, fence, first, manifest(first)
            )
            await scans.cancel_job(db, original.id, actor)
            assert (await read_scan(db, run.id)).status == "cancelled"
            run = await scans.resume_scan(db, run.id, actor)
            current = await jobs.get_job(db, run.async_job_id)
            assert current.id != original.id
            assert current.celery_task_id != original.celery_task_id
            fresh = await scans.claim(db, run.id, current.celery_task_id, "scan@test")
            assert fresh is not None
            with pytest.raises(scans.LostScanLease):
                await scans.apply_directory(
                    db, run.id, original.celery_task_id, fence, first, manifest(first)
                )
            await scans.signal_interrupted(db, original, cancelled=False)
            resumed = await read_scan(db, run.id)
            assert resumed.status == "running" and resumed.async_job_id == current.id
            checkpoint = await next_work(db, run, current, fresh)
            assert checkpoint["path_digest"] == first["path_digest"]
            assert checkpoint["phase"] == "resume_validation"
            await scans.apply_directory(
                db,
                run.id,
                current.celery_task_id,
                fresh,
                checkpoint,
                manifest(checkpoint),
            )
            await drain(db, run, current, fresh)
            finished = await read_scan(db, run.id)
            assert finished.complete and finished.generation == 2
            assert finished.counts["file_count"] == 1
            retired_job = await jobs.get_job(db, original.id, populate_existing=True)
            completed_job = await jobs.get_job(db, current.id, populate_existing=True)
            assert retired_job.status == "cancelled"
            assert completed_job.status == "completed"
            assert completed_job.progress_pct == 100
            assert completed_job.result["run_id"] == str(run.id)
            assert completed_job.result["complete"]
            assert completed_job.result["file_count"] == 1
    finally:
        async with maker.begin() as owner:
            await owner.execute(
                delete(StorageLocationProbe).where(
                    StorageLocationProbe.location_id == location.id
                )
            )
            await owner.execute(
                delete(StorageLocation).where(StorageLocation.id == location.id)
            )
            await owner.execute(
                delete(StorageMaintenanceGate).where(
                    StorageMaintenanceGate.location_id == location.id
                )
            )
            await owner.execute(delete(User).where(User.id == actor.id))
