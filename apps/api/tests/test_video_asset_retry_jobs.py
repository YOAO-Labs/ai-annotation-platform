"""Storage retry jobs observe existing output attempts without owning execution."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import func, select

from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.video_derivative import VideoDerivativeOutput as Output
from app.services import video_asset_retry_jobs as jobs
from app.services import video_derivative_objects as objects
from app.services import video_derivatives as core
from app.services import storage_access
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.workers import media_derivatives as worker
from tests.test_video_derivative_api import (
    _headers,
    _inventory,
    _publish,
    world as world,
)
from tests.test_video_derivative_objects import _new, owned as owned
from tests.test_video_derivative_pipeline import real_video as real_video


async def retry(client, actor, item, asset_type="probe", **extra):
    response = await client.post(
        "/api/v1/storage/video-assets/retry",
        headers=_headers(actor),
        json={"asset_type": asset_type, "dataset_item_id": str(item.id), **extra},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def detail(client, actor, job_id):
    response = await client.get(f"/api/v1/async-jobs/{job_id}", headers=_headers(actor))
    assert response.status_code == 200, response.text
    return response.json()


async def test_retry_reuses_own_job_and_preserves_other_actor_privacy(
    httpx_client, db_session, world, super_admin, annotator, project_admin
):
    first = await retry(httpx_client, super_admin, world.item)
    second = await retry(httpx_client, super_admin, world.item)
    assert first["job_id"] == second["job_id"]
    job = await db_session.get(AsyncJob, uuid.UUID(first["job_id"]))
    output = await db_session.get(Output, uuid.UUID(job.payload["output_id"]))
    assert job.project_id is None and job.celery_task_id is None
    assert set(job.payload) == {"output_id", "dataset_item_id", "asset_type"}
    assert (await detail(httpx_client, super_admin, job.id))["status"] == "pending"
    assert (
        await httpx_client.get(
            f"/api/v1/async-jobs/{job.id}", headers=_headers(annotator)
        )
    ).status_code == 403
    await objects.claim(db_session, output)
    original_authority = deepcopy(output.auth_ref)
    world.project.owner_id = project_admin[0].id
    await db_session.commit()
    response = await retry(httpx_client, project_admin, world.item)
    other = await db_session.get(AsyncJob, uuid.UUID(response["job_id"]))
    assert other.id != job.id and other.status == "running"
    await db_session.refresh(other)
    assert (await detail(httpx_client, project_admin, other.id))["status"] == "running"
    assert other.celery_task_id is None
    assert output.auth_ref == original_authority


@pytest.mark.parametrize("asset_type", ["probe", "poster", "frame_timetable"])
async def test_metadata_job_uses_requested_asset_result(
    httpx_client, db_session, world, super_admin, asset_type
):
    response = await retry(httpx_client, super_admin, world.item, asset_type)
    output, _ = await core.prepare(
        db_session, world.item.id, world.authority, "metadata"
    )
    token = await objects.claim(db_session, output)
    result = {
        "schema": 1,
        "kind": "metadata",
        "video": {
            "fps": 30,
            "frame_count": 90,
            "width": 640,
            "height": 480,
            "poster_error": "poster_extraction_failed",
            "frame_timetable_error": "frame_timetable_failed",
        },
        "frames": [],
    }
    await core.publish(
        db_session,
        output,
        world.authority,
        token,
        result,
        _inventory(output, result),
        await storage_access.check(db_session, LEGACY_LOCATION_ID),
    )
    await db_session.commit()
    job = await detail(httpx_client, super_admin, response["job_id"])
    assert output.state == "ready"
    assert job["status"] == ("completed" if asset_type == "probe" else "failed")
    assert job["result"]["success_count"] == (1 if asset_type == "probe" else 0)
    await jobs.sync_output(db_session, output)
    await db_session.commit()
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.target_id == uuid.UUID(response["job_id"]))
        )
        == 1
    )


@pytest.mark.parametrize("kind", ["frame", "chunk"])
async def test_retry_frame_and_chunk_follow_existing_published_output(
    httpx_client, db_session, world, super_admin, kind
):
    metadata = await _publish(db_session, world)
    extra = (
        {"frame_index": 3, "width": 320, "format": "webp"}
        if kind == "frame"
        else {"chunk_id": 0}
    )
    spec = (
        core.frame_spec(metadata, 3, 320, "webp")
        if kind == "frame"
        else core.chunk_spec(metadata, 0)
    )
    await core.prepare(db_session, world.item.id, world.authority, kind, spec=spec)
    await db_session.commit()
    response = await retry(httpx_client, super_admin, world.item, kind, **extra)
    await _publish(db_session, world, kind, spec)
    job = await detail(httpx_client, super_admin, response["job_id"])
    assert job["status"] == "completed" and job["result"]["success_count"] == 1


async def test_generation_failure_is_terminal_even_when_celery_returns_normally(
    httpx_client, db_session, world, super_admin
):
    response = await retry(httpx_client, super_admin, world.item)
    output, _ = await core.prepare(
        db_session, world.item.id, world.authority, "metadata"
    )
    token = await objects.claim(db_session, output)
    await db_session.commit()

    @asynccontextmanager
    async def factory():
        yield db_session

    await worker.fail(factory, output.id, token, "private_provider_failure")
    job = await detail(httpx_client, super_admin, response["job_id"])
    assert job["status"] == "failed"
    assert job["error_message"] == "video_asset_retry_failed"
    assert "private_provider_failure" not in str(job)


async def test_unknown_broker_delivery_keeps_same_job_for_redispatch(
    httpx_client, db_session, world, super_admin, monkeypatch
):
    def unavailable(ids):
        raise RuntimeError("s3://private:credential@provider/internal-path")

    monkeypatch.setattr(core, "dispatch", unavailable)
    first = await retry(httpx_client, super_admin, world.item)
    assert first["status"] == "dispatch_pending"
    job = await detail(httpx_client, super_admin, first["job_id"])
    assert job["status"] == "pending" and job["result"]["stage"] == "dispatch_pending"
    assert "credential" not in str(job)
    monkeypatch.setattr(core, "dispatch", lambda ids: None)
    second = await retry(httpx_client, super_admin, world.item)
    assert second["job_id"] == first["job_id"] and second["status"] == "queued"
    await _publish(db_session, world)
    assert (await detail(httpx_client, super_admin, first["job_id"]))[
        "status"
    ] == "completed"


async def test_existing_expiration_finishes_observer_without_new_timeout(owned):
    async with owned.factory() as db:
        output = await _new(db, owned, old=True)
        job = await jobs.observe(db, output, owned.user_id, "probe")
        await db.commit()
        await objects._expire_building(db, objects.utcnow() + timedelta(hours=2), 100)
        await db.refresh(job)
        assert (
            job.status == "failed" and job.error_message == "video_asset_retry_expired"
        )


async def test_real_decoder_publication_finishes_shared_job(real_video):
    async with real_video.factory() as db:
        output, _ = await core.prepare(
            db, real_video.item_id, real_video.authority, "metadata"
        )
        job = await jobs.observe(db, output, real_video.user_id, "poster")
        output_id, job_id = output.id, job.id
        await db.commit()
    await worker.run_outputs([str(output_id)])
    async with real_video.factory() as db:
        job = await db.get(AsyncJob, job_id)
        assert job.status == "completed" and job.result["success_count"] == 1
