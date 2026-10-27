"""Real accepted scan checkpoints advance independently of the percentage."""

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from cryptography.fernet import Fernet
import pytest

from app.config import settings
from app.core.crypto import encrypt_secret
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_source import DatasetScanEntry
from app.db.models.storage_connection import StorageConnection
from app.schemas.dataset_source import DatasetSourceCreate
from app.services import dataset_scans as scans
from app.services import connector_guard, storage_location as locations
from app.services.source_readers import s3 as backend
from app.workers import storage_usage
from tests.test_dataset_scans import (
    begin,
    manifest,
    next_work,
    world as world,  # noqa: F401
)
from tests.test_s3_references import s3_world as s3_world  # noqa: F401


async def test_directory_acceptance_advances_at_same_percent_and_poll_does_not(
    db_session, world
):
    (world[0] / "child").mkdir()
    run, job, fence = await begin(db_session, world)
    assert job.last_progress_at is None and job.progress_observation is None
    for expected in (1, 2):
        work = await next_work(db_session, run, job, fence)
        assert job.progress_pct == 0
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
        assert job.progress_observation["completed"] == expected
        assert job.progress_observation["sequence"] == expected
        assert job.progress_observation["stage"] == "scanning"
        assert job.progress_pct == 0
    last_at, observation = job.last_progress_at, deepcopy(job.progress_observation)
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, manifest(work)
        )
    await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
    assert (job.last_progress_at, job.progress_observation) == (last_at, observation)


async def test_directory_validation_accepts_once_and_keeps_phase_visible(
    db_session, world
):
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, manifest(work)
    )
    work = await next_work(db_session, run, job, fence)
    assert work["phase"] == "validating"
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, manifest(work)
    )
    at, observation = job.last_progress_at, deepcopy(job.progress_observation)
    assert observation["stage"] == "validating" and observation["sequence"] == 2
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, fence, work, manifest(work)
    )
    assert (job.last_progress_at, job.progress_observation) == (at, observation)


async def test_directory_checkpoint_and_progress_rollback_together(db_session, world):
    run, job, fence = await begin(db_session, world)
    work = await next_work(db_session, run, job, fence)
    run_id = run.id
    with pytest.raises(RuntimeError, match="owner commit failed"):
        async with db_session.begin_nested():
            await scans.apply_directory(
                db_session, run.id, job.celery_task_id, fence, work, manifest(work)
            )
            assert job.last_progress_at is not None
            raise RuntimeError("owner commit failed")
    await db_session.refresh(job)
    assert job.last_progress_at is None and job.progress_observation is None
    entry = await db_session.get(
        DatasetScanEntry, (run_id, work["path_digest"]), populate_existing=True
    )
    assert entry.state == "pending"


async def test_resume_revalidation_has_new_execution_and_rejects_old_generation(
    db_session, world
):
    run, old, fence = await begin(db_session, world)
    work = await next_work(db_session, run, old, fence)
    await scans.apply_directory(
        db_session, run.id, old.celery_task_id, fence, work, manifest(work)
    )
    old_observation = deepcopy(old.progress_observation)
    await scans.cancel_job(db_session, old.id, world[1])
    await scans.resume_scan(db_session, run.id, world[1])
    job = await db_session.get(AsyncJob, run.async_job_id)
    new_fence = await scans.claim(db_session, run.id, job.celery_task_id, "scan@test")
    await db_session.flush()
    assert job.last_progress_at is None
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(
            db_session, run.id, old.celery_task_id, fence, work, manifest(work)
        )
    work = await next_work(db_session, run, job, new_fence)
    assert work["phase"] == "resume_validation"
    await scans.apply_directory(
        db_session, run.id, job.celery_task_id, new_fence, work, manifest(work)
    )
    assert job.progress_observation["stage"] == "validating"
    assert job.progress_observation["sequence"] == 1
    assert job.progress_observation["execution_id"] != old_observation["execution_id"]


async def test_s3_accepted_pages_advance_without_percentage_and_stale_page_rejected(
    db_session, s3_world, monkeypatch
):
    memory = s3_world[0]
    memory.objects["root/collection/second.png"] = memory.data
    original = memory.list_objects_v2

    def one_per_page(**kwargs):
        return original(**{**kwargs, "MaxKeys": 1})

    monkeypatch.setattr(memory, "list_objects_v2", one_per_page)
    run, _ = await scans.start_scan(db_session, s3_world[-1].id, s3_world[1])
    job = await db_session.get(AsyncJob, run.async_job_id)
    fence = await scans.claim(db_session, run.id, job.celery_task_id, "content@test")
    await db_session.flush()
    for sequence in (1, 2):
        work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
        page = work["access"].list_page(work["path"], work["cursor"])
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, page
        )
        assert job.progress_pct == 0
        assert job.progress_observation["unit"] == "pages"
        assert job.progress_observation["sequence"] == sequence
    at = job.last_progress_at
    with pytest.raises(scans.LostScanLease):
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, page
        )
    assert job.last_progress_at == at


async def test_actual_minio_scan_page_is_accepted_with_checkpoint_progress(
    db_session, super_admin, monkeypatch
):
    # Only the local test endpoint bypasses the connector's loopback prohibition.
    # All S3 calls, parsing, saved owner facts and scan fences remain production.
    client = storage_usage._client()
    endpoint = client.meta.endpoint_url.rstrip("/")
    bucket = settings.minio_datasets_bucket
    prefix = f"progress-scan-{uuid.uuid4()}"
    key = prefix + "/one.png"
    client.put_object(Bucket=bucket, Key=key, Body=b"scan-progress")

    async def local_target(_db, target):
        assert target == endpoint

    def local_endpoint(target, _allowlist):
        assert target == endpoint

    monkeypatch.setattr(
        connector_guard, "assert_connection_target_allowed", local_target
    )
    monkeypatch.setattr(backend, "allowed_endpoint", local_endpoint)
    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    actor = super_admin[0]
    connection = StorageConnection(
        name=f"progress-source-{uuid.uuid4()}",
        kind="s3",
        scope="owner",
        created_by=actor.id,
        config={"endpoint": endpoint, "bucket": bucket, "base_prefix": prefix},
        secret_enc=encrypt_secret(
            {
                "access_key": settings.minio_access_key,
                "secret_key": settings.minio_secret_key,
            }
        ),
    )
    dataset = Dataset(
        display_id=f"progress-{uuid.uuid4().hex[:10]}",
        name="Progress",
        created_by=actor.id,
    )
    db_session.add_all([connection, dataset])
    await db_session.flush()
    location = await locations.register_connection(db_session, connection)
    try:
        result = await locations.perform_probe(
            db_session, location, "worker:progress@test", "one.png"
        )
        assert result["ok"], result
        await locations.store_probe(
            db_session,
            location,
            "worker:progress@test",
            result,
            datetime.now(timezone.utc),
        )
        source = await scans.create_source(
            db_session,
            DatasetSourceCreate(
                dataset_id=dataset.id,
                location_id=location.id,
                name="Progress",
                node_id="progress@test",
                cache_policy="none",
            ),
            actor,
        )
        run, _ = await scans.start_scan(db_session, source.id, actor)
        job = await db_session.get(AsyncJob, run.async_job_id)
        fence = await scans.claim(
            db_session, run.id, job.celery_task_id, "progress@test"
        )
        await db_session.flush()
        work = await scans.next_directory(db_session, run.id, job.celery_task_id, fence)
        page = work["access"].list_page(work["path"], work["cursor"])
        assert page["listed_count"] == 1
        await scans.apply_directory(
            db_session, run.id, job.celery_task_id, fence, work, page
        )
        await db_session.commit()
        await db_session.refresh(job)
        assert job.progress_observation["completed"] == 1
        assert job.progress_observation["unit"] == "pages"
        assert run.counts["file_count"] == 1
    finally:
        client.delete_object(Bucket=bucket, Key=key)
