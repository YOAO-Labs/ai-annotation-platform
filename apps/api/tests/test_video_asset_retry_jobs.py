"""Storage retry jobs observe existing output attempts without owning execution."""

from datetime import timedelta

from app.db.models.async_job import AsyncJob
from app.services import video_asset_retry_jobs as jobs
from app.services import video_derivative_objects as objects
from app.services import video_derivatives as core
from app.workers import media_derivatives as worker
from tests.test_video_derivative_objects import _new, owned as owned
from tests.test_video_derivative_pipeline import real_video as real_video


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
