"""Quality evidence distinguishes byte proof, inspection, availability and review."""

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import io
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import delete, select

from app.db.enums import UserRole
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_quality import (
    DatasetQualityResult as Result,
)
from app.schemas.dataset_quality import DatasetQualityConfig, DatasetQualityReview
from app.services import (
    content_snapshot as snapshots,
    dataset_quality as svc,
    dataset_content as content,
)
from app.services.dataset_assets import register_item_asset
from app.services.storage import storage_service
from app.workers import dataset_quality as worker
from tests.test_dataset_content import content_world as content_world, headers


def png(size=(48, 32), color="red"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
async def world(db_session, super_admin, monkeypatch):
    db, actor = db_session, super_admin[0]
    ds = Dataset(
        display_id=f"QC-{uuid.uuid4().hex[:8]}", name="Quality", created_by=actor.id
    )
    db.add(ds)
    await db.flush()
    blobs, items = {}, []
    for name, data, trusted in [
        ("a.png", png(), True),
        ("b.png", png(), True),
        ("bad.png", b"broken PNG", True),
        ("legacy.png", png(), False),
        ("different.png", png(color="blue"), True),
    ]:
        item = DatasetItem(
            dataset_id=ds.id,
            file_name=name,
            file_path=f"qc/{uuid.uuid4()}.png",
            file_type="image",
            file_size=len(data),
            content_hash="f" * 32,
        )
        db.add(item)
        await db.flush()
        await register_item_asset(
            db,
            item,
            sha256=hashlib.sha256(data).hexdigest() if trusted else None,
            size_bytes=len(data) if trusted else None,
        )
        items.append(item)
        blobs[item.file_path] = data
    await db.commit()

    from app.services import managed_original_reader as reader

    original_capture = reader.capture
    bucket = storage_service.datasets_bucket
    owned_keys = tuple(blobs)

    async def capture(spec, **kwargs):
        data = blobs[spec.object_key]
        if isinstance(data, Exception):
            raise data
        storage_service.client.put_object(
            Bucket=spec.bucket, Key=spec.object_key, Body=data
        )
        return await original_capture(spec, **kwargs)

    monkeypatch.setattr(reader, "capture", capture)

    async def dispatch(db, run):
        await db.commit()

    monkeypatch.setattr(worker, "dispatch", dispatch)
    try:
        yield db, ds, actor, items, blobs
    finally:
        for key in owned_keys:
            storage_service.client.delete_object(Bucket=bucket, Key=key)


async def run_all(db, run):
    job = await db.get(AsyncJob, run.async_job_id)
    await worker.execute(db, run.id, job.celery_task_id, "worker:test")
    await db.refresh(run)
    assert run.status == "completed", run.error_code


async def test_fixed_members_full_sha_duplicates_corrupt_and_unverified(world):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    extra = DatasetItem(
        dataset_id=ds.id, file_name="late.png", file_path="late", file_type="image"
    )
    db.add(extra)
    await db.commit()
    await run_all(db, run)
    assert run.total == 5 and run.processed == 5
    assert run.summary == {
        "processed_count": 5,
        "checked_count": 4,
        "skipped_count": 1,
        "issue_count": 4,
        "duplicate_group_count": 1,
        "duplicate_member_count": 2,
    }
    page = await svc.result_page(db, run, limit=1, offset=1, finding="exact_duplicate")
    assert page["total"] == 2 and len(page["items"]) == 1
    assert page["items"][0].duplicate_count == 2
    rows = list(await db.scalars(select(Result).where(Result.run_id == run.id)))
    bad = next(r for r in rows if r.file_name == "bad.png")
    assert bad.bytes_verified and bad.findings == ["decode_failed"]
    legacy = next(r for r in rows if r.file_name == "legacy.png")
    assert (
        legacy.state == "skipped" and not legacy.bytes_verified and not legacy.findings
    )
    assert all(r.item_id != extra.id for r in rows)


async def test_unavailable_and_changed_are_not_corrupt_and_deleted_identity_stays(
    world,
):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    blobs[items[0].file_path] = OSError("offline")
    blobs[items[1].file_path] = png(color="green")
    await db.execute(delete(DatasetItem).where(DatasetItem.id == items[4].id))
    await db.commit()
    await run_all(db, run)
    page = await svc.result_page(db, run, limit=20, offset=0)
    rows = {r.file_name: r for r in page["items"]}
    assert rows["a.png"].reason_code == "content_source_unavailable"
    assert rows["b.png"].reason_code in {"source_changed", "content_mismatch"}
    assert (
        rows["different.png"].reason_code == "item_removed"
        and rows["different.png"].stale
    )
    assert not rows["a.png"].findings and not rows["b.png"].bytes_verified
    assert run.summary["duplicate_group_count"] == 0


async def test_review_is_optimistic_audited_and_scoped_to_run(world):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    await run_all(db, run)
    row = await db.scalar(
        select(Result).where(Result.run_id == run.id, Result.file_name == "bad.png")
    )
    await svc.review_result(
        db,
        run,
        row.id,
        actor,
        DatasetQualityReview(
            state="dismissed", note="已人工确认可忽略", expected_revision=0
        ),
    )
    await db.commit()
    with pytest.raises(HTTPException) as conflict:
        await svc.review_result(
            db,
            run,
            row.id,
            actor,
            DatasetQualityReview(
                state="confirmed", note="迟到决定", expected_revision=0
            ),
        )
    assert conflict.value.status_code == 409
    audit = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "dataset_quality.review",
            AuditLog.target_id == str(row.id),
        )
    )
    assert audit.detail_json["sha256"] == row.sha256
    assert row.findings == ["decode_failed"]
    other = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    with pytest.raises(HTTPException) as absent:
        await svc.review_result(
            db,
            other,
            row.id,
            actor,
            DatasetQualityReview(state="open", note="wrong run", expected_revision=1),
        )
    assert absent.value.status_code == 404
    assert all(
        r.review_state == "open"
        for r in await db.scalars(select(Result).where(Result.run_id == other.id))
    )


async def test_cancel_resume_keeps_committed_members_and_fences_old_worker_signal(
    world,
):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    job = await db.get(AsyncJob, run.async_job_id)
    generation, old_job, old_task = run.generation, job.id, job.celery_task_id
    assert await worker.process_one(
        db, run.id, job.id, old_task, generation, "worker:test"
    )
    committed = await db.scalar(
        select(Result.id).where(Result.run_id == run.id, Result.state != "pending")
    )
    await svc.cancel_job(db, job, actor)
    await db.commit()
    await svc.resume(db, ds.id, run.id, actor)
    await db.commit()
    assert await svc.fenced(db, run.id, old_job, old_task, generation) is None
    await svc.signal_interrupted(db, old_job, old_task, cancelled=False)
    await db.commit()
    assert run.status == "pending" and run.processed == 1
    new_job = await db.get(AsyncJob, run.async_job_id)
    previous_task = new_job.celery_task_id
    new_job.celery_task_id = str(uuid.uuid4())
    await db.commit()
    await svc.signal_interrupted(db, new_job.id, previous_task, cancelled=False)
    await db.commit()
    assert run.status == "pending"
    await run_all(db, run)
    assert (await db.get(Result, committed)).state != "pending" and run.processed == 5


async def test_permission_revoked_during_io_does_not_commit_evidence(
    world, monkeypatch
):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    await db.commit()
    original = content.quality_snapshot

    async def revoked(*args, **kwargs):
        value = await original(*args, **kwargs)
        actor.role = UserRole.ANNOTATOR
        await db.commit()
        return value

    monkeypatch.setattr(content, "quality_snapshot", revoked)
    await worker.execute(
        db,
        run.id,
        (await db.get(AsyncJob, run.async_job_id)).celery_task_id,
        "worker:test",
    )
    await db.refresh(run)
    assert run.status == "failed" and run.error_code == "access_revoked"
    assert not await db.scalar(
        select(Result.id).where(
            Result.run_id == run.id, Result.bytes_verified.is_(True)
        )
    )


async def test_expired_lease_recovery_rotates_fence(world):
    db, ds, actor, items, blobs = world
    run = await svc.start(db, ds.id, actor, DatasetQualityConfig())
    job = await db.get(AsyncJob, run.async_job_id)
    old_task, generation = job.celery_task_id, run.generation
    run.status = "running"
    run.lease_until = svc.now() - timedelta(seconds=1)
    await db.commit()
    await worker.recover(db)
    assert run.status == "pending" and run.generation > generation
    assert job.celery_task_id != old_task
    assert await svc.fenced(db, run.id, job.id, old_task, generation) is None
    await run_all(db, run)


async def test_api_authority_parent_validation_jobs_and_cancel(
    world, httpx_client, super_admin, annotator, project_admin
):
    db, ds, actor, items, blobs = world
    client, auth = httpx_client, headers(super_admin)
    url = f"/api/v1/datasets/{ds.id}/quality-runs"
    assert (
        await client.post(url, json={"min_width": 100, "max_width": 10}, headers=auth)
    ).status_code == 422
    assert (
        await client.post(url, json={}, headers=headers(annotator))
    ).status_code == 403
    created = await client.post(url, json={}, headers=auth)
    assert created.status_code == 201, created.text
    value = created.json()
    assert "authority" not in value
    assert (await client.post(url, json={}, headers=auth)).status_code == 409
    assert (
        await client.get(url + "/" + value["id"], headers=headers(project_admin))
    ).status_code == 404
    wrong = f"/api/v1/datasets/{uuid.uuid4()}/quality-runs/{value['id']}"
    assert (await client.get(wrong, headers=auth)).status_code == 404
    job_url = f"/api/v1/async-jobs/{value['async_job_id']}"
    assert (await client.get(job_url, headers=headers(annotator))).status_code == 403
    assert (await client.get(job_url, headers=auth)).status_code == 200
    assert (await client.post(job_url + "/cancel", headers=auth)).status_code == 200
    assert (
        await client.post(url + "/" + value["id"] + "/resume", headers=auth)
    ).status_code == 200
    assert (
        await client.get(
            url + "/" + value["id"] + "/results?finding=unknown", headers=auth
        )
    ).status_code == 422


@pytest.mark.parametrize("data", [b"", b"\x89PNG\r\n\x1a\ntruncated"])
async def test_quality_capture_keeps_sha_for_bad_image_but_strict_reader_rejects(data):
    @contextmanager
    def reader():
        yield io.BytesIO(data), {"size": len(data)}

    sha = hashlib.sha256(data).hexdigest()
    snap = await snapshots.capture_for_quality(reader, sha)
    assert snap.sha256 == sha and snap.inspection_error == "invalid_image"
    snap.close()
    with pytest.raises(snapshots.ContentError, match="invalid_image"):
        await snapshots.capture(reader, sha)


async def test_exif_dimensions_and_budget_remain_distinct(monkeypatch):
    image = Image.new("RGB", (80, 40))
    exif = Image.Exif()
    exif[274] = 6
    stream = io.BytesIO()
    image.save(stream, format="JPEG", exif=exif)
    data = stream.getvalue()

    @contextmanager
    def reader():
        yield io.BytesIO(data), {"size": len(data)}

    snap = await snapshots.capture_for_quality(reader, hashlib.sha256(data).hexdigest())
    assert (snap.width, snap.height) == (40, 80)
    snap.close()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    snap = await snapshots.capture_for_quality(reader, hashlib.sha256(data).hexdigest())
    assert snap.inspection_error == "image_pixel_budget_exceeded"
    snap.close()


async def test_reference_dispatches_to_registered_worker_and_keeps_missing_separate(
    db_session, content_world, monkeypatch
):
    from app.services.dataset_references import register_entry

    root, data, actor, ds, location, source, scan, entry = content_world
    await register_entry(
        db_session, scan.id, entry.path_digest, actor, node_id="worker:content@test"
    )
    await db_session.commit()
    run = await svc.start(db_session, ds.id, actor, DatasetQualityConfig())
    await db_session.commit()
    assert await worker.next_node(db_session, run.id) == "content@test"
    job = await db_session.get(AsyncJob, run.async_job_id)
    assert (
        await worker.process_one(
            db_session,
            run.id,
            job.id,
            job.celery_task_id,
            run.generation,
            "worker:other@test",
        )
        == "yield"
    )
    assert run.processed == 0
    await worker.execute(db_session, run.id, job.celery_task_id, "worker:content@test")
    await db_session.refresh(run)
    assert run.status == "completed" and run.summary["checked_count"] == 1
    row = await db_session.scalar(select(Result).where(Result.run_id == run.id))
    assert row.bytes_verified and row.findings == ["dimensions_out_of_range"]
    other = await svc.start(db_session, ds.id, actor, DatasetQualityConfig())
    await db_session.commit()
    (root / "图片 空格.png").unlink()
    job = await db_session.get(AsyncJob, other.async_job_id)
    await worker.execute(
        db_session, other.id, job.celery_task_id, "worker:content@test"
    )
    row = await db_session.scalar(select(Result).where(Result.run_id == other.id))
    assert (
        row.state == "skipped"
        and row.reason_code != "invalid_image"
        and not row.bytes_verified
    )


async def test_quality_schema_roundtrip(db_session, monkeypatch):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    def verify(sync):
        path = Path(__file__).parents[1] / "alembic/versions/0207_dataset_quality.py"
        spec = importlib.util.spec_from_file_location(path.stem, path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        monkeypatch.setattr(
            migration, "op", Operations(MigrationContext.configure(sync.connection()))
        )
        child_path = path.with_name("0208_dataset_quality_review.py")
        child_spec = importlib.util.spec_from_file_location(child_path.stem, child_path)
        child = importlib.util.module_from_spec(child_spec)
        child_spec.loader.exec_module(child)
        monkeypatch.setattr(child, "op", migration.op)
        child.downgrade()
        migration.downgrade()
        assert (
            "dataset_quality_runs" not in inspect(sync.connection()).get_table_names()
        )
        migration.upgrade()
        child.upgrade()
        schema = inspect(sync.connection())
        foreign = schema.get_foreign_keys("dataset_quality_runs")
        assert (
            next(f for f in foreign if f["constrained_columns"] == ["async_job_id"])[
                "options"
            ]["ondelete"]
            == "SET NULL"
        )
        assert {
            c["name"] for c in schema.get_columns("dataset_quality_results")
        } == set(Result.__table__.columns.keys())
        assert any(
            i["name"] == "uq_dataset_quality_active" and i["unique"]
            for i in schema.get_indexes("dataset_quality_runs")
        )

    await db_session.run_sync(verify)
