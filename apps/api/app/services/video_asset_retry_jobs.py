"""Project saved video-output facts into the existing private job detail."""

import uuid

from sqlalchemy import text

from app.db.models.async_job import AsyncJobKind
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.jobs_lifecycle_data import job_lock_statement

KIND = AsyncJobKind.VIDEO_ASSET_RETRY.value
ACTIVE = ("pending", "running")


async def observe(db, output, user_id: uuid.UUID, asset_type: str):
    """Called while prepare holds the Item lock; never reuse another actor's job."""
    await db.flush()
    job_id = await db.scalar(
        text(
            "SELECT yoao_jobs_v1.find_video_retry_job("
            "CAST(:output_id AS uuid), CAST(:user_id AS uuid), CAST(:asset_type AS text))"
        ),
        {"output_id": output.id, "user_id": user_id, "asset_type": asset_type},
    )
    job = await jobs.get_job(db, job_id) if job_id is not None else None
    if job is None:
        job = await jobs.create_job(
            db,
            kind=KIND,
            user_id=user_id,
            payload={
                "output_id": str(output.id),
                "dataset_item_id": str(output.dataset_item_id),
                "asset_type": asset_type,
            },
        )
    await _project(db, job, output)
    return job


async def dispatch_pending(db, job_id):
    """A broker exception does not prove that delivery or execution failed."""
    job = await db.scalar(
        job_lock_statement(job_id, mode="update", populate_existing=True)
    )
    if job is not None and job.status == "pending":
        job.result = {"stage": "dispatch_pending"}


def _failure(output, asset_type):
    if output.state != "ready":
        return (
            "video_asset_retry_expired"
            if output.error_code == "lease_expired"
            else "video_asset_retry_failed"
        )
    if asset_type in {"probe", "poster", "frame_timetable"}:
        video = (output.result or {}).get("video") or {}
        if video.get(f"{asset_type}_error"):
            return f"video_asset_retry_{asset_type}_failed"
        if asset_type == "poster" and not video.get("poster_frame_path"):
            return "video_asset_retry_poster_failed"
        if asset_type == "frame_timetable" and not (output.result or {}).get("frames"):
            return "video_asset_retry_frame_timetable_failed"
    return None


async def _project(db, job, output):
    if job.status not in ACTIVE:
        return
    if output.state == "building":
        if output.lease_token is not None:
            await jobs.mark_running(db, job.id)
            job.result = {"stage": "processing"}
        else:
            job.result = {"stage": "queued"}
        return
    error = _failure(output, job.payload["asset_type"])
    result = {
        "asset_type": job.payload["asset_type"],
        "success_count": 0 if error else 1,
        "failed_count": 1 if error else 0,
    }
    if error:
        await jobs.mark_failed(db, job.id, error=error, result=result)
    else:
        await jobs.mark_complete(db, job.id, result=result)
    await notify_job_terminal(db, job_id=job.id)


async def sync_output(db, output):
    """Called after locking the Output, in its existing transition transaction."""
    await db.flush()
    identifiers = await db.scalars(
        text(
            "SELECT * FROM yoao_jobs_v1.lock_video_retry_jobs(CAST(:output_id AS uuid))"
        ),
        {"output_id": output.id},
    )
    for identifier in identifiers:
        job = await db.scalar(
            job_lock_statement(identifier, mode="update", populate_existing=True)
        )
        if job is not None:
            await _project(db, job, output)
