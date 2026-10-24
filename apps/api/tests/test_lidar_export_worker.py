"""Exercise real SQL ownership and ZIP publication through the LiDAR worker.

Physical transport and original authorization have their own boundary suites;
this fixture keeps SQL migrations, frozen serialization and cache coupling real.
"""

from contextlib import nullcontext
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, text

from app.db.models.async_job import AsyncJob
from app.db.models.export_artifact import ExportArtifact
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import export_media_storage as physical
from app.services import export_outputs, export_storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_delivery, lidar_jobs, output_authority
from app.services.exporting.lidar_frozen import FrozenLidar
from app.workers import export as legacy
from app.workers import lidar_export as worker
from tests.test_export_media_bundle_models import _migration
from tests.test_lidar_export_inputs import input_db as input_db, setup


@pytest.fixture
async def worker_db(input_db):
    db = input_db
    await db.execute(
        text("CREATE TABLE async_jobs (LIKE public.async_jobs INCLUDING ALL)")
    )
    await db.execute(
        text(
            "CREATE TABLE export_artifacts (LIKE public.export_artifacts INCLUDING ALL)"
        )
    )
    await db.execute(text("ALTER TABLE export_artifacts DROP COLUMN output_id"))
    connection = await db.connection()
    await connection.run_sync(
        lambda sync: _migration(sync, "0224_export_output_storage").upgrade()
    )
    await connection.run_sync(lambda sync: _migration(sync).upgrade())
    await db.commit()
    return db


class Harness:
    def __init__(self, db, monkeypatch):
        self.db, self.objects, self.copies = db, {}, {}
        self.prepares = 0
        self.revoked = False
        self.during_zip = None
        self.guard = None
        self.built_paths = []
        self.notices = []

        async def authorize(db, *_args, **_kwargs):
            if self.revoked:
                raise ContentError("original_key_revoked")

        async def notify(db, job_id, **kwargs):
            self.notices.append((job_id, kwargs))

        monkeypatch.setattr(legacy, "_reauthorize_export_final_write", authorize)
        monkeypatch.setattr(legacy, "_emit_export_notification", notify)
        monkeypatch.setattr(legacy, "_publish_export_notification", AsyncMock())
        monkeypatch.setattr(output_authority, "validate", AsyncMock())
        monkeypatch.setattr(storage_access, "check", AsyncMock())
        monkeypatch.setattr(
            export_storage,
            "sign",
            lambda receipt, **_kwargs: "https://saved.test/" + str(receipt.id),
        )

        async def io(db, receipt, call, *, guard, **kwargs):
            await guard(db)
            result = call()
            await guard(db)
            await db.commit()
            return result, 1

        monkeypatch.setattr(worker, "_io", io)

        async def prepare(db, receipt, *, guard):
            self.prepares += 1
            self.guard = guard
            self.copies.update(receipt.inventory["objects"])
            outer = self

            class View(FrozenLidar):
                expires_at = receipt.expires_at

                def entry(self, task_id, role):
                    source = next(
                        row["source"]
                        for row in receipt._input_snapshot["media"]
                        if row["task_id"] == str(task_id) and row["role"] == role
                    )
                    return dict(
                        presigned_url="https://saved.test/copy",
                        expires_at=self.expires_at.isoformat(),
                        sha256=source["sha256"],
                        size_bytes=source["size_bytes"],
                    )

                def point_count(self, task_id):
                    return 3

            await guard(db)
            await db.commit()
            if outer.during_zip:
                await outer.during_zip()
            return View(receipt.input_snapshot)

        monkeypatch.setattr(lidar_delivery, "prepare", prepare)

        def write(receipt, path):
            self.built_paths.append(Path(path))
            data = Path(path).read_bytes()
            assert hashlib.sha256(data).hexdigest() == receipt.sha256
            assert len(data) == receipt.size_bytes
            self.objects[receipt.id] = data

        def verify(receipt, **kwargs):
            data = self.objects.get(receipt.id)
            return (
                data is not None and hashlib.sha256(data).hexdigest() == receipt.sha256
            )

        monkeypatch.setattr(export_storage, "write", write)
        monkeypatch.setattr(export_storage, "verify", verify)
        monkeypatch.setattr(
            physical, "open_storage", lambda record: nullcontext(object())
        )

        def verify_copy(client, record, relative):
            key = physical.resolve(record).prefix + relative
            if self.copies.get(key) != record.inventory["objects"][key]:
                raise physical.ExportMediaStorageError(
                    "export_media_storage_integrity_mismatch"
                )
            return self.copies[key]

        monkeypatch.setattr(physical, "verify_object", verify_copy)
        monkeypatch.setattr(
            physical,
            "list_objects",
            lambda client, record: {
                key: facts["size_bytes"]
                for key, facts in self.copies.items()
                if key.startswith(physical.resolve(record).prefix)
            },
        )


async def request(db, project, *, targets=None):
    targets = targets or ["pointmask", "aap_json"]
    opts = {"lidar": {"kitti_camera_role": "camera_front"}}
    await db.refresh(project)
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="export",
        project_id=project.id,
        user_id=project.owner_id,
        payload={"targets": targets},
    )
    db.add(job)
    await db.flush()
    receipt = await lidar_jobs.capture_job(
        db, job, project, batch_id=None, targets=targets, opts=opts
    )
    await db.commit()
    args = dict(
        project_id=str(project.id),
        batch_id=None,
        task_ids=None,
        targets=targets,
        opts=opts,
        async_job_id=str(job.id),
        celery_task_id=str(uuid.uuid4()),
    )
    return job.id, receipt, args


async def state(db, model, identifier):
    await db.rollback()
    return await db.get(model, identifier, populate_existing=True)


async def test_real_zip_publication_then_verified_cache_preserves_original_deadline(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    first, receipt, args = await request(db, project)
    await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, first)
    assert row.status == "completed" and row.progress_pct == 100
    assert row.result["cache_hit"] is False
    assert row.result["expires_at"] == receipt.expires_at.isoformat()
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.state == "ready" and output.media_bundle_id == receipt.id
    saved_output = export_outputs.snapshot(output)
    bundle = await state(db, ExportMediaBundle, receipt.id)
    assert bundle.state == "ready" and bundle.published_at is not None
    second, new_receipt, again = await request(db, project)
    assert new_receipt.expires_at > receipt.expires_at
    await worker.run_owned(db, **again)
    row = await state(db, AsyncJob, second)
    assert row.status == "completed" and row.result["cache_hit"] is True
    assert row.result["expires_at"] == receipt.expires_at.isoformat()
    assert row.result["object_key"] == export_outputs.object_key(saved_output)
    assert saved_output.expires_at == receipt.expires_at
    assert harness.prepares == 1 and len(harness.objects) == 1
    assert len(harness.notices) == 2
    assert all(not path.exists() for path in harness.built_paths)
    # This HTTP attempt's unused inventory is never falsely published.
    unused = await state(db, ExportMediaBundle, new_receipt.id)
    assert unused.state == "building" and unused.published_at is None


async def test_duplicate_and_stale_deliveries_cannot_copy_or_change_winner(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    await worker.run_owned(db, **args)
    before = dict((await state(db, AsyncJob, job_id)).result)
    await worker.run_owned(db, **args)
    await worker.run_owned(db, **{**args, "celery_task_id": str(uuid.uuid4())})
    row = await state(db, AsyncJob, job_id)
    assert row.status == "completed" and row.result == before
    assert harness.prepares == 1 and len(harness.notices) == 1
    assert (await state(db, ExportMediaBundle, receipt.id)).state == "ready"


@pytest.mark.parametrize("change", ["revoked", "cancelled", "superseded"])
async def test_late_attempt_is_fenced_before_zip_or_ready_publication(
    worker_db, monkeypatch, change
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)

    async def interrupt():
        if change == "revoked":
            harness.revoked = True
        else:
            row = await db.get(AsyncJob, job_id)
            if change == "cancelled":
                row.status = "cancelled"
            else:
                row.celery_task_id = "superseding-delivery"
                row.progress_pct = 73
            await db.commit()

    harness.during_zip = interrupt
    with pytest.raises(ContentError):
        await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, job_id)
    assert row.status == (
        "failed"
        if change == "revoked"
        else "cancelled"
        if change == "cancelled"
        else "running"
    )
    if change == "superseded":
        assert row.celery_task_id == "superseding-delivery" and row.progress_pct == 73
    assert not harness.objects
    assert await db.get(ExportOutput, receipt.zip_output_id) is None
    assert (await state(db, ExportMediaBundle, receipt.id)).published_at is None
    assert not list((await db.scalars(select(ExportArtifact))).all())


async def test_changed_input_before_claim_fails_without_physical_writes(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    project.name = "changed input"
    await db.commit()
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, job_id)
    assert row.status == "failed" and row.error_message == "lidar_export_inputs_changed"
    assert not harness.objects and not harness.copies
    assert (await state(db, ExportMediaBundle, receipt.id)).lease_token is None


async def test_corrupt_saved_copy_invalidates_cache_without_reusing_or_retiring_old_owner(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    first, receipt, args = await request(db, project)
    await worker.run_owned(db, **args)
    corrupt_key = next(iter(harness.copies))
    harness.copies[corrupt_key] = {"sha256": "0" * 64, "size_bytes": 5}
    second, new_receipt, again = await request(db, project)
    await worker.run_owned(db, **again)
    row = await state(db, AsyncJob, second)
    assert row.status == "completed" and row.result["cache_hit"] is False
    assert row.result["expires_at"] == new_receipt.expires_at.isoformat()
    assert harness.prepares == 2 and len(harness.objects) == 2
    previous = await state(db, ExportMediaBundle, receipt.id)
    assert previous.state == "ready" and previous.expires_at == receipt.expires_at
    assert previous.published_at is not None
    assert (await state(db, AsyncJob, first)).status == "completed"


async def test_duplicate_same_celery_delivery_during_build_does_not_steal_claim(
    worker_db, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)

    async def duplicate():
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
            await worker.run_owned(other, **args)

    harness.during_zip = duplicate
    await worker.run_owned(db, **args)
    assert (await state(db, AsyncJob, job_id)).status == "completed"
    assert (await state(db, ExportMediaBundle, receipt.id)).state == "ready"
    assert (
        harness.prepares == 1
        and len(harness.objects) == 1
        and len(harness.notices) == 1
    )


@pytest.mark.parametrize(
    "old_proof",
    [None, {"schema": 1, "bundle_id": str(uuid.uuid4()), "input_digest": "a" * 64}],
)
async def test_matching_historical_job_without_contract_fails_with_actionable_code(
    worker_db, monkeypatch, old_proof
):
    db = worker_db
    project, *_ = await setup(db)
    harness = Harness(db, monkeypatch)
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="export",
        project_id=project.id,
        user_id=project.owner_id,
        payload={"targets": ["pointmask"]},
    )
    if old_proof is not None:
        job.payload = {**job.payload, "lidar_media": old_proof}
    db.add(job)
    await db.commit()
    job_id, project_id = job.id, project.id
    await worker.run_owned(
        db,
        project_id=str(project_id),
        batch_id=None,
        task_ids=None,
        targets=["pointmask"],
        opts={},
        async_job_id=str(job_id),
        celery_task_id="old-message",
    )
    row = await state(db, AsyncJob, job_id)
    assert (
        row.status == "failed" and row.error_message == "lidar_export_contract_missing"
    )
    assert not harness.objects and not harness.copies
    assert len(harness.notices) == 1


async def test_exact_snapshot_invalid_parent_is_rejected_before_claim_or_copy(
    worker_db, monkeypatch
):
    from tests.test_lidar_registered_preflight import calibration
    from tests.factory import build_tool_bindings

    db = worker_db
    project, _, _, _, _, _, cameras, annotation = await setup(db)
    project.tool_bindings = build_tool_bindings(["car"], unit="lidar_box_3d")
    camera = cameras[0][0]
    camera.metadata_ = {"calibration": calibration()}
    annotation.geometry = {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3}
    annotation.tool_unit_id = "bbox"
    annotation.annotation_type = "bbox"
    annotation.source = "manual"
    annotation.sensor_role = "camera_front"
    annotation.sensor_dataset_item_id = camera.id
    annotation.sensor_visibility = "visible"
    annotation.scene_track_id = uuid.uuid4()
    annotation.track_id = "orphan"
    annotation.calibration_revision = 1
    annotation.calibration_digest = "c" * 64
    await db.commit()
    harness = Harness(db, monkeypatch)
    job_id, receipt, args = await request(db, project, targets=["coco-multicamera"])
    with pytest.raises(ContentError, match="multicamera_coco_parent_invalid"):
        await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, job_id)
    assert (
        row.status == "failed"
        and row.error_message == "multicamera_coco_parent_invalid"
    )
    assert (await state(db, ExportMediaBundle, receipt.id)).lease_token is None
    assert harness.prepares == 0 and not harness.objects and not harness.copies
