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
from tests.export_worker_native_fixtures import setup, worker_db as worker_db
from tests.test_lidar_export_worker import Harness, state


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
    targets, opts = ["coco", "yolo-det", "native_json"], {"include_attributes": True}
    payload = {"targets": targets, "batch_id": None, **opts}
    if task_ids is not None:
        selected = [str(identifier) for identifier in task_ids]
        payload.update(task_ids=selected, scope={"task_ids": selected})
    job, receipt = await image_jobs.create_scoped_job(
        db,
        project,
        actor_id=project.owner_id,
        payload=payload,
        batch_id=None,
        targets=targets,
        opts=opts,
        task_ids=task_ids,
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
    project, _, _, _, task, _ = await setup(db, "image")
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
    project, _, _, _, task, annotation = await setup(db, "image")
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
    project, _, _, _, _, annotation = await setup(db, "image")
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
    project, *_ = await setup(db, "image")
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
    project, *_ = await setup(db, "image")
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


async def test_image_scope_blocks_only_its_dataset_and_fences_rebinding(worker_db):
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db.models.dataset import Dataset
    from app.db.models.fixture_run import FixtureRun
    from app.services import fixture_run_ownership as ownership

    db = worker_db
    project, dataset, *_ = await setup(db, "image")
    own_run, neighbor_run, neighbor_dataset = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.add_all(
        [
            Dataset(
                id=neighbor_dataset,
                display_id=f"nbr-{neighbor_dataset.hex[:12]}",
                name="Neighbor originals",
                data_type="image",
                created_by=project.owner_id,
                metadata_={},
            ),
            FixtureRun(id=own_run, family="scale"),
            FixtureRun(id=neighbor_run, family="scale"),
        ]
    )
    await db.flush()
    await ownership.reserve(
        db, own_run, "dataset", dataset.id, logical_key="dataset", published=True
    )
    await ownership.reserve(
        db,
        neighbor_run,
        "dataset",
        neighbor_dataset,
        logical_key="dataset",
        published=True,
    )
    await ownership.seal(db, neighbor_run, family="scale")
    assert (
        await db.scalar(
            text("SELECT yoao_assets_v1.seal_fixture_run_without_media(:run)"),
            {"run": neighbor_run},
        )
        is True
    )
    await db.commit()

    job_id, receipt, _ = await request(db, project)
    await ownership.guard_dataset_jobs(db, [neighbor_dataset])
    with pytest.raises(HTTPException) as blocked:
        async with db.begin_nested():
            await ownership.guard_dataset_jobs(db, [dataset.id])
    assert blocked.value.detail["code"] == "fixture_dataset_job_unsettled"
    with pytest.raises(DBAPIError, match="image export job binding changed"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "UPDATE public.async_jobs SET payload = payload || jsonb_build_object("
                    "'image_media', jsonb_build_object('schema', 3, "
                    "'bundle_id', CAST(:other AS text), 'input_digest', CAST(:digest AS text))) "
                    "WHERE id = :id"
                ),
                {
                    "id": job_id,
                    "other": str(uuid.uuid4()),
                    "digest": receipt.input_digest,
                },
            )
    assert await db.scalar(
        text("SELECT public.image_export_scope_matches(:job, :bundle)"),
        {"job": job_id, "bundle": receipt.id},
    )
    assert not await db.scalar(
        text("SELECT public.image_export_scope_matches(:job, :bundle)"),
        {"job": job_id, "bundle": uuid.uuid4()},
    )
    await ownership.seal(db, own_run, family="scale")
    await db.commit()
    with pytest.raises(ContentError, match="fixture_run_closed"):
        async with db.begin_nested():
            await request(db, project)
    with pytest.raises(DBAPIError, match="fixture_dataset_job_scope_unknown"):
        async with db.begin_nested():
            db.add(
                AsyncJob(
                    id=uuid.uuid4(),
                    kind="export",
                    project_id=project.id,
                    user_id=project.owner_id,
                    payload={},
                )
            )
            await db.flush()
    with pytest.raises(DBAPIError, match="image export scope is not bound"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "SELECT public.prepare_image_export_scope("
                    ":job, :bundle, :project, :actor, CAST(ARRAY[] AS uuid[]))"
                ),
                {
                    "job": uuid.uuid4(),
                    "bundle": uuid.uuid4(),
                    "project": project.id,
                    "actor": project.owner_id,
                },
            )
            await db.execute(
                text("SET CONSTRAINTS public.image_export_scope_complete IMMEDIATE")
            )


async def test_unbound_image_delivery_and_changed_source_never_start_io(
    worker_db, monkeypatch
):
    db = worker_db
    project, _, item, _, _, _ = await setup(db, "image")
    harness = ImageHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)

    other_job, other_receipt, _ = await request(db, project)
    owner_id = project.owner_id
    job = await db.get(AsyncJob, job_id)
    with pytest.raises(ContentError, match="scope_binding_changed"):
        await image_jobs.assert_scope_binding(db, job, other_receipt)
    assert harness.prepares == 0
    legacy = AsyncJob(
        id=uuid.uuid4(),
        kind="export",
        user_id=project.owner_id,
        project_id=project.id,
        payload={"targets": args["targets"], "batch_id": None, **args["opts"]},
    )
    db.add(legacy)
    await db.flush()
    await image_jobs.capture_job(
        db, legacy, project, batch_id=None, targets=args["targets"], opts=args["opts"]
    )
    await db.commit()
    with pytest.raises(ContentError, match="scope_binding_changed"):
        await worker.run_owned(db, **{**args, "async_job_id": str(legacy.id)})
    assert harness.prepares == 0

    from app.db.models.dataset import Dataset

    replacement = Dataset(
        id=uuid.uuid4(),
        display_id=f"DS-{uuid.uuid4().hex[:12]}",
        name="replacement",
        data_type="image",
        created_by=owner_id,
        metadata_={},
    )
    db.add(replacement)
    await db.flush()
    item.dataset_id = replacement.id
    await db.commit()
    with pytest.raises(ContentError, match="inputs_changed"):
        await worker.run_owned(db, **args)
    assert harness.prepares == 0
    assert (await state(db, ExportMediaBundle, receipt.id)).published_at is None
    assert (await state(db, AsyncJob, other_job)).status == "pending"
