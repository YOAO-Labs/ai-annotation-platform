"""Real frozen image ZIPs and SQL publication over the shared attempt fence."""

from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services.content_snapshot import ContentError
from app.services.exporting import image_delivery, image_jobs
from app.services.exporting.reference_media import ExportCancelled
from app.workers import lidar_export as worker
from tests.test_image_export_inputs import input_db as input_db, setup
from tests.test_lidar_export_worker import Harness, state, worker_db as worker_db


class ImageHarness(Harness):
    def __init__(self, db, monkeypatch):
        super().__init__(db, monkeypatch)

        async def prepare(db, receipt, *, guard):
            self.prepares += 1
            self.guard = guard
            self.copies.update(receipt.inventory["objects"])
            context = image_delivery.Delivery(receipt)
            for row in receipt.input_snapshot["media"]:
                source = row["source"]
                context._receipts[uuid.UUID(row["task_id"])] = SimpleNamespace(
                    url="https://saved.test/image-copy",
                    original_sha=source["sha256"],
                    size=source["size_bytes"],
                )
            await guard(db)
            await db.commit()
            if self.during_zip is not None:
                await self.during_zip()
            return context

        monkeypatch.setattr(image_delivery, "prepare", prepare)


async def request(db, project, *, task_ids=None):
    await db.refresh(project)
    targets, opts = ["coco", "yolo-det", "aap_json"], {"include_attributes": True}
    payload = {"targets": targets, "batch_id": None, **opts}
    if task_ids is not None:
        selected = [str(identifier) for identifier in task_ids]
        payload.update(task_ids=selected, scope={"task_ids": selected})
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="export",
        project_id=project.id,
        user_id=project.owner_id,
        payload=payload,
    )
    db.add(job)
    await db.flush()
    receipt = await image_jobs.capture_job(
        db, job, project, batch_id=None, targets=targets, opts=opts, task_ids=task_ids
    )
    await db.commit()
    return (
        job.id,
        receipt,
        {
            "project_id": str(project.id),
            "batch_id": None,
            "task_ids": [str(identifier) for identifier in task_ids]
            if task_ids is not None
            else None,
            "targets": targets,
            "opts": opts,
            "async_job_id": str(job.id),
            "celery_task_id": str(uuid.uuid4()),
            "media_kind": "image",
        },
    )


@pytest.mark.parametrize("selected", [False, True])
async def test_image_zip_and_verified_cache_keep_first_deadline(
    worker_db, monkeypatch, selected
):
    db = worker_db
    project, _, _, _, _, task, _ = await setup(db)
    harness = ImageHarness(db, monkeypatch)
    selection = [task.id] if selected else None
    first, receipt, args = await request(db, project, task_ids=selection)
    await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, first)
    assert row.status == "completed" and not row.result["cache_hit"]
    assert row.result["expires_at"] == receipt.expires_at.isoformat()
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.media_bundle_id == receipt.id and output.state == "ready"
    second, unused, again = await request(db, project, task_ids=selection)
    await worker.run_owned(db, **again)
    row = await state(db, AsyncJob, second)
    assert row.status == "completed" and row.result["cache_hit"]
    assert row.result["expires_at"] == receipt.expires_at.isoformat()
    assert harness.prepares == 1 and len(harness.objects) == 1
    assert (await state(db, ExportMediaBundle, unused.id)).published_at is None
    assert all(not path.exists() for path in harness.built_paths)


async def test_empty_scope_publishes_its_zero_media_bundle(worker_db, monkeypatch):
    db = worker_db
    project, _, _, _, _, task, annotation = await setup(db)
    await db.delete(annotation)
    await db.delete(task)
    await db.commit()
    harness = ImageHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    assert receipt.object_count == 0
    await worker.run_owned(db, **args)
    assert (await state(db, AsyncJob, job_id)).status == "completed"
    row = await state(db, ExportMediaBundle, receipt.id)
    assert row.state == "ready" and row.inventory["objects"] == {}
    assert harness.prepares == 1 and len(harness.objects) == 1 and not harness.copies


async def test_annotation_change_during_packaging_keeps_unpublished_responsibility(
    worker_db, monkeypatch
):
    db = worker_db
    project, _, _, _, _, _, annotation = await setup(db)
    annotation_id = annotation.id
    harness = ImageHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)

    async def mutate():
        from app.db.models.annotation import Annotation

        current = await db.get(Annotation, annotation_id, populate_existing=True)
        current.geometry = {"type": "bbox", "x": 0.3, "y": 0.1, "w": 0.2, "h": 0.2}
        await db.commit()

    harness.during_zip = mutate
    with pytest.raises(ContentError, match="inputs_changed"):
        await worker.run_owned(db, **args)
    assert (await state(db, AsyncJob, job_id)).status == "failed"
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.state == "pending"
    assert (await state(db, ExportMediaBundle, receipt.id)).published_at is None
    assert (
        await db.scalar(select(ExportOutput.id).where(ExportOutput.state == "ready"))
        is None
    )
    assert all(not path.exists() for path in harness.built_paths)


async def test_cancel_after_zip_write_retains_pending_owner_and_cancelled_status(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db)
    harness = ImageHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)

    async def cancel_after_write(db, saved, call, *, guard, **kwargs):
        await guard(db)
        result = call()
        job = await db.get(AsyncJob, job_id, populate_existing=True)
        job.status = "cancelled"
        await db.commit()
        await guard(db)
        return result, 1

    monkeypatch.setattr(worker, "_io", cancel_after_write)
    with pytest.raises(ExportCancelled):
        await worker.run_owned(db, **args)
    job = await state(db, AsyncJob, job_id)
    assert job.status == "cancelled" and not job.result.get("download_url")
    assert (await state(db, ExportOutput, receipt.zip_output_id)).state == "pending"
    assert (await state(db, ExportMediaBundle, receipt.id)).published_at is None
    assert len(harness.objects) == 1
    assert all(not path.exists() for path in harness.built_paths)


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_final_commit_keeps_paired_image_responsibility(
    worker_db, monkeypatch, committed
):
    from app.db.models.export_artifact import ExportArtifact

    db = worker_db
    project, *_ = await setup(db)
    harness = ImageHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    original_commit = db.commit
    fired = False

    async def unknown_commit():
        nonlocal fired
        status = await db.scalar(select(AsyncJob.status).where(AsyncJob.id == job_id))
        if not fired and status == "completed":
            fired = True
            if committed:
                await original_commit()
            raise OSError("commit acknowledgement unknown")
        await original_commit()

    monkeypatch.setattr(db, "commit", unknown_commit)
    with pytest.raises(OSError, match="acknowledgement unknown"):
        await worker.run_owned(db, **args)
    monkeypatch.setattr(db, "commit", original_commit)
    assert fired and len(harness.objects) == 1
    job_status = (await state(db, AsyncJob, job_id)).status
    output_state = (await state(db, ExportOutput, receipt.zip_output_id)).state
    bundle = await state(db, ExportMediaBundle, receipt.id)
    assert job_status == ("completed" if committed else "failed")
    assert output_state == ("ready" if committed else "pending")
    assert bundle.state == ("ready" if committed else "building")
    assert (bundle.published_at is not None) is committed
    assert (
        bool(
            await db.scalar(
                select(ExportArtifact.id).where(
                    ExportArtifact.output_id == receipt.zip_output_id
                )
            )
        )
        is committed
    )
    assert all(not path.exists() for path in harness.built_paths)
