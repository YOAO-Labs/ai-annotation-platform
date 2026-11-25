"""Real frozen video ZIPs and SQL publication over the shared attempt fence."""

from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models.async_job import AsyncJob
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.services import async_job, export_delivery_data, export_storage, storage_access
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.exporting import video_delivery, video_jobs
from app.services.exporting.reference_media import ExportCancelled
from app.services.jobs_dispatch import prepare_job_dispatch
from app.workers.export import _ExportExecution
from app.workers import lidar_export as worker
from tests.export_worker_native_fixtures import setup, worker_db as worker_db
from tests.test_lidar_export_worker import Harness, state
from tests.test_workspace_project_authority_locking import _api_url


@pytest.fixture
def publication_sessions(request):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1":
        return request.getfixturevalue("native_publication_sessions")
    db = request.getfixturevalue("worker_db")
    return db, db


@pytest.fixture
async def native_publication_sessions(test_engine):
    url = _api_url(test_engine)
    business_engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with (
            async_sessionmaker(test_engine, expire_on_commit=False)() as owner,
            async_sessionmaker(business_engine, expire_on_commit=False)() as business,
        ):
            assert await business.scalar(text("SELECT SESSION_USER")) == url.username
            await business.rollback()
            yield owner, business
    finally:
        await business_engine.dispose()


class VideoHarness(Harness):
    def __init__(self, db, monkeypatch):
        super().__init__(db, monkeypatch)

        async def prepare(db, receipt, *, guard):
            self.prepares += 1
            self.guard = guard
            self.copies.update(receipt.inventory["objects"])
            context = video_delivery.Delivery(receipt)
            for row in receipt.input_snapshot["media"]:
                source = row["source"]
                context._receipts[uuid.UUID(row["task_id"])] = SimpleNamespace(
                    url="https://saved.test/video-copy",
                    original_sha=source["sha256"],
                    size=source["size_bytes"],
                )
                identifier = uuid.UUID(row["task_id"])
                metadata = {
                    "fps": 6.0,
                    "frame_count": 12,
                    "width": 64,
                    "height": 48,
                    "duration_ms": 2000,
                    "codec": "h264",
                }
                context.metadata_by_task[identifier] = metadata
                context.metadata_receipts[identifier] = {
                    "probe_contract": 1,
                    "video": metadata,
                    "sha256": source["sha256"],
                    "size_bytes": source["size_bytes"],
                }
            await guard(db)
            await db.commit()
            if self.during_zip is not None:
                await self.during_zip()
            return context

        monkeypatch.setattr(video_delivery, "prepare", prepare)


async def request(db, project, *, task_ids=None, observer=None):
    await (db if observer is None else observer).refresh(project)
    targets, opts = (
        ["video_json", "mot", "kitti", "native_json"],
        {"include_attributes": True},
    )
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
    receipt = await video_jobs.capture_job(
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
            "media_kind": "video",
        },
    )


@pytest.mark.parametrize("selected", [False, True])
async def test_video_zip_and_verified_cache_keep_first_deadline(
    worker_db, monkeypatch, selected
):
    db = worker_db
    project, _, _, _, task, _ = await setup(db, "video")
    harness = VideoHarness(db, monkeypatch)
    selection = [task.id] if selected else None
    first, receipt, args = await request(db, project, task_ids=selection)
    await worker.run_owned(db, **args)
    row = await state(db, AsyncJob, first)
    assert row.status == "completed" and not row.result["cache_hit"]
    assert row.result["expires_at"] == receipt.expires_at.isoformat()
    output = await state(db, ExportOutput, receipt.zip_output_id)
    assert output.media_bundle_id == receipt.id and output.state == "ready"
    assert output.media == "video"
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
    project, _, _, _, task, annotation = await setup(db, "video")
    await db.delete(annotation)
    await db.delete(task)
    await db.commit()
    harness = VideoHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project)
    assert receipt.object_count == 0
    await worker.run_owned(db, **args)
    assert (await state(db, AsyncJob, job_id)).status == "completed"
    row = await state(db, ExportMediaBundle, receipt.id)
    assert row.state == "ready" and row.inventory["objects"] == {}
    assert harness.prepares == 1 and len(harness.objects) == 1 and not harness.copies


async def test_annotation_change_during_packaging_keeps_unpublished_responsibility(
    publication_sessions, monkeypatch
):
    observer, db = publication_sessions
    project, _, _, _, _, annotation = await setup(observer, "video")
    annotation_id = annotation.id
    harness = VideoHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project, observer=observer)

    async def mutate():
        from app.db.models.annotation import Annotation

        current = await observer.get(Annotation, annotation_id, populate_existing=True)
        current.geometry = {"type": "bbox", "x": 0.3, "y": 0.1, "w": 0.2, "h": 0.2}
        await observer.commit()

    harness.during_zip = mutate
    with pytest.raises(ContentError, match="inputs_changed"):
        await worker.run_owned(db, **args)
    assert (await state(observer, AsyncJob, job_id)).status == "failed"
    output = await state(observer, ExportOutput, receipt.zip_output_id)
    assert output.state == "pending"
    assert (await state(observer, ExportMediaBundle, receipt.id)).published_at is None
    assert (
        await observer.scalar(
            select(ExportOutput.id).where(
                ExportOutput.project_id == receipt.project_id,
                ExportOutput.state == "ready",
            )
        )
        is None
    )
    assert all(not path.exists() for path in harness.built_paths)


async def test_cancel_after_zip_write_retains_pending_owner_and_cancelled_status(
    worker_db, monkeypatch
):
    db = worker_db
    project, *_ = await setup(db, "video")
    harness = VideoHarness(db, monkeypatch)
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
async def test_unknown_final_commit_keeps_paired_video_responsibility(
    publication_sessions, monkeypatch, committed
):
    from app.db.models.export_artifact import ExportArtifact

    observer, db = publication_sessions
    project, *_ = await setup(observer, "video")
    harness = VideoHarness(db, monkeypatch)
    job_id, receipt, args = await request(db, project, observer=observer)
    original_commit = db.commit
    fired = False

    async def unknown_commit():
        nonlocal fired
        await db.flush()
        status = (await async_job.get_job(db, job_id, populate_existing=True)).status
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
    job_status = (await state(observer, AsyncJob, job_id)).status
    output_state = (await state(observer, ExportOutput, receipt.zip_output_id)).state
    bundle = await state(observer, ExportMediaBundle, receipt.id)
    assert job_status == ("completed" if committed else "failed")
    assert output_state == ("ready" if committed else "pending")
    assert bundle.state == ("ready" if committed else "building")
    assert (bundle.published_at is not None) is committed
    assert (
        bool(
            await observer.scalar(
                select(ExportArtifact.id).where(
                    ExportArtifact.output_id == receipt.zip_output_id
                )
            )
        )
        is committed
    )
    assert all(not path.exists() for path in harness.built_paths)


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable verifier and both real export LOGINs",
)
async def test_delivery_capabilities_deny_cross_role_and_table_access(
    publication_sessions, test_engine
):
    owner, business = publication_sessions
    control_url = make_url(os.environ["AAP_MODULE_DATA_EXPORT_EXECUTION_URL"])
    assert control_url.username == "yoao_export_execution_client"
    assert (control_url.host, control_url.port, control_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    control_engine = create_async_engine(
        control_url, poolclass=NullPool, hide_parameters=True
    )
    project, *_ = await setup(owner, "video")
    job = await async_job.create_job(
        owner, kind="export", project_id=project.id, user_id=project.owner_id
    )
    delivery_id = str(uuid.uuid4())
    await prepare_job_dispatch(
        owner,
        job_id=job.id,
        task_name="app.workers.export.run_export",
        args=[],
        execution_id=delivery_id,
        kwargs={
            "project_id": str(project.id),
            "batch_id": None,
            "targets": ["video_json"],
            "opts": {},
            "async_job_id": str(job.id),
        },
    )
    await owner.commit()
    try:
        async with (
            control_engine.connect() as control,
            control_engine.connect() as other,
        ):
            assert (
                await control.scalar(text("SELECT SESSION_USER"))
                == control_url.username
            )
            privilege = text(
                "SELECT has_function_privilege(SESSION_USER, :signature, 'EXECUTE')"
            )
            for signature, allowed in (
                ("yoao_export_v1.begin_execution(uuid,uuid,text,uuid,integer)", False),
                ("yoao_export_v1.finish_execution(uuid,uuid)", False),
                ("yoao_export_v1.zip_receipt(uuid,text)", True),
                (
                    "yoao_export_v1.publish_media(uuid,uuid,timestamptz,timestamptz)",
                    True,
                ),
            ):
                assert (
                    await business.scalar(privilege, {"signature": signature})
                    is allowed
                )
                assert (
                    await control.scalar(privilege, {"signature": signature})
                    is not allowed
                )
            for signature in (
                "yoao_export_v1._zip_has_read(uuid)",
                "yoao_export_v1.retained_zip_download(uuid)",
                "yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb)",
            ):
                assert not await business.scalar(privilege, {"signature": signature})
                assert not await control.scalar(privilege, {"signature": signature})
            access = text(
                "SELECT has_table_privilege(SESSION_USER, :relation, "
                "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                "OR has_any_column_privilege(SESSION_USER, :relation, "
                "'SELECT,INSERT,UPDATE,REFERENCES')"
            )
            for relation in (
                "public.export_artifacts",
                "public.export_outputs",
                "public.export_media_bundles",
                "public.export_media_member_versions",
                "yoao_export_v1.executions",
                "yoao_export_v1._media_delivery",
                "yoao_export_v1.export_zip_maintenance",
            ):
                assert not await business.scalar(access, {"relation": relation})
                assert not await control.scalar(access, {"relation": relation})

            lock = text("SELECT pg_try_advisory_lock(hashtextextended(:tag, 0))")
            assert await control.scalar(lock, {"tag": f"aap-export:control:{job.id}"})
            begin = text(
                "SELECT yoao_export_v1.begin_execution("
                ":job, :project, :delivery, :token, :pid)"
            )
            params = {
                "job": job.id,
                "project": project.id,
                "delivery": delivery_id,
                "token": uuid.uuid4(),
                "pid": await business.scalar(text("SELECT pg_backend_pid()")),
            }
            assert await control.scalar(begin, params) == "blocked"
            assert await other.scalar(lock, {"tag": f"aap-export:job:{job.id}"})
            assert (
                await control.scalar(
                    begin,
                    {
                        **params,
                        "pid": await other.scalar(text("SELECT pg_backend_pid()")),
                    },
                )
                == "blocked"
            )
            assert await other.scalar(
                text("SELECT pg_advisory_unlock(hashtextextended(:tag, 0))"),
                {"tag": f"aap-export:job:{job.id}"},
            )
            execution = _ExportExecution(business, await business.connection(), control)
            await execution.acquire(f"job:{job.id}")
            assert await control.scalar(
                text("SELECT pg_advisory_unlock(hashtextextended(:tag, 0))"),
                {"tag": f"aap-export:control:{job.id}"},
            )
            assert await control.scalar(begin, params) == "blocked"
            await execution.begin(job.id, project.id, delivery_id)
            await execution.enter_io()
            await execution.settle_io()
            await execution.finish(job.id)
    finally:
        await control_engine.dispose()


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable verifier and real Source and business LOGINs",
)
async def test_source_zip_proof_binds_one_saved_address_operation_and_phase(
    publication_sessions, test_engine
):
    _, business = publication_sessions
    source_url = make_url(os.environ["AAP_MODULE_DATA_ASSET_SOURCE_URL"])
    assert (source_url.host, source_url.port, source_url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert source_url.username != await business.scalar(text("SELECT SESSION_USER"))
    created = datetime.now(timezone.utc)
    candidate = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        media_bundle_id=None,
        media="video",
        format="native_json",
        file_count=1,
        sha256="a" * 64,
        size_bytes=1,
        created_at=created,
        expires_at=created + timedelta(days=7),
    )
    candidate.storage_ref = await export_storage.allocate(business, candidate)
    assert await export_delivery_data.reserve_zip(business, candidate)
    await business.commit()
    permit = await storage_access.acquire(
        business, LEGACY_LOCATION_ID, "export_write_" + candidate.id.hex
    )
    source_engine = create_async_engine(
        source_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        async with permit, source_engine.connect() as source:
            assert (
                await source.scalar(text("SELECT SESSION_USER")) == source_url.username
            )
            subject = text(
                "SELECT yoao_export_v1.zip_io_subject(:output, :op, :action)"
            )
            params = {"output": candidate.id, "op": permit.id, "action": "write"}
            facts = await source.scalar(subject, params)
            assert facts["phase"] == "awaiting"
            assert facts["operation_id"] == str(permit.id)
            assert facts["object_key"] == candidate.storage_ref["object_key"]
            assert facts["storage_ref"] == candidate.storage_ref
            assert facts["version_id"] is None and facts["sha256"] == candidate.sha256
            for drift in (
                {"op": uuid.uuid4()},
                {"output": uuid.uuid4()},
                {"action": "sign"},
                {"action": "delete"},
            ):
                assert await source.scalar(subject, params | drift) is None
            assert not await business.scalar(
                text(
                    "SELECT has_function_privilege(SESSION_USER,"
                    "'yoao_export_v1.save_source_zip_proof(uuid,uuid,jsonb)','EXECUTE')"
                )
            )
            save = text(
                "SELECT yoao_export_v1.save_source_zip_proof(:output, :op, CAST(:proof AS jsonb))"
            )
            raw = {"version_id": "retained-exact-version", "etag": '"verified"'}
            proof = {"output": candidate.id, "op": permit.id, "proof": json.dumps(raw)}
            assert await source.scalar(save, proof | {"op": uuid.uuid4()}) is False
            assert await source.scalar(save, proof) is True
            assert await source.scalar(save, proof) is False
            assert await source.scalar(subject, params) is None
            await source.commit()

        reader = await storage_access.acquire(
            business, LEGACY_LOCATION_ID, "export_read_" + candidate.id.hex
        )
        async with reader, source_engine.connect() as source:
            facts = await source.scalar(
                subject, params | {"op": reader.id, "action": "sign"}
            )
            assert (
                facts["phase"] == "verified"
                and facts["version_id"] == raw["version_id"]
            )
            assert facts["etag"] == raw["etag"]
            retained = await export_delivery_data.zip_receipt(business, candidate.id)
            assert retained.state == "pending" and retained.upload_proof == raw
    finally:
        await source_engine.dispose()
